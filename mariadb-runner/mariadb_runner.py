"""
MariaDB/MySQL reusable query runner — installable package.
Canonical source: jaciturazzi/mariadb-runner/mariadb_runner.py

Install once into any venv:
    pip install -e /path/to/jaciturazzi/mariadb-runner/

Usage:
    from mariadb_runner import MariaDBRunner

    runner = MariaDBRunner(host="...", user="...", password="...", db="main")

    # Single query
    rows = runner.execute("SELECT id, email FROM users WHERE active = %s", (1,))

    # N independent queries in parallel — results in input order
    results = runner.parallel_execute([
        ("SELECT COUNT(*) AS c FROM table_a", None),
        ("SELECT MAX(id) AS m FROM table_b", None),
    ])

    # Full-table scan — single PK
    rows = runner.chunked_scan(
        table="orders",
        partition_col="order_id",
        sql=(
            "SELECT {partition_col}, status, total "
            "FROM {table} "
            "WHERE {partition_col} > %(lower)s AND {partition_col} <= %(upper)s"
        ),
    )

    # Full-table scan — composite PK (user_id, item_id, created_at)
    # Chunk on any one indexed numeric column; the others are just payload.
    rows = runner.chunked_scan(
        table="user_items",
        partition_col="user_id",
        index_hint="PRIMARY",
        sql=(
            "SELECT user_id, item_id, created_at, quantity "
            "FROM {table} "
            "WHERE {partition_col} > %(lower)s AND {partition_col} <= %(upper)s"
        ),
        chunk_size=50_000,
        workers=4,
    )

    # Streaming to file — does not accumulate rows in memory
    import csv
    with open("out.csv", "w", newline="") as f:
        writer = None
        def write(chunk_rows):
            nonlocal writer
            if not chunk_rows:
                return
            if writer is None:
                writer = csv.DictWriter(f, fieldnames=chunk_rows[0].keys())
                writer.writeheader()
            writer.writerows(chunk_rows)
        runner.chunked_scan(..., on_chunk=write)
"""

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

import pymysql
import pymysql.cursors

logger = logging.getLogger(__name__)


class MariaDBRunner:
    """
    Reusable MySQL/MariaDB query executor.

    Capabilities:
      execute()          — single query, with server-load backpressure
      parallel_execute() — N independent queries in parallel, results ordered
      chunked_scan()     — parallel full-table scan partitioned on any indexed
                           numeric column; works with single or composite PKs
    """

    def __init__(
        self,
        host: str,
        user: str,
        password: str,
        db: str,
        workers: int = 4,
        chunk_size: int = 100_000,
        backpressure_threshold: int = 50,
        read_uncommitted: bool = True,
        connect_timeout: int = 15,
    ):
        self.host = host
        self.user = user
        self.password = password
        self.db = db
        self.workers = workers
        self.chunk_size = chunk_size
        self.backpressure_threshold = backpressure_threshold
        self.read_uncommitted = read_uncommitted
        self.connect_timeout = connect_timeout

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_conn(self) -> pymysql.connections.Connection:
        return pymysql.connect(
            host=self.host,
            user=self.user,
            password=self.password,
            database=self.db,
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=True,
            connect_timeout=self.connect_timeout,
        )

    def _wait_if_busy(self, conn: pymysql.connections.Connection) -> None:
        """Block until Threads_running drops below backpressure_threshold."""
        with conn.cursor() as cur:
            cur.execute("SHOW GLOBAL STATUS LIKE 'Threads_running'")
            threads = int(cur.fetchone()["Value"])
        if threads > self.backpressure_threshold:
            logger.warning(
                "Server under load (%d threads running). Backing off 3s...", threads
            )
            time.sleep(3)
            self._wait_if_busy(conn)

    def _set_session(self, conn: pymysql.connections.Connection) -> None:
        with conn.cursor() as cur:
            if self.read_uncommitted:
                cur.execute("SET SESSION TRANSACTION ISOLATION LEVEL READ UNCOMMITTED")
            # Prevent server from killing long-running analytical queries
            cur.execute("SET SESSION net_read_timeout = 3600")
            cur.execute("SET SESSION net_write_timeout = 3600")
            cur.execute("SET SESSION wait_timeout = 3600")

    def _run_one(self, sql: str, params: Any) -> list[dict]:
        """Execute one query with a dedicated connection. Used by thread pools."""
        conn = self._get_conn()
        try:
            self._wait_if_busy(conn)
            self._set_session(conn)
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchall()
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Public API: single query
    # ------------------------------------------------------------------

    def execute(self, sql: str, params: Any = None) -> list[dict]:
        """Run a single query and return all rows as a list of dicts."""
        conn = self._get_conn()
        try:
            self._wait_if_busy(conn)
            self._set_session(conn)
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchall()
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Public API: parallel independent queries
    # ------------------------------------------------------------------

    def parallel_execute(
        self,
        queries: list[tuple[str, Any]],
        workers: int | None = None,
    ) -> list[list[dict]]:
        """
        Execute independent (sql, params) pairs in parallel.
        Results are returned in the same order as the input list.

        Example:
            results = runner.parallel_execute([
                ("SELECT COUNT(*) AS c FROM orders WHERE status = %s", ("open",)),
                ("SELECT MAX(id) AS m FROM products", None),
            ])
            open_orders = results[0][0]["c"]
            max_product = results[1][0]["m"]
        """
        _workers = workers or self.workers
        ordered: list[list[dict] | None] = [None] * len(queries)
        with ThreadPoolExecutor(max_workers=_workers) as executor:
            futures = {
                executor.submit(self._run_one, sql, params): i
                for i, (sql, params) in enumerate(queries)
            }
            for future in as_completed(futures):
                ordered[futures[future]] = future.result()
        return ordered  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Public API: chunked full-table scan
    # ------------------------------------------------------------------

    def _get_partition_bounds(
        self,
        table: str,
        partition_col: str,
        index_hint: str | None = None,
    ) -> tuple[int, int]:
        hint = f"FORCE INDEX (`{index_hint}`)" if index_hint else ""
        rows = self.execute(
            f"SELECT MIN(`{partition_col}`) AS min_v, MAX(`{partition_col}`) AS max_v"
            f" FROM `{table}` {hint}".strip()
        )
        if not rows or rows[0]["min_v"] is None:
            return 0, 0
        return int(rows[0]["min_v"]), int(rows[0]["max_v"])

    def chunked_scan(
        self,
        table: str,
        partition_col: str,
        sql: str,
        index_hint: str | None = None,
        chunk_size: int | None = None,
        workers: int | None = None,
        on_chunk: Callable[[list[dict]], None] | None = None,
    ) -> list[dict]:
        """
        Parallel full-table scan using non-overlapping numeric range chunks.

        Works with any table layout:
          - Single integer PK
          - Composite PK (chunk on any one numeric component)
          - No PK at all (chunk on any indexed numeric column)

        SQL template placeholders:
          {table}          backtick-escaped table name
          {partition_col}  backtick-escaped partition column name
          %(lower)s        exclusive lower bound  (col > lower)
          %(upper)s        inclusive upper bound  (col <= upper)

        Args:
            partition_col: The numeric column used to build range chunks.
                           Must be indexed for performance. Can be any component
                           of a composite PK, or any other indexed numeric column.
            index_hint:    Optional FORCE INDEX name for the MIN/MAX bounds query
                           (e.g. "PRIMARY", "idx_user_id"). Avoids full scans.
            chunk_size:    Rows per chunk. Override self.chunk_size.
            workers:       Parallel threads. Override self.workers.
            on_chunk:      Callback receiving each chunk's row list as it lands.
                           Use for streaming to avoid holding all rows in memory.
                           When set, this method returns [].

        Returns:
            Flat list of all rows when on_chunk is None, otherwise [].

        Single PK example:
            runner.chunked_scan(
                table="orders",
                partition_col="order_id",
                sql=(
                    "SELECT {partition_col}, status, total "
                    "FROM {table} "
                    "WHERE {partition_col} > %(lower)s "
                    "  AND {partition_col} <= %(upper)s"
                ),
            )

        Composite PK (user_id, item_id, created_at) example:
            runner.chunked_scan(
                table="user_items",
                partition_col="user_id",
                index_hint="PRIMARY",
                sql=(
                    "SELECT user_id, item_id, created_at, quantity "
                    "FROM {table} "
                    "WHERE {partition_col} > %(lower)s "
                    "  AND {partition_col} <= %(upper)s"
                ),
                chunk_size=50_000,
            )

        Streaming (no memory accumulation):
            import csv
            with open("out.csv", "w", newline="") as f:
                writer = None
                def write(rows):
                    nonlocal writer
                    if not rows:
                        return
                    if writer is None:
                        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
                        writer.writeheader()
                    writer.writerows(rows)
                runner.chunked_scan(..., on_chunk=write)
        """
        _chunk_size = chunk_size or self.chunk_size
        _workers = workers or self.workers

        min_v, max_v = self._get_partition_bounds(table, partition_col, index_hint)
        if min_v == max_v == 0:
            logger.warning(
                "No rows found in %s (partition_col: %s)", table, partition_col
            )
            return []

        resolved_sql = (
            sql
            .replace("{table}", f"`{table}`")
            .replace("{partition_col}", f"`{partition_col}`")
        )

        # Non-overlapping ranges: lower exclusive, upper inclusive
        # Mirrors the Glue job pattern: WHERE col > lower AND col <= upper
        chunks: list[dict] = []
        lower = min_v - 1
        while lower < max_v:
            upper = min(lower + _chunk_size, max_v)
            chunks.append({"lower": lower, "upper": upper})
            lower = upper

        logger.info(
            "chunked_scan %s.%s: %d chunks × ~%s rows/chunk | range %d–%d",
            table, partition_col, len(chunks), f"{_chunk_size:,}", min_v, max_v,
        )

        all_rows: list[dict] = []
        with ThreadPoolExecutor(max_workers=_workers) as executor:
            futures = {
                executor.submit(self._run_one, resolved_sql, chunk): chunk
                for chunk in chunks
            }
            for future in as_completed(futures):
                chunk_rows = future.result()
                if on_chunk is not None:
                    on_chunk(chunk_rows)
                else:
                    all_rows.extend(chunk_rows)

        return all_rows

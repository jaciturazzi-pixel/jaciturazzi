import csv
import argparse
import logging
from operator import itemgetter

from mariadb_runner import MariaDBRunner

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

LARGE_TABLE_THRESHOLD = 1_000_000

MAX_INT_CAPACITY = {
    'tinyint':   {'signed': 127,                 'unsigned': 255},
    'smallint':  {'signed': 32767,               'unsigned': 65535},
    'mediumint': {'signed': 8388607,             'unsigned': 16777215},
    'int':       {'signed': 2147483647,          'unsigned': 4294967295},
    'bigint':    {'signed': 9223372036854775807,  'unsigned': 18446744073709551615},
}


def _make_row(t_name, t_rows, c, dtype, cap, max_val):
    usage_pct = round((max_val / cap) * 100, 2) if cap > 0 else 0
    return {
        'table':       t_name,
        'approx_rows': t_rows,
        'column':      c['COLUMN_NAME'],
        'data_type':   dtype,
        'full_type':   c['COLUMN_TYPE'],
        'max_used':    max_val,
        'capacity':    cap,
        'usage_pct':   usage_pct,
    }


class ColumnCapacityAnalyzer(MariaDBRunner):
    """
    One pass per table: AUTO_INCREMENT resolved from schema (zero cost), everything
    else covered by a single chunked_scan that computes MAX for all columns at once.
    Falls back to individual MAX() queries only if no partition column exists.
    """

    def _analyze_table(self, table_info: dict) -> list[dict]:
        t_name = table_info['TABLE_NAME']
        t_rows = table_info['TABLE_ROWS'] or 0
        results = []
        scan_cols = []
        partition_col = None

        conn = self._get_conn()
        try:
            self._wait_if_busy(conn)
            self._set_session(conn)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COLUMN_NAME, DATA_TYPE, COLUMN_TYPE,
                           CHARACTER_MAXIMUM_LENGTH, COLUMN_KEY, EXTRA
                    FROM information_schema.COLUMNS
                    WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
                    """,
                    (self.db, t_name),
                )
                columns = cur.fetchall()

                for c in columns:
                    if 'auto_increment' in c.get('EXTRA', '').lower():
                        partition_col = c['COLUMN_NAME']
                        break
                if not partition_col:
                    for c in columns:
                        if c['DATA_TYPE'].lower() in MAX_INT_CAPACITY and 'PRI' in c.get('COLUMN_KEY', ''):
                            partition_col = c['COLUMN_NAME']
                            break

                for c in columns:
                    dtype = c['DATA_TYPE'].lower()
                    cap = None

                    if dtype in MAX_INT_CAPACITY:
                        is_uns = 'unsigned' in c['COLUMN_TYPE'].lower()
                        cap = MAX_INT_CAPACITY[dtype]['unsigned' if is_uns else 'signed']
                    elif c['CHARACTER_MAXIMUM_LENGTH']:
                        cap = int(c['CHARACTER_MAXIMUM_LENGTH'])

                    if not cap:
                        continue

                    # AUTO_INCREMENT: read current value from schema, no query needed
                    if 'auto_increment' in c.get('EXTRA', '').lower() and table_info['AUTO_INCREMENT']:
                        results.append(_make_row(t_name, t_rows, c, dtype, cap, int(table_info['AUTO_INCREMENT'])))
                        continue

                    scan_cols.append({
                        'COLUMN_NAME': c['COLUMN_NAME'],
                        'COLUMN_TYPE': c['COLUMN_TYPE'],
                        'data_type':   dtype,
                        'capacity':    cap,
                    })

                if not scan_cols:
                    return results

                # No partition column — can't chunk, fall back to individual MAX() queries
                if not partition_col:
                    logger.warning(
                        "No partition column for %s — falling back to full-scan MAX(). "
                        "Columns seen: %s",
                        t_name,
                        [(c['COLUMN_NAME'], c['DATA_TYPE'], c['COLUMN_KEY'], c['EXTRA']) for c in columns],
                    )
                    for c in scan_cols:
                        col = f"`{c['COLUMN_NAME']}`"
                        sql = (
                            f"SELECT MAX({col}) AS m FROM `{t_name}`"
                            if c['data_type'] in MAX_INT_CAPACITY else
                            f"SELECT MAX(CHAR_LENGTH({col})) AS m FROM `{t_name}`"
                        )
                        cur.execute(sql)
                        res = cur.fetchone()
                        max_val = res['m'] if res and res['m'] is not None else 0
                        results.append(_make_row(t_name, t_rows, c, c['data_type'], c['capacity'], max_val))
                    return results

        except Exception:
            logger.exception("Schema phase failed for %s", t_name)
            return results
        finally:
            conn.close()

        # Single chunked_scan covering all non-AUTO_INCREMENT columns at once.
        # Each chunk returns MAX(int_col) / MAX(CHAR_LENGTH(str_col)) for the range.
        # on_chunk accumulates running_max — no rows held in memory between chunks.
        exprs = []
        for c in scan_cols:
            col = f"`{c['COLUMN_NAME']}`"
            expr = (
                f"MAX({col}) AS {col}"
                if c['data_type'] in MAX_INT_CAPACITY else
                f"MAX(CHAR_LENGTH({col})) AS {col}"
            )
            exprs.append(expr)

        sql = (
            f"SELECT {', '.join(exprs)} "
            f"FROM {{table}} "
            f"WHERE {{partition_col}} > %(lower)s AND {{partition_col}} <= %(upper)s"
        )

        running_max: dict[str, int] = {c['COLUMN_NAME']: 0 for c in scan_cols}

        def accumulate(chunk_rows: list[dict]) -> None:
            for row in chunk_rows:
                for col_name, val in row.items():
                    if val is not None and int(val) > running_max.get(col_name, 0):
                        running_max[col_name] = int(val)

        logger.info("chunked_scan: %d columns in %s (~%s rows)", len(scan_cols), t_name, f"{t_rows:,}")
        try:
            self.chunked_scan(
                table=t_name,
                partition_col=partition_col,
                sql=sql,
                index_hint="PRIMARY",
                on_chunk=accumulate,
            )
        except Exception:
            logger.exception("chunked_scan failed for %s", t_name)
            return results

        col_meta = {c['COLUMN_NAME']: c for c in scan_cols}
        results.extend(
            _make_row(t_name, t_rows, col_meta[col_name], col_meta[col_name]['data_type'],
                      col_meta[col_name]['capacity'], max_val)
            for col_name, max_val in running_max.items()
        )
        return results

    def run(self, table_filters: list[str] | None = None, output_file: str = 'auditoria_campos.csv') -> None:
        sql = """
            SELECT TABLE_NAME, TABLE_ROWS, AUTO_INCREMENT
            FROM information_schema.TABLES
            WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'
        """
        params: list = [self.db]
        if table_filters:
            placeholders = ', '.join(['%s'] * len(table_filters))
            sql += f" AND TABLE_NAME IN ({placeholders})"
            params.extend(table_filters)

        tables = self.execute(sql, params)
        logger.info("Analyzing %d tables in '%s'...", len(tables), self.db)

        all_results: list[dict] = []
        for t in tables:
            all_results.extend(self._analyze_table(t))

        all_results.sort(key=itemgetter('table', 'data_type'))

        headers = ['table', 'approx_rows', 'column', 'data_type', 'full_type', 'max_used', 'capacity', 'usage_pct']
        with open(output_file, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=headers)
            writer.writeheader()
            writer.writerows(all_results)

        logger.info("Report written to %s", output_file)


def main() -> None:
    parser = argparse.ArgumentParser(description="Column type capacity audit — MariaDB/MySQL")
    parser.add_argument('--db',       required=True)
    parser.add_argument('--host',     required=True)
    parser.add_argument('--user',     default="jaci")
    parser.add_argument('--password', default="wuRfuq-9rodko-cycmej")
    parser.add_argument('--table',    action='append', default=[],
                        help="Filter by table. Repeatable: --table t1 --table t2")
    parser.add_argument('--workers',  type=int, default=5)
    parser.add_argument('--output',   default=None)
    args = parser.parse_args()

    output_file   = args.output or f"{args.db}.csv"
    table_filters = [t.strip() for t in args.table if t and t.strip()]

    analyzer = ColumnCapacityAnalyzer(
        host=args.host, user=args.user, password=args.password,
        db=args.db, workers=args.workers,
    )
    analyzer.run(table_filters=table_filters, output_file=output_file)


if __name__ == "__main__":
    main()
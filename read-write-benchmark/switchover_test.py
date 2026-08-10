#!/usr/bin/env python3
"""
Cluster Switchover Validation Tool
Tests data integrity, downtime, and consistency during a MariaDB/MySQL cluster switchover.

Usage:
  python3 switchover_test.py --writer-host <WRITER_EP> --reader-host <READER_EP> \
    [--port 3306] [--user admin] [--password secret] [--threads 5] [--interval 0.05]
"""

import pymysql
import pymysql.cursors
import time
import threading
import argparse
import sys
import json
import uuid
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Optional


# --- Data Structures ---

@dataclass
class EndpointStats:
    total_ops: int = 0
    successful_ops: int = 0
    failed_ops: int = 0
    first_failure_at: Optional[datetime] = None
    last_failure_at: Optional[datetime] = None
    recovery_at: Optional[datetime] = None
    errors: list = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def downtime_seconds(self) -> float:
        if not self.first_failure_at or not self.recovery_at:
            return 0.0
        return (self.recovery_at - self.first_failure_at).total_seconds()

    def record_success(self):
        with self._lock:
            self.total_ops += 1
            self.successful_ops += 1
            if self.last_failure_at and not self.recovery_at:
                self.recovery_at = datetime.now()

    def record_failure(self, error_msg: str):
        with self._lock:
            now = datetime.now()
            self.total_ops += 1
            self.failed_ops += 1
            if not self.first_failure_at:
                self.first_failure_at = now
            self.last_failure_at = now
            self.recovery_at = None
            if len(self.errors) < 50:
                self.errors.append((now.isoformat(), error_msg))


# --- Core Test Engine ---

class SwitchoverTest:
    def __init__(self, args):
        self.test_uuid = str(uuid.uuid4())[:8]
        self.db_name = "switchover_validation"
        self.table_name = f"seq_test_{self.test_uuid}"

        self.writer_config = {
            'host': args.writer_host,
            'port': args.port,
            'user': args.user,
            'password': args.password,
            'database': self.db_name,
            'connect_timeout': 2,
            'read_timeout': 5,
            'write_timeout': 5,
        }
        self.reader_config = {
            'host': args.reader_host,
            'port': args.port,
            'user': args.user,
            'password': args.password,
            'database': self.db_name,
            'connect_timeout': 2,
            'read_timeout': 5,
            'write_timeout': 5,
        }
        if not args.skip_ssl:
            self.writer_config['ssl'] = {'check_hostname': False}
            self.reader_config['ssl'] = {'check_hostname': False}

        self.num_threads = args.threads
        self.interval = args.interval
        self.running = True

        self.writer_stats = EndpointStats()
        self.reader_stats = EndpointStats()

        self.sequence_counter = 0
        self.seq_lock = threading.Lock()
        self.committed_sequences: list[int] = []
        self.committed_lock = threading.Lock()

        self.start_time: Optional[datetime] = None

    def _connect(self, config: dict) -> pymysql.Connection:
        return pymysql.connect(**config)

    def _next_sequence(self) -> int:
        with self.seq_lock:
            self.sequence_counter += 1
            return self.sequence_counter

    def test_connectivity(self):
        """Test connectivity to both endpoints before starting."""
        print(f"[CONN] Testing writer: {self.writer_config['host']}:{self.writer_config['port']} ... ", end="", flush=True)
        try:
            config = self.writer_config.copy()
            config.pop('database', None)
            conn = self._connect(config)
            with conn.cursor() as cur:
                cur.execute("SELECT @@server_id, @@hostname")
                sid, hostname = cur.fetchone()
            conn.close()
            print(f"OK (server_id={sid}, hostname={hostname})")
        except Exception as e:
            print(f"FAILED\n  Error: {e}")
            sys.exit(1)

        print(f"[CONN] Testing reader: {self.reader_config['host']}:{self.reader_config['port']} ... ", end="", flush=True)
        try:
            config = self.reader_config.copy()
            config.pop('database', None)
            conn = self._connect(config)
            with conn.cursor() as cur:
                cur.execute("SELECT @@server_id, @@hostname")
                sid, hostname = cur.fetchone()
            conn.close()
            print(f"OK (server_id={sid}, hostname={hostname})")
        except Exception as e:
            print(f"FAILED\n  Error: {e}")
            sys.exit(1)

    def setup_schema(self):
        """Create test database and table on writer endpoint (two connections)."""
        # Connection 1: create database
        config_no_db = self.writer_config.copy()
        config_no_db.pop('database', None)
        config_no_db['autocommit'] = True

        print(f"[SETUP] Creating database '{self.db_name}'...")
        conn1 = self._connect(config_no_db)
        try:
            with conn1.cursor() as cur:
                cur.execute(f"CREATE DATABASE IF NOT EXISTS `{self.db_name}`")
        finally:
            conn1.close()

        # Connection 2: create table (connects directly to the database)
        config_with_db = self.writer_config.copy()
        config_with_db['database'] = self.db_name
        config_with_db['autocommit'] = True

        print(f"[SETUP] Creating table '{self.table_name}'...")
        conn2 = self._connect(config_with_db)
        try:
            with conn2.cursor() as cur:
                cur.execute(f"""
                    CREATE TABLE IF NOT EXISTS `{self.table_name}` (
                        seq INT NOT NULL,
                        thread_id INT NOT NULL,
                        test_uuid VARCHAR(8) NOT NULL,
                        server_id INT UNSIGNED DEFAULT NULL,
                        written_at TIMESTAMP(3) DEFAULT CURRENT_TIMESTAMP(3),
                        PRIMARY KEY (seq)
                    ) ENGINE=InnoDB
                """)
                cur.execute("SHOW TABLES LIKE %s", (self.table_name,))
                if not cur.fetchone():
                    print(f"[SETUP] FATAL: Table not created. Check grants.")
                    sys.exit(1)
        finally:
            conn2.close()

        print(f"[SETUP] Schema ready. Table: {self.db_name}.{self.table_name}")
        print(f"[SETUP] Test UUID: {self.test_uuid}")

    def writer_worker(self, thread_id: int):
        """Write sequential records, track which sequences were committed."""
        while self.running:
            conn = None
            seq = self._next_sequence()
            try:
                conn = self._connect(self.writer_config)
                with conn.cursor() as cursor:
                    cursor.execute(
                        f"INSERT INTO `{self.table_name}` (seq, thread_id, test_uuid, server_id) "
                        f"VALUES (%s, %s, %s, @@server_id)",
                        (seq, thread_id, self.test_uuid)
                    )
                conn.commit()
                with self.committed_lock:
                    self.committed_sequences.append(seq)
                self.writer_stats.record_success()
                sys.stdout.write(".")
                sys.stdout.flush()
            except pymysql.Error as err:
                self.writer_stats.record_failure(str(err))
                ts = datetime.now().strftime('%H:%M:%S.%f')[:-3]
                print(f"\n[W-DOWN] {ts} T{thread_id} seq={seq}: {err}")
            except Exception as e:
                self.writer_stats.record_failure(str(e))
            finally:
                if conn:
                    try:
                        conn.close()
                    except Exception:
                        pass
            time.sleep(self.interval)

    def reader_worker(self, thread_id: int):
        """Read from reader endpoint, validate connectivity and lag."""
        warmup = True
        while self.running:
            conn = None
            try:
                conn = self._connect(self.reader_config)
                with conn.cursor() as cursor:
                    cursor.execute(f"SELECT MAX(seq) FROM `{self.table_name}`")
                    cursor.fetchone()
                    cursor.execute("SELECT @@server_id")
                    cursor.fetchone()
                self.reader_stats.record_success()
                warmup = False
                sys.stdout.write("*")
                sys.stdout.flush()
            except pymysql.Error as err:
                # 1146 = table doesn't exist — LB backend not yet replicated (warmup)
                if warmup and err.args[0] == 1146:
                    time.sleep(0.5)
                    continue
                self.reader_stats.record_failure(str(err))
                ts = datetime.now().strftime('%H:%M:%S.%f')[:-3]
                print(f"\n[R-DOWN] {ts} T{thread_id}: {err}")
            except Exception as e:
                self.reader_stats.record_failure(str(e))
            finally:
                if conn:
                    try:
                        conn.close()
                    except Exception:
                        pass
            time.sleep(self.interval)

    def validate_data_integrity(self) -> dict:
        """Post-switchover: check for gaps in committed sequences and split-brain."""
        print("\n\n[VALIDATION] Checking data integrity...")
        time.sleep(2)

        conn = self._connect(self.writer_config)
        try:
            with conn.cursor() as cursor:
                cursor.execute(f"SELECT seq, server_id, written_at FROM `{self.table_name}` ORDER BY seq")
                db_rows = cursor.fetchall()
        finally:
            conn.close()

        db_sequences = {row[0] for row in db_rows}
        server_ids = {row[1] for row in db_rows}

        committed_set = set(self.committed_sequences)

        lost_writes = committed_set - db_sequences
        phantom_writes = db_sequences - committed_set

        if db_sequences:
            max_seq = max(db_sequences)
            expected_full = set(range(1, max_seq + 1))
            gaps_in_db = expected_full - db_sequences
        else:
            gaps_in_db = set()

        split_brain = False
        interleave_count = 0
        if len(server_ids) > 1:
            transitions = 0
            prev_sid = None
            for row in db_rows:
                sid = row[1]
                if prev_sid is not None and sid != prev_sid:
                    transitions += 1
                prev_sid = sid
            split_brain = transitions > 1
            interleave_count = transitions

        return {
            "total_committed_by_client": len(committed_set),
            "total_in_database": len(db_sequences),
            "lost_writes": sorted(lost_writes)[:20],
            "lost_writes_count": len(lost_writes),
            "phantom_writes_count": len(phantom_writes),
            "gaps_in_sequence": sorted(gaps_in_db)[:20],
            "gaps_count": len(gaps_in_db),
            "server_ids_seen": sorted(server_ids),
            "split_brain_detected": split_brain,
            "server_id_transitions": interleave_count,
        }

    def validate_read_consistency(self) -> dict:
        """Verify reader endpoint can see the latest committed data."""
        print("[VALIDATION] Checking read consistency (reader sees latest writes)...")
        time.sleep(1)

        conn_w = self._connect(self.writer_config)
        try:
            with conn_w.cursor() as cur:
                cur.execute(f"SELECT MAX(seq), COUNT(*) FROM `{self.table_name}`")
                writer_max, writer_count = cur.fetchone()
        finally:
            conn_w.close()

        reader_max = None
        reader_count = 0
        for _ in range(10):
            try:
                conn_r = self._connect(self.reader_config)
                try:
                    with conn_r.cursor() as cur:
                        cur.execute(f"SELECT MAX(seq), COUNT(*) FROM `{self.table_name}`")
                        reader_max, reader_count = cur.fetchone()
                finally:
                    conn_r.close()
                if reader_max == writer_max:
                    break
            except Exception:
                pass
            time.sleep(1)

        return {
            "writer_max_seq": writer_max,
            "writer_row_count": writer_count,
            "reader_max_seq": reader_max,
            "reader_row_count": reader_count,
            "consistent": writer_max == reader_max,
            "replication_lag_rows": (writer_count - reader_count) if writer_count and reader_count else None,
        }

    def print_report(self, integrity: dict, consistency: dict):
        """Final human-readable report."""
        elapsed = (datetime.now() - self.start_time).total_seconds()

        print("\n" + "=" * 70)
        print("           SWITCHOVER VALIDATION REPORT")
        print("=" * 70)
        print(f"  Test UUID:        {self.test_uuid}")
        print(f"  Duration:         {elapsed:.1f}s")
        print(f"  Writer endpoint:  {self.writer_config['host']}:{self.writer_config['port']}")
        print(f"  Reader endpoint:  {self.reader_config['host']}:{self.reader_config['port']}")
        print()

        print("--- DOWNTIME ---")
        print(f"  Writer downtime:  {self.writer_stats.downtime_seconds:.3f}s "
              f"({self.writer_stats.failed_ops} failed / {self.writer_stats.total_ops} total ops)")
        print(f"  Reader downtime:  {self.reader_stats.downtime_seconds:.3f}s "
              f"({self.reader_stats.failed_ops} failed / {self.reader_stats.total_ops} total ops)")
        if self.writer_stats.first_failure_at:
            print(f"  Writer first failure: {self.writer_stats.first_failure_at.strftime('%H:%M:%S.%f')[:-3]}")
            if self.writer_stats.recovery_at:
                print(f"  Writer recovered:     {self.writer_stats.recovery_at.strftime('%H:%M:%S.%f')[:-3]}")
        if self.reader_stats.first_failure_at:
            print(f"  Reader first failure: {self.reader_stats.first_failure_at.strftime('%H:%M:%S.%f')[:-3]}")
            if self.reader_stats.recovery_at:
                print(f"  Reader recovered:     {self.reader_stats.recovery_at.strftime('%H:%M:%S.%f')[:-3]}")
        print()

        print("--- DATA INTEGRITY ---")
        print(f"  Committed by client:  {integrity['total_committed_by_client']}")
        print(f"  Found in database:    {integrity['total_in_database']}")
        print(f"  Lost writes:          {integrity['lost_writes_count']}")
        if integrity['lost_writes']:
            print(f"    Sequences lost:     {integrity['lost_writes']}")
        print(f"  Gaps in sequence:     {integrity['gaps_count']}")
        if integrity['gaps_in_sequence']:
            print(f"    Gap sequences:      {integrity['gaps_in_sequence']}")
        print(f"  Phantom writes:       {integrity['phantom_writes_count']}")
        print()

        print("--- SPLIT BRAIN ---")
        print(f"  Server IDs seen:      {integrity['server_ids_seen']}")
        print(f"  Server ID transitions: {integrity['server_id_transitions']}"
              f" (1 = clean switchover)")
        status = "YES — CRITICAL" if integrity['split_brain_detected'] else "No"
        print(f"  Split-brain:          {status}")
        print()

        print("--- READ CONSISTENCY ---")
        print(f"  Writer max seq:       {consistency['writer_max_seq']}")
        print(f"  Reader max seq:       {consistency['reader_max_seq']}")
        print(f"  Consistent:           {'Yes' if consistency['consistent'] else 'NO — LAG DETECTED'}")
        if consistency['replication_lag_rows']:
            print(f"  Replication lag:      {consistency['replication_lag_rows']} rows behind")
        print()

        print("--- VERDICT ---")
        issues = []
        if integrity['lost_writes_count'] > 0:
            issues.append(f"DATA LOSS: {integrity['lost_writes_count']} writes lost")
        if integrity['split_brain_detected']:
            issues.append("SPLIT BRAIN detected")
        if not consistency['consistent']:
            issues.append("Reader not consistent with writer")
        if self.writer_stats.downtime_seconds > 30:
            issues.append(f"Writer downtime excessive: {self.writer_stats.downtime_seconds:.1f}s")

        if not issues:
            print("  PASS — Zero data loss, acceptable downtime, no split-brain.")
        else:
            print("  FAIL —")
            for issue in issues:
                print(f"    - {issue}")
        print("=" * 70)

        report = {
            "test_uuid": self.test_uuid,
            "duration_seconds": elapsed,
            "writer_downtime_s": self.writer_stats.downtime_seconds,
            "reader_downtime_s": self.reader_stats.downtime_seconds,
            "integrity": integrity,
            "consistency": consistency,
            "pass": len(issues) == 0,
        }
        report_file = f"switchover_report_{self.test_uuid}.json"
        with open(report_file, 'w') as f:
            json.dump(report, f, indent=2, default=str)
        print(f"\n[INFO] JSON report saved: {report_file}")

    def wait_for_reader_schema(self, timeout: int = 30):
        """Wait until ALL reader LB backends can see the test table."""
        print("[SETUP] Waiting for schema to replicate to reader backends...", end="", flush=True)
        config = self.reader_config.copy()
        config.pop('database', None)
        # Need consecutive successes to cover multiple LB backends
        consecutive_ok = 0
        needed = 5
        for _ in range(timeout * 2):
            conn = None
            try:
                conn = self._connect(config)
                with conn.cursor() as cursor:
                    cursor.execute(f"SELECT 1 FROM `{self.db_name}`.`{self.table_name}` LIMIT 1")
                    cursor.fetchall()
                consecutive_ok += 1
                if consecutive_ok >= needed:
                    print(f" OK ({needed} consecutive checks passed)")
                    return
            except pymysql.Error:
                consecutive_ok = 0
            finally:
                if conn:
                    try:
                        conn.close()
                    except Exception:
                        pass
            time.sleep(0.5)
            print(".", end="", flush=True)
        print(" TIMEOUT (proceeding anyway — some backends may lag)")

    def run(self):
        self.test_connectivity()
        self.setup_schema()
        self.wait_for_reader_schema()
        self.start_time = datetime.now()

        print(f"\n[START] Launching {self.num_threads} writer + {self.num_threads} reader threads")
        print(f"[START] Writing and reading in progress, please, perform the switchover in the cluster.")
        print(f"[START] Do the switchover in another terminal. Press Ctrl+C when finished.\n")

        threads = []
        for i in range(self.num_threads):
            t = threading.Thread(target=self.writer_worker, args=(i,), daemon=True)
            t.start()
            threads.append(t)
        for i in range(self.num_threads):
            t = threading.Thread(target=self.reader_worker, args=(i,), daemon=True)
            t.start()
            threads.append(t)

        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n\n[STOP] Stopping test, running validation...")
            self.running = False
            time.sleep(2)

        try:
            integrity = self.validate_data_integrity()
            consistency = self.validate_read_consistency()
            self.print_report(integrity, consistency)
        except Exception as e:
            print(f"\n[ERROR] Validation failed: {e}")
            print("  Ensure both endpoints are reachable for post-test validation.")


def main():
    parser = argparse.ArgumentParser(
        description="Cluster Switchover Validation Tool — tests data integrity and downtime"
    )
    parser.add_argument("--writer-host", required=True, help="Writer/primary endpoint")
    parser.add_argument("--reader-host", required=True, help="Reader/replica endpoint")
    parser.add_argument("--port", type=int, default=3306, help="DB port (default: 3306)")
    parser.add_argument("--user", default="admin", help="DB user (default: admin)")
    parser.add_argument("--password", required=True, help="DB password")
    parser.add_argument("--threads", type=int, default=5, help="Threads per endpoint (default: 5)")
    parser.add_argument("--interval", type=float, default=0.05,
                        help="Seconds between operations per thread (default: 0.05)")
    parser.add_argument("--skip-ssl", action="store_true",
                        help="Disable TLS certificate verification")
    args = parser.parse_args()

    test = SwitchoverTest(args)
    test.run()


if __name__ == "__main__":
    main()

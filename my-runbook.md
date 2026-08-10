#### Master - Master (Binlog-based)

**1. Set auto_increment on both masters**

Old Master (current master)
```sql
-- `increment` defines the ID range size (added to last auto_inc), `offset` is the exclusive position each server gets within that range.
SET GLOBAL auto_increment_increment = 2;
SET GLOBAL auto_increment_offset = 1;
```

New Master candidate:
```sql
SET GLOBAL auto_increment_increment = 2;
SET GLOBAL auto_increment_offset = 2;
```


**2. Set up Master x Master: Old Master replicates from New Master**

Old Master (current master)
```sql
FLUSH LOGS; -- clean binlog boundary before setting up replication

CHANGE MASTER TO
  master_host='002.stag-sql-awards.us-west-2.pool.minicliptech.com',
  master_user='slave_ssl',
  master_password='<from_vault>',
  master_log_file='replication.000002',
  master_log_pos=7563889,
  Master_SSL=1,
  Master_SSL_Verify_Server_Cert=1,
  Master_SSL_CA='/opt/minicryption/certs/ca-bundle.pem',
  MASTER_SSL_CAPATH='',
  Master_SSL_Cert="/opt/minicryption/certs/cert.pem",
  Master_SSL_Cipher="",
  Master_SSL_Key="/opt/minicryption/certs/key.pem";

START SLAVE;
SHOW SLAVE STATUS\G
-- confirm: Slave_IO_Running: Yes, Slave_SQL_Running: Yes, Seconds_Behind_Master: 0
```

**3. Freeze read slaves at a known position**

Across all read slaves
```sql
STOP SLAVE;
START SLAVE UNTIL master_log_file='replication.000004', master_log_pos=1000;
-- master_log_file can be a file ahead of the current one, then you flush logs on the master to push at it
SHOW SLAVE STATUS;
```

Old Master (current master)
```sql
FLUSH LOGS; -- force binlog rotation so slaves reach the UNTIL position
```

Get the real Exec_Master_Log_Pos where the slaves stopped:
```sql
SHOW SLAVE STATUS\G
```


**4. Correlate binlog positions between old and new master**

Go to the old master and search for that position:
```shell
mysqlbinlog -vvv --base64-output=decode-rows /var/lib/mysql/replication.000003 | grep "end_log_pos 7564065" -A 40 -B 40
```
Get some part of the query or the GTID position if it has one (even if not enabled).

If a GTID order error returns, use this flag: `--skip-gtid-strict-mode`

Go to the new master and search for the same query:
```shell
mysqlbinlog -vvv --base64-output=decode-rows /var/lib/mysql/replication.000003 | grep "(2952, 4" -A 40 -B 40
```
You need to get the next `# at <pos>` — that will be your position to configure the replicas.

**5. Point read replicas to the new master**

```sql
CHANGE MASTER TO
  master_host='002.stag-sql-awards.us-west-2.pool.minicliptech.com',
  master_user='slave_ssl',
  master_password='tPH1iw6zy',
  master_log_file='replication.000002',
  master_log_pos=7563889,
  Master_SSL=1,
  Master_SSL_Verify_Server_Cert=1,
  Master_SSL_CA='/opt/minicryption/certs/ca-bundle.pem',
  MASTER_SSL_CAPATH='',
  Master_SSL_Cert="/opt/minicryption/certs/cert.pem",
  Master_SSL_Cipher="",
  Master_SSL_Key="/opt/minicryption/certs/key.pem";

START SLAVE;
SHOW SLAVE STATUS\G
```

**6. Cut traffic**

- Change the writer CNAME on Route53, pointing to the new master
- Wait for old master connections to drain

**7. Cleanup**

New master:
```sql
STOP SLAVE;
RESET SLAVE ALL;
```

New master and old master:
```sql
SET GLOBAL auto_increment_increment = 1;
SET GLOBAL auto_increment_offset = 1;
```



---

#### Master - Master (GTID-based)

To enable GTID replication on an existing replica:
```sql
STOP SLAVE;
CHANGE MASTER TO MASTER_USE_GTID = slave_pos;
START SLAVE;
SHOW SLAVE STATUS\G
```

**1. Set auto_increment on both masters**

```sql
-- Old Master (current master)
SET GLOBAL auto_increment_increment = 2;
SET GLOBAL auto_increment_offset = 1;

-- New Master candidate
SET GLOBAL auto_increment_increment = 2;
SET GLOBAL auto_increment_offset = 2;
```

**2. Set up Master x Master: Old Master replicates from New Master**

Old Master (current master) - set as replica from the new master
```sql
CHANGE MASTER TO
  master_host='002.stag-sql-awards.us-west-2.pool.minicliptech.com',
  master_user='slave_ssl',
  master_password='tPH1iw6zy',
  Master_SSL=1,
  Master_SSL_Verify_Server_Cert=1,
  Master_SSL_CA='/opt/minicryption/certs/ca-bundle.pem',
  MASTER_SSL_CAPATH='',
  Master_SSL_Cert="/opt/minicryption/certs/cert.pem",
  Master_SSL_Cipher="",
  Master_SSL_Key="/opt/minicryption/certs/key.pem",
  MASTER_USE_GTID = current_pos; -- uses local gtid_binlog_pos as starting point (includes own writes)

START SLAVE;
SHOW SLAVE STATUS\G
-- confirm: Slave_IO_Running: Yes, Slave_SQL_Running: Yes, Seconds_Behind_Master: 0
```

**3. Point all read slaves to the new master**

```sql
STOP SLAVE;

CHANGE MASTER TO
  master_host='002.stag-sql-awards.us-west-2.pool.minicliptech.com',
  MASTER_USE_GTID = slave_pos;
-- slave_pos is correct here — these are pure replicas, they only track received GTIDs

START SLAVE;
SHOW SLAVE STATUS\G
```

**4. Cut traffic**

- Change the writer CNAME on Route53, pointing to the new master
- Wait for old master connections to drain

**5. Cleanup**

New master:
```sql
STOP SLAVE;
RESET SLAVE ALL;
```

New master and old master:
```sql
SET GLOBAL auto_increment_increment = 1;
SET GLOBAL auto_increment_offset = 1;
```

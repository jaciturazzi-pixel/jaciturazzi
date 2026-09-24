# mcplanner — MariaDB Capacity Planning & Rightsizing CLI v2.0

A high-performance Python CLI tool designed for **Capacity Planning** and **Rightsizing** of MariaDB database clusters on AWS (Intel Xeon `r8i`/`m8i` instance families and EBS storage). 

By consuming high-resolution telemetry directly from the **Percona Monitoring and Management (PMM) Prometheus API**, `mcplanner` eliminates false optimization impressions caused by aggregated AWS CloudWatch averages.

---

## Key Technical Foundations

1. **vCPU vs. Physical Cores (SMT) & Fixed Engine Threads**:
   - Accounts for Hyperthreading (e.g., `r8i.xlarge` has 2 physical cores / 4 vCPUs).
   - Evaluates active MariaDB background threads (`innodb_read_io_threads`, `innodb_write_io_threads`, `innodb_page_cleaners`, `innodb_purge_threads`, `slave_parallel_workers`) against available physical cores to prevent thread overcommit.
   - Monitors Context Switch rates (`node_context_switches_total`) and process run queue (`node_procs_running`) to prevent OS scheduler thrashing when reducing vCPU count.

2. **Per-Core Microarchitecture & Monothread Bottleneck**:
   - Analyzes individual vCPU metrics (`node_cpu_seconds_total`) to detect single-core saturation (e.g., single-threaded replication or heavy crons > 75%), even if overall instance average CPU is < 5%.
   - Computes core load skewness coefficient to identify monothread locks.

3. **Replication Micro-Lags (SBM < 10s) Under High Concurrency**:
   - Tracks sub-10-second replication lag episodes (`mysql_slave_status_seconds_behind_master`) over 90-day time series. Any recurring micro-lag spikes during peak traffic automatically veto CPU/concurrency downsizing to protect Read-After-Write consistency.

4. **Real Memory Footprint Beyond Buffer Pool**:
   - Calculates session memory overhead (`sort_buffer_size`, `read_buffer_size`, `join_buffer_size`, `thread_stack`) multiplied by peak `max_used_connections`.
   - Analyzes `mysqld` process RSS, InnoDB LRU page ejection rates (`Innodb_buffer_pool_pages_lru_freed`), and OS `MemAvailable` to prevent Buffer Pool collapse and Linux OOM Killer risk.

5. **Storage Evaluation: gp3 vs. io2 Latency & IOPS Right-Sizing**:
   - Evaluates P99 write latency (`node_disk_write_time_seconds_total / node_disk_writes_completed_total`).
   - If synchronous transaction commit is active (`innodb_flush_log_at_trx_commit=1`) and write latency P99 is critical (> 2ms), `io2` is retained and only provisioned IOPS are right-sized.
   - If latency is sub-millisecond and peak IOPS < 16k, migrates to `gp3` for maximum cost savings.

6. **Dynamic 6-Month Disk Growth Runway**:
   - Performs linear regression on 90-day disk space usage (`node_filesystem_free_bytes`).
   - Projects 6-month growth (+10% safety buffer), triggering online AWS EBS expansion alerts for occupancy > 75%, or volume downsizing recommendations when free space exceeds 300 GB.

---

## Installation

```bash
# Navigate to the project directory
cd mariadb-capacity-planner

# Create a Python virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install the package in editable mode with development dependencies
pip install -e ".[dev]"
```

---

## Configuration

Copy the example configuration file and fill in your PMM credentials and AWS details:

```bash
cp config.example.yaml config.yaml

# Set your PMM password as an environment variable (recommended)
export PMM_PASSWORD="your-pmm-password"
```

---

## CLI Options & Usage Examples

### CLI Command Options (`mcplanner analyze`)

| Argument | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `-c`, `--config` | `path` | **Required** | Path to `config.yaml` configuration file |
| `--profile` | `string` | `miniclippool-databases` | AWS CLI authentication profile |
| `--region` | `string` | `us-west-2` | Target AWS region |
| `--instance` | `string` | `None` | Target single instance name or instance ID (e.g. `prod-sql-awards010` or `i-07af44ef17621173c`) |
| `--tag` | `string` | `None` | Target EC2 tag filter (e.g. `mc:service=awards` or `Cluster=prod-sql-qfpsystem`) |
| `-d`, `--days` | `int` | `90` | Number of recent days to evaluate (default: **90 days / 3 months**) |
| `--start-date` | `string` | `None` | Start Date (`YYYY-MM-DD`). Defaults to end-date minus `--days` if omitted. |
| `--end-date` | `string` | `None` | End Date (`YYYY-MM-DD`). Defaults to today if omitted. |
| `--use-ssm` | `flag` | `False` | Enables AWS SSM remote MariaDB status (`mysql`) and OS disk (`df`) queries |
| `--keep-io2` | `flag` | `False` | Keeps `io2` volume type, but right-sizes provisioned IOPS based on peak usage |
| `-o`, `--output-dir` | `string` | `./reports` | Output directory for timestamped Markdown reports and CSV exports |

---

### Execution Examples

#### 1. Standard 90-Day Fleet Assessment
Analyze all configured nodes in `config.yaml` over the default 90-day evaluation period:
```bash
mcplanner analyze --config config.yaml
```

#### 2. Analyze a Specific Database Instance
Target a single instance by name or AWS Instance ID:
```bash
mcplanner analyze --config config.yaml --instance prod-sql-awards015
```

#### 3. Analyze a Specific Cluster via Tag
Target an entire cluster using EC2 tag filters:
```bash
mcplanner analyze --config config.yaml --tag mc:service=qfpsystem
```

#### 4. Historical Peak Period Analysis (e.g., Black Friday / Holiday Season)
Specify custom start and end dates to evaluate seasonal peak loads:
```bash
mcplanner analyze --config config.yaml --start-date 2025-11-01 --end-date 2025-12-31
```

#### 5. Deep Inspection with AWS SSM Integration
Enable AWS SSM to query internal MariaDB status variables and OS filesystem space directly:
```bash
mcplanner analyze --config config.yaml --use-ssm --instance prod-sql-qfpsystem012
```

#### 6. Retain `io2` Storage with IOPS Optimization
Force keeping `io2` volumes while right-sizing provisioned IOPS to peak requirements:
```bash
mcplanner analyze --config config.yaml --keep-io2
```

#### 7. Verify PMM Prometheus Connectivity
Check API connection and list discovered node and service labels:
```bash
mcplanner check-pmm --config config.yaml
```

#### 8. Raw Telemetry Collection (Debugging)
Dump raw PMM PromQL summaries for a specific host:
```bash
mcplanner collect --config config.yaml --instance prod-sql-awards010 --days 30 --format json
```

---

## Veto Engine Rules

The rightsizing decision engine applies **6 strict technical vetoes**. If any veto triggers on a node, compute downsizing is blocked to guarantee production stability:

| Veto Reason | Condition | Technical Impact |
| :--- | :--- | :--- |
| `PER_CORE_SATURATION` | Hottest core P95 > 75% | Single-thread bottleneck (replication worker, cron query) |
| `CONTEXT_SWITCH_OVERLOAD` | CS/s > 50,000 with < 8 cores | CPU scheduler thrashing under heavy thread concurrency |
| `RUNQUEUE_SATURATION` | `procs_running` > 2× physical cores | True CPU saturation and queuing |
| `REPLICATION_LAG` | SBM P99 > 5s or > 3 micro-lag episodes | Read-After-Write data inconsistency |
| `OOM_RISK` | (Peak RSS + Session Ceiling) > 85% Target RAM | Linux OOM Killer risk on downsized memory |
| `THREAD_OVERCOMMIT` | Fixed background threads > Target physical cores | Engine background thread starvation |

---

## Project Structure

```
mariadb-capacity-planner/
├── pyproject.toml               # Project package specifications and dependencies
├── config.example.yaml          # Template configuration file
├── README.md                    # System documentation
│
├── src/mcplanner/
│   ├── cli.py                   # CLI entry point (Click commands)
│   ├── config.py                # Pydantic configuration loader & EC2/EBS pricing specs
│   ├── models.py                # Typed dataclasses and enums
│   │
│   ├── collectors/
│   │   ├── pmm_client.py        # PromQL HTTP client for PMM Prometheus API
│   │   └── aws_client.py        # boto3 client for EC2 instance & EBS volume metadata
│   │
│   ├── analyzers/
│   │   ├── cpu_analyzer.py      # Per-core saturation, SMT, context switches, skewness
│   │   ├── memory_analyzer.py   # mysqld RSS, Buffer Pool, session overhead, OOM risk
│   │   ├── disk_analyzer.py     # Peak IOPS, P99 fsync latency, gp3 vs io2 verdict
│   │   ├── replication_analyzer.py # SBM micro-lags time series & veto engine
│   │   └── runway_projector.py  # 6-month linear regression growth projection
│   │
│   ├── engine/
│   │   └── rightsizing_engine.py # Decision matrix, veto aggregation & cost calculator
│   │
│   └── reporters/
│       ├── terminal_report.py   # Rich terminal inspection panels and summary tables
│       ├── markdown_report.py   # Executive Markdown report with Mermaid topology diagrams
│       └── csv_export.py        # Granular 40+ column CSV export
│
└── tests/                       # Unit and integration test suite
```

---

## Generated Reports & Outputs

Every run of `mcplanner analyze` generates three distinct output formats:

1. **Terminal Inspection Panel**: Color-coded Rich terminal display showing per-core heatmaps, memory breakdown, disk latency metrics, and veto warnings.
2. **Executive Markdown Report (`.md`)**: Complete report containing financial savings, decision summaries, per-cluster Mermaid topology diagrams, and technical details per host.
3. **Granular CSV Export (`.csv`)**: Data file containing 40+ metric columns per instance for spreadsheet analysis and stakeholder reporting.

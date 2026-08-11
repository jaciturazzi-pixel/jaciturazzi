# 🏆 MariaDB AWS Capacity Planning & Rightsizing Tool

An automated, enterprise-grade assessment tool designed for **MariaDB clusters on AWS EC2 & EBS**. It retrieves CloudWatch infrastructure metrics across custom date ranges or recent sampling windows, inspects internal MariaDB status metrics remotely via **AWS Systems Manager (SSM)**, filters main database data volumes, and generates executive financial and technical downsizing reports.

---

## 🎯 Architecture Strategy & Engineering Directives

1. **100% Intel Xeon Architecture (`r8i` / `m8i`)**: Recommendations stick strictly to 8th Generation Intel Xeon EC2 instance families (`r8i` / `m8i`), maintaining **100% x86_64 binary compatibility** with zero Graviton/ARM migration risk.
2. **Guaranteed Spare Headroom Baseline**: Sizing recommendations evaluate instances against historical peaks, ensuring projected downsized CPU remains **< 50% max utilization**, guaranteeing **> 50% free vCPU headroom** during peak traffic.
3. **Flexible Date Range Evaluation (`--start-date` & `--end-date`)**: Supports independent start/end date flags (e.g., passing only `--start-date` defaults end date to today; passing only `--end-date` calculates start date relative to `--days`).
4. **High-Resolution Short-Spike Capture (5-Minute Sampling)**: Evaluates a 3-day window relative to the evaluation end date at 5-minute granularity (`Period=300`) to capture short-lived 90% CPU spikes from cron jobs and data retention routines that would otherwise be averaged out by 30-day hourly metrics.
5. **Resilient SRE Safety Gates**: Protects against CloudWatch API throttling/network failures by requiring verified metric delivery before emitting any downsizing proposals. If CloudWatch metrics fail, the node is marked as `COLLECTION ERROR` to prevent false positive downsizing.
6. **Strict Main Data Volume Filtering**: Filters EBS volumes attached to `/dev/xvdh`, `/dev/xvdf`, `/dev/sdh`, `/dev/sdf`, or type `io2` (>100GB), explicitly ignoring OS root boot disks (`/dev/xvda`).

---

## ⚡ Quick Start & Environment Setup (First-Time Setup)

When running on any new machine or fresh virtual environment (`venv`), install `boto3` first:

```bash
# 1. Navigate to project directory
cd mariadb-rightsizing

# 2. Create virtual environment (if not created)
python3 -m venv venv
source venv/bin/activate

# 3. Install required dependencies (boto3)
pip install boto3
```

> 💡 **Fix `ModuleNotFoundError: No module named 'boto3'`**: If you see this error, run `pip install boto3` inside your activated `venv`.

---

## 📊 AWS CloudWatch Metrics Reference Table

The tool queries CloudWatch across 2 primary namespaces and flexible sampling windows:

| Namespace | Metric Name | Dimensions | Statistic | Sampling Window | Purpose / Metric Calculation |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **`AWS/EC2`** | `CPUUtilization` | `InstanceId` | `Maximum`, `Average`, `p95` | **30-Day or Custom Date Range** | Evaluates average, P95, and max CPU utilization over specified dates |
| **`AWS/EC2`** | `CPUUtilization` | `InstanceId` | `Maximum` | **3-Day (5-Min Fine)** | Captures fast 5-minute CPU spikes (`Period=300s`) relative to evaluation end date |
| **`AWS/EBS`** | `VolumeReadOps` | `VolumeId` | `Sum` | **30-Day or Custom Date Range** | Total read operations count. Divided by `Period_Seconds` to calculate **Read IOPS/sec** |
| **`AWS/EBS`** | `VolumeWriteOps` | `VolumeId` | `Sum` | **30-Day or Custom Date Range** | Total write operations count. Divided by `Period_Seconds` to calculate **Write IOPS/sec** |
| **`AWS/EBS`** | `VolumeReadBytes` | `VolumeId` | `Sum` | **30-Day or Custom Date Range** | Total bytes read count. Converted to **Read Throughput MB/s** |
| **`AWS/EBS`** | `VolumeWriteBytes` | `VolumeId` | `Sum` | **30-Day or Custom Date Range** | Total bytes written count. Converted to **Read Throughput MB/s** |

---

## 🐬 Internal MariaDB SSM Status Metrics

When `--use-ssm` is enabled, the script executes non-intrusive internal MariaDB queries via `aws ssm send-command` using a status polling loop (`get_command_invocation`):

| Metric Category | Source Variable / Query | Technical Purpose |
| :--- | :--- | :--- |
| **Buffer Pool Config** | `@@innodb_buffer_pool_size` | Total configured InnoDB Buffer Pool size in GB |
| **Real Used Buffer Pool** | `Innodb_buffer_pool_pages_data` / `total` | Exact allocated Buffer Pool occupancy in GB (`(data/total) * config`) |
| **Dirty Pages** | `Innodb_buffer_pool_pages_dirty` | Unwritten dirty data pages currently sitting in RAM |
| **Hit Ratio %** | `Hit Ratio %` | Buffer Pool cache efficiency percentage (Target > 99.5%) |
| **Wait Free Pages** | `Innodb_buffer_pool_wait_free` | Count of times queries waited for free pages (Memory pressure lock) |
| **Threads Running** | `Threads_running` | Queries actively processing in vCPUs at current millisecond |
| **Threads Connected** | `Threads_connected` | Total active open client connections |
| **Max Used Connections** | `Max_used_connections` | Historical peak client connection count |
| **Row Lock Waits** | `Innodb_row_lock_waits` | Total count of InnoDB row locking contentions |

---

## 🔄 How the Script Works

```mermaid
flowchart TD
    A[Start: run_full_assessment.py] --> B{CLI Target Filters}
    B -->|--instance| C[Query specific EC2 node live via Paginator]
    B -->|--tag| D[Query EC2 nodes by AWS Tag via Paginator]
    B -->|Default| E[Query all live *prod-sql* nodes via Paginator]
    
    C & D & E --> F{Custom Date Range?}
    F -->|--start-date / --end-date| G[Query CloudWatch for custom YYYY-MM-DD window]
    F -->|Default| H[Query CloudWatch for recent --days 30 window]
    
    G & H --> I[EC2 & EBS Metrics Collection with Safety Verification]
    I -->|EC2 CPU| J[Custom/30d Max/Avg + 3d 5-min Spikes relative to End Date]
    I -->|EBS Storage| K[ReadOps + WriteOps Sum -> Peak Used IOPS]
    
    J & K --> L{--use-ssm enabled?}
    L -->|Yes| M[Query MariaDB via SSM Status Polling Loop]
    L -->|No / Fallback| N[Use AWS Infrastructure Metrics]
    
    M & N --> O[Sizing Engine & Savings Calculator with Safety Gates]
    O --> P[Print Senior DBA Inspection Panel]
    P --> Q[Print Consolidated Summary Table]
    Q --> R[Export Timestamped Markdown Report .md]
    R --> S[Export Consolidated CSV Data .csv]
```

---

## 💻 Portable 1-Line Execution Commands

Once `pip install boto3` is completed inside `venv`:

#### Full Inventory Assessment (All 30 Production Nodes):
```bash
python3 run_full_assessment.py --profile miniclippool-databases --region us-west-2 --use-ssm
```

#### Single Instance Assessment (e.g., `prod-sql-qfp013`):
```bash
python3 run_full_assessment.py --profile miniclippool-databases --region us-west-2 --instance prod-sql-qfp013 --use-ssm
```

#### Entire Cluster Assessment by Tag (e.g., `mc:service=secureinventory`):
```bash
python3 run_full_assessment.py --profile miniclippool-databases --region us-west-2 --tag mc:service=secureinventory --use-ssm
```

#### Flexible Date Range Assessment:
```bash
python3 run_full_assessment.py --profile miniclippool-databases --region us-west-2 --start-date 2026-07-15 --use-ssm
```

---

## 📋 CLI Arguments Reference

| Argument | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `--profile` | `string` | `miniclippool-databases` | AWS CLI authentication profile |
| `--region` | `string` | `us-west-2` | Target AWS region |
| `--instance` | `string` | `None` | Target single instance name or instance ID |
| `--tag` | `string` | `None` | Target tag filter (e.g., `mc:service=awards`) |
| `--days` | `int` | `30` | Number of recent days to evaluate (if dates not provided) |
| `--start-date` | `string` | `None` | Start Date (`YYYY-MM-DD`). Defaults to end-date minus `--days` if omitted. |
| `--end-date` | `string` | `None` | End Date (`YYYY-MM-DD`). Defaults to today if omitted. |
| `--use-ssm` | `flag` | `False` | Enables AWS SSM remote MariaDB status queries |
| `--output-dir` | `string` | `./reports` | Output directory for timestamped Markdown reports |

---

## 📋 Consolidated Summary Table Output Format

```text
═══════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════
📊 FINAL CONSOLIDATED SUMMARY - MARIADB CAPACITY, PERFORMANCE & DOWNSIZING RECOMMENDATIONS
═══════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════
| Instance                     | Current EC2   | Proposed Intel     | Peak CPU% | Proj CPU% | Data Volume  | Prov IOPS  | Peak Used IOPS  | Storage Recommendation | EC2 Savings  | EBS Savings  | Total Savings ($/mo) |
|------------------------------|---------------|--------------------|-----------|-----------|--------------|------------|-----------------|------------------------|--------------|--------------|----------------------|
| prod-sql-awards010           | r8i.2xlarge   | r8i.xlarge         | 9.7%      | 19.4%     | 2048GB io2   | 5000 IOPS  | 412.8 IOPS      | gp3 (4000 IOPS)        | $190.26      | $412.16      | $602.42              |
| prod-sql-awards012           | r8i.2xlarge   | r8i.xlarge         | 5.1%      | 10.2%     | 2048GB io2   | 5000 IOPS  | 298.6 IOPS      | gp3 (4000 IOPS)        | $190.26      | $412.16      | $602.42              |
| prod-sql-community010        | r8i.2xlarge   | r8i.xlarge         | 10.6%     | 21.3%     | 2048GB io2   | 5000 IOPS  | 534.1 IOPS      | gp3 (4000 IOPS)        | $190.26      | $347.16      | $537.42              |
| prod-sql-secureinventory002  | r6in.16xlarge | r8i.8xlarge        | 15.7%     | 31.3%     | 9500GB io2   | 8000 IOPS  | 3135.9 IOPS     | gp3 (4390 IOPS)        | $1,841.79    | $942.50      | $2,784.29            |
═══════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════
```

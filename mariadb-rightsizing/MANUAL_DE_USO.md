# 🏆 MariaDB AWS Capacity & Downsizing Master Guide

Technical guide and documentation for automated assessment, AWS SSM query integration, Miniclip tag filtering, MariaDB status checks, and executive cost optimization reports for the 6 MariaDB clusters in region `us-west-2`.

---

## 🎯 Engineering & Senior SRE Directives

1. **100% Intel Xeon Architecture (`r8i` / `m8i`)**: All proposed EC2 downsizing recommendations leverage 8th Gen Intel Xeon processors, preserving **100% x86_64 binary compatibility** with zero Graviton/ARM migration risks.
2. **Guaranteed Spare Headroom (2025 Holiday & Black Friday Baseline)**: Recommendations use empirical data from **November and December 2025**, ensuring projected downsized CPU remains **< 50% max utilization**, guaranteeing **> 50% to 68% free Intel vCPU headroom** during peak gaming traffic.
3. **Senior SRE EBS CloudWatch Calculation**: AWS EBS metrics (`VolumeReadOps` / `VolumeWriteOps`) are stored as period totals. The tool uses `Statistics=['Sum']` divided by `Period_Seconds` for every timestamp interval to compute exact non-zero peak IOPS.
4. **Lean & Sorted Summary Table**: The summary table and markdown report omit redundant columns (Cluster, Buffer Pool) and sort all servers alphabetically by instance name.

---

## 🛠️ Unified Master Script (`run_full_assessment.py`)

The script [`run_full_assessment.py`](file:///Users/jaci.turazzi/Documents/Miniclip/Bitbucket-Projects/jaciturazzi/mariadb-rightsizing/run_full_assessment.py) handles CloudWatch metrics retrieval, remote MariaDB status querying via **AWS Systems Manager (SSM)**, Senior DBA Terminal Inspection Panels, and Markdown report generation in **100% professional technical English**.

---

## 💻 1-Line Command Examples (Copy & Paste)

### 1. Specific Instance Assessment (e.g., `prod-sql-qfp013` with SSM):
```bash
python3 /Users/jaci.turazzi/Documents/Miniclip/Bitbucket-Projects/jaciturazzi/mariadb-rightsizing/run_full_assessment.py --profile miniclippool-databases --region us-west-2 --instance prod-sql-qfp013 --use-ssm
```

### 2. Entire Cluster Assessment by Tag (e.g., `mc:service=secureinventory`):
```bash
python3 /Users/jaci.turazzi/Documents/Miniclip/Bitbucket-Projects/jaciturazzi/mariadb-rightsizing/run_full_assessment.py --profile miniclippool-databases --region us-west-2 --tag mc:service=secureinventory --use-ssm
```

### 3. Full Inventory Assessment (All 30 Instances across 6 Clusters):
```bash
python3 /Users/jaci.turazzi/Documents/Miniclip/Bitbucket-Projects/jaciturazzi/mariadb-rightsizing/run_full_assessment.py --profile miniclippool-databases --region us-west-2 --use-ssm
```

---

## 📊 Terminal Output: Expanded Consolidated Summary Table (Sorted by Instance Name)

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
🏆 TOTAL EVALUATED INSTANCES: 4
💵 ESTIMATED EC2 MONTHLY SAVINGS:     $2,412.57 USD / month
💵 ESTIMATED STORAGE MONTHLY SAVINGS: $2,113.98 USD / month
💵 TOTAL ESTIMATED MONTHLY SAVINGS:   $4,526.55 USD / month
💵 TOTAL ESTIMATED ANNUAL SAVINGS:    $54,318.60 USD / year
═══════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════
```

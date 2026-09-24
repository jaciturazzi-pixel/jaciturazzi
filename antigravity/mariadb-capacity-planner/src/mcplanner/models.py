"""
mcplanner.models — Typed dataclasses for all analysis results, configurations, and intermediate data.

Every module in the project produces and consumes these models, ensuring strict
type safety and clear contracts between collectors, analyzers, engine, and reporters.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class NodeRole(str, Enum):
    WRITER = "writer"
    READER = "reader"
    SNAPSHOT = "snapshot"


class EbsVerdict(str, Enum):
    KEEP_IO2 = "keep_io2"
    MIGRATE_GP3 = "migrate_gp3"
    ALREADY_GP3 = "already_gp3"


class RunwayAlert(str, Enum):
    OK = "ok"
    EXPAND = "expand"           # occupancy > 75% within runway
    DOWNSIZE = "downsize"       # free_space > 300 GB — candidate for smaller volume


class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class VetoReason(str, Enum):
    PER_CORE_SATURATION = "per_core_saturation"
    CONTEXT_SWITCH_OVERLOAD = "context_switch_overload"
    RUNQUEUE_SATURATION = "runqueue_saturation"
    REPLICATION_LAG = "replication_lag"
    OOM_RISK = "oom_risk"
    THREAD_OVERCOMMIT = "thread_overcommit"


# ---------------------------------------------------------------------------
# Node metadata
# ---------------------------------------------------------------------------

@dataclass
class NodeConfig:
    """A single MariaDB node as declared in config.yaml."""
    label: str
    pmm_node_name: str
    service_name: str
    aws_instance_id: str
    cluster: str
    role: NodeRole


# ---------------------------------------------------------------------------
# AWS metadata
# ---------------------------------------------------------------------------

@dataclass
class Ec2Info:
    """EC2 instance metadata retrieved via boto3."""
    instance_id: str
    instance_type: str
    vcpus: int
    physical_cores: int          # vcpus / 2 for Intel SMT
    memory_gb: float
    name_tag: str = ""
    cluster_tag: str = ""


@dataclass
class EbsVolumeInfo:
    """EBS volume metadata retrieved via boto3."""
    volume_id: str
    device: str
    volume_type: str             # io2, gp3, gp2, etc.
    size_gb: int
    provisioned_iops: int
    provisioned_throughput_mbps: int = 0


# ---------------------------------------------------------------------------
# Time-series helpers
# ---------------------------------------------------------------------------

@dataclass
class TimeSeriesPoint:
    """A single (timestamp, value) from Prometheus."""
    timestamp: float
    value: float


@dataclass
class MetricSummary:
    """Statistical summary of a Prometheus time-series."""
    avg: float = 0.0
    p50: float = 0.0
    p95: float = 0.0
    p99: float = 0.0
    max: float = 0.0
    min: float = 0.0
    samples: int = 0
    raw: list[TimeSeriesPoint] = field(default_factory=list)


# ---------------------------------------------------------------------------
# CPU Analysis
# ---------------------------------------------------------------------------

@dataclass
class PerCoreUsage:
    """Usage statistics for a single logical CPU (vCPU)."""
    cpu_id: str
    usage_pct: MetricSummary     # rate(node_cpu_seconds_total{mode!="idle"}) * 100


@dataclass
class CpuAnalysisResult:
    """Full CPU analysis for a node."""
    # Aggregate
    aggregate_avg_pct: float = 0.0
    aggregate_p95_pct: float = 0.0
    aggregate_max_pct: float = 0.0

    # Per-core
    per_core: list[PerCoreUsage] = field(default_factory=list)
    hottest_core_p95_pct: float = 0.0
    hottest_core_max_pct: float = 0.0
    skewness_coefficient: float = 0.0  # scipy.stats.skew

    # Context switches
    context_switches_per_sec: MetricSummary = field(default_factory=MetricSummary)

    # Run queue
    procs_running: MetricSummary = field(default_factory=MetricSummary)

    # MariaDB fixed threads
    innodb_read_io_threads: int = 0
    innodb_write_io_threads: int = 0
    innodb_page_cleaners: int = 0
    innodb_purge_threads: int = 0
    slave_parallel_workers: int = 0
    total_background_threads: int = 0
    threads_running: MetricSummary = field(default_factory=MetricSummary)

    # Projected (downsized)
    projected_aggregate_max_pct: float = 0.0
    projected_hottest_core_pct: float = 0.0

    # Vetoes
    vetoes: list[VetoReason] = field(default_factory=list)
    veto_details: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Memory Analysis
# ---------------------------------------------------------------------------

@dataclass
class MemoryAnalysisResult:
    """Full memory analysis for a node."""
    # Instance
    total_ram_gb: float = 0.0

    # mysqld RSS
    mysqld_rss_gb: MetricSummary = field(default_factory=MetricSummary)

    # Buffer Pool
    buffer_pool_configured_gb: float = 0.0
    buffer_pool_used_pct: float = 0.0
    buffer_pool_hit_ratio_pct: float = 0.0
    buffer_pool_dirty_pct: float = 0.0
    buffer_pool_wait_free: MetricSummary = field(default_factory=MetricSummary)
    lru_pages_freed_per_sec: MetricSummary = field(default_factory=MetricSummary)

    # Session overhead
    max_connections: int = 0
    max_used_connections: int = 0
    per_session_overhead_mb: float = 0.0
    session_ceiling_gb: float = 0.0   # max_used_connections * per_session_overhead

    # OS-level
    mem_available_gb: MetricSummary = field(default_factory=MetricSummary)
    swap_used_bytes: MetricSummary = field(default_factory=MetricSummary)
    swap_activity: bool = False

    # OOM risk
    oom_risk_score: float = 0.0       # 0..100, projected against the actual downsize candidate
    effective_free_pct: float = 0.0   # after accounting for session overhead
    projected_rss_after_downsize_gb: float = 0.0  # re-tuned buffer pool + fixed overhead
    cache_pressure_warning: bool = False          # advisory only, never blocks downsizing

    # Vetoes
    vetoes: list[VetoReason] = field(default_factory=list)
    veto_details: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Disk / EBS Analysis
# ---------------------------------------------------------------------------

@dataclass
class DiskAnalysisResult:
    """Full disk I/O and latency analysis for a node's data volume."""
    volume_id: str = ""
    device: str = ""
    volume_type: str = ""
    size_gb: int = 0
    provisioned_iops: int = 0

    # IOPS
    read_iops: MetricSummary = field(default_factory=MetricSummary)
    write_iops: MetricSummary = field(default_factory=MetricSummary)
    total_iops_peak: float = 0.0
    total_iops_p95: float = 0.0

    # Latency (milliseconds)
    read_latency_ms: MetricSummary = field(default_factory=MetricSummary)
    write_latency_ms: MetricSummary = field(default_factory=MetricSummary)

    # Throughput (MB/s)
    read_throughput_mbps: MetricSummary = field(default_factory=MetricSummary)
    write_throughput_mbps: MetricSummary = field(default_factory=MetricSummary)

    # InnoDB fsync
    innodb_data_fsyncs_per_sec: MetricSummary = field(default_factory=MetricSummary)
    innodb_os_log_fsyncs_per_sec: MetricSummary = field(default_factory=MetricSummary)
    innodb_pending_fsyncs: MetricSummary = field(default_factory=MetricSummary)
    innodb_flush_log_at_trx_commit: int = 1

    # Verdict
    verdict: EbsVerdict = EbsVerdict.ALREADY_GP3
    recommended_gp3_iops: int = 3000
    recommended_gp3_throughput_mbps: int = 125
    monthly_savings_usd: float = 0.0


# ---------------------------------------------------------------------------
# Replication Analysis
# ---------------------------------------------------------------------------

@dataclass
class ReplicationAnalysisResult:
    """Replication health analysis for a replica node."""
    is_replica: bool = False

    # SBM (Seconds Behind Master)
    sbm: MetricSummary = field(default_factory=MetricSummary)
    sbm_p99: float = 0.0

    # Micro-lag episodes: SBM > threshold for > 30s
    micro_lag_episodes: int = 0
    micro_lag_max_duration_sec: float = 0.0

    # Correlation
    lag_correlated_with_writer_io: bool = False

    # Vetoes
    vetoes: list[VetoReason] = field(default_factory=list)
    veto_details: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Disk Runway (Growth Projection)
# ---------------------------------------------------------------------------

@dataclass
class RunwayResult:
    """6-month disk growth projection for a data volume."""
    current_used_gb: float = 0.0
    current_total_gb: float = 0.0
    current_free_gb: float = 0.0
    current_occupancy_pct: float = 0.0

    # Linear regression
    daily_growth_gb: float = 0.0
    monthly_growth_gb: float = 0.0
    projected_used_6m_gb: float = 0.0          # +10% safety factor applied
    projected_occupancy_6m_pct: float = 0.0

    # Days remaining
    days_to_75_pct: Optional[float] = None
    days_to_85_pct: Optional[float] = None
    days_to_95_pct: Optional[float] = None

    # Alert
    alert: RunwayAlert = RunwayAlert.OK
    alert_detail: str = ""


# ---------------------------------------------------------------------------
# Rightsizing Recommendation (final output per node)
# ---------------------------------------------------------------------------

@dataclass
class RightsizingRecommendation:
    """Final recommendation for a single node."""
    # Identity
    node_label: str = ""
    cluster: str = ""
    role: NodeRole = NodeRole.READER
    aws_instance_id: str = ""

    # Current
    current_ec2_type: str = ""
    current_vcpus: int = 0
    current_memory_gb: float = 0.0

    # Recommended
    recommended_ec2_type: str = ""
    recommended_vcpus: int = 0
    recommended_memory_gb: float = 0.0

    # Analysis results (attached for reporting)
    cpu: Optional[CpuAnalysisResult] = None
    memory: Optional[MemoryAnalysisResult] = None
    disk: Optional[DiskAnalysisResult] = None
    replication: Optional[ReplicationAnalysisResult] = None
    runway: Optional[RunwayResult] = None

    # All vetoes aggregated
    all_vetoes: list[VetoReason] = field(default_factory=list)
    all_veto_details: dict[str, str] = field(default_factory=dict)

    # Confidence
    confidence: Confidence = Confidence.MEDIUM

    # Savings
    ec2_monthly_savings_usd: float = 0.0
    ebs_monthly_savings_usd: float = 0.0
    total_monthly_savings_usd: float = 0.0
    total_annual_savings_usd: float = 0.0

    # Status
    status: str = "OK"   # OK | ERROR | VETOED
    error_msg: str = ""

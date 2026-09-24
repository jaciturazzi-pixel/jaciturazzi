"""
mcplanner.config — Configuration loader, validator, and typed accessor.

Loads config.yaml via Pydantic models with strict validation, providing
a single Config object consumed by all modules.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Pydantic sub-models (matching config.yaml structure)
# ---------------------------------------------------------------------------

class PmmConfig(BaseModel):
    """PMM / Prometheus connection settings."""
    base_url: str = "https://pmm-server.us-west-2.pool.minicliptech.com"
    auth_method: str = "basic"   # basic | token
    username: str = "admin"
    password: str = ""
    token: Optional[str] = None
    verify_ssl: bool = True
    timeout_seconds: int = 30

    @field_validator("auth_method")
    @classmethod
    def _validate_auth_method(cls, v: str) -> str:
        allowed = {"basic", "token"}
        if v not in allowed:
            raise ValueError(f"auth_method must be one of {allowed}, got '{v}'")
        return v

    @property
    def prometheus_url(self) -> str:
        """Full URL to the Prometheus API inside PMM."""
        base = self.base_url.rstrip("/")
        return f"{base}/prometheus/api/v1"


class AwsConfig(BaseModel):
    """AWS session settings."""
    profile: str = "miniclippool-databases"
    region: str = "us-west-2"


class NodeEntry(BaseModel):
    """A single MariaDB node."""
    label: str
    pmm_node_name: str = ""
    service_name: str = ""
    aws_instance_id: str = ""
    cluster: str = ""
    role: str = "reader"   # writer | reader | snapshot

    @field_validator("role")
    @classmethod
    def _validate_role(cls, v: str) -> str:
        allowed = {"writer", "reader", "snapshot"}
        if v.lower() not in allowed:
            raise ValueError(f"role must be one of {allowed}, got '{v}'")
        return v.lower()

    def model_post_init(self, __context: object) -> None:
        # Default pmm_node_name and service_name from label if not set
        if not self.pmm_node_name:
            self.pmm_node_name = self.label
        if not self.service_name:
            self.service_name = f"{self.label}-mysql"


class CpuThresholds(BaseModel):
    per_core_saturation_pct: float = 75.0
    projected_max_after_downsize: float = 50.0
    context_switches_per_sec_warn: int = 50000
    skewness_warn: float = 1.5


class MemoryThresholds(BaseModel):
    min_free_pct_after_session_overhead: float = 15.0
    buffer_pool_hit_ratio_min: float = 99.5
    lru_freed_per_sec_warn: int = 1000
    oom_risk_score_veto: float = 80.0


class ReplicationThresholds(BaseModel):
    sbm_micro_lag_threshold_sec: float = 5.0
    sbm_veto_percentile: int = 99
    micro_lag_min_duration_sec: float = 30.0


class DiskThresholds(BaseModel):
    write_latency_p99_ms_io2_threshold: float = 2.0
    gp3_max_iops: int = 16000
    gp3_max_throughput_mbps: int = 1000
    iops_safety_factor: float = 1.4
    iops_floor: int = 3000


class RunwayThresholds(BaseModel):
    months: int = 6
    growth_factor: float = 1.10
    occupancy_alert_pct: float = 75.0
    occupancy_critical_pct: float = 85.0
    free_space_downsize_gb: float = 300.0


class DiskConfig(BaseModel):
    """Disk-related settings (device, mountpoint)."""
    data_device: str = "nvme4n1"
    data_mountpoint: str = "/mnt/storage"


class Ec2HourlyPricing(BaseModel):
    """On-Demand hourly pricing for Intel Xeon r8i / m8i instances."""
    prices: dict[str, float] = Field(default_factory=lambda: {
        "r8i.16xlarge": 4.170,
        "r8i.8xlarge": 2.085,
        "r8i.4xlarge": 1.0425,
        "r8i.2xlarge": 0.52125,
        "r8i.xlarge": 0.26063,
        "m8i.4xlarge": 0.768,
        "m8i.2xlarge": 0.384,
        "m8i.xlarge": 0.192,
    })


class ThresholdsConfig(BaseModel):
    cpu: CpuThresholds = Field(default_factory=CpuThresholds)
    memory: MemoryThresholds = Field(default_factory=MemoryThresholds)
    replication: ReplicationThresholds = Field(default_factory=ReplicationThresholds)
    disk: DiskThresholds = Field(default_factory=DiskThresholds)
    runway: RunwayThresholds = Field(default_factory=RunwayThresholds)


class EbsPricing(BaseModel):
    """EBS cost models for io2 and gp3."""
    io2_per_gb: float = 0.125
    io2_per_iops: float = 0.065
    gp3_per_gb: float = 0.08
    gp3_base_iops: int = 3000
    gp3_per_extra_iops: float = 0.005
    gp3_base_throughput_mbps: int = 125
    gp3_per_extra_throughput_mbps: float = 0.04


class PricingConfig(BaseModel):
    ec2_hourly: Ec2HourlyPricing = Field(default_factory=Ec2HourlyPricing)
    ebs: EbsPricing = Field(default_factory=EbsPricing)
    hours_per_month: int = 730


# ---------------------------------------------------------------------------
# Top-level Config
# ---------------------------------------------------------------------------

class Config(BaseModel):
    """Top-level configuration for mcplanner."""
    pmm: PmmConfig = Field(default_factory=PmmConfig)
    aws: AwsConfig = Field(default_factory=AwsConfig)
    nodes: list[NodeEntry] = Field(default_factory=list)
    disk: DiskConfig = Field(default_factory=DiskConfig)
    thresholds: ThresholdsConfig = Field(default_factory=ThresholdsConfig)
    pricing: PricingConfig = Field(default_factory=PricingConfig)


# ---------------------------------------------------------------------------
# EC2 instance specs lookup (vCPUs, physical cores, RAM)
# ---------------------------------------------------------------------------

# Intel Xeon families: r8i (memory-opt) and m8i (general-purpose)
# vCPUs = 2 * physical cores (SMT / Hyperthreading)
EC2_INSTANCE_SPECS: dict[str, dict] = {
    # r8i family (Memory Optimized — 8 GiB/vCPU)
    "r8i.xlarge":    {"vcpus": 4,  "cores": 2,  "memory_gb": 32},
    "r8i.2xlarge":   {"vcpus": 8,  "cores": 4,  "memory_gb": 64},
    "r8i.4xlarge":   {"vcpus": 16, "cores": 8,  "memory_gb": 128},
    "r8i.8xlarge":   {"vcpus": 32, "cores": 16, "memory_gb": 256},
    "r8i.16xlarge":  {"vcpus": 64, "cores": 32, "memory_gb": 512},
    # m8i family (General Purpose — 4 GiB/vCPU)
    "m8i.xlarge":    {"vcpus": 4,  "cores": 2,  "memory_gb": 16},
    "m8i.2xlarge":   {"vcpus": 8,  "cores": 4,  "memory_gb": 32},
    "m8i.4xlarge":   {"vcpus": 16, "cores": 8,  "memory_gb": 64},
    "m8i.8xlarge":   {"vcpus": 32, "cores": 16, "memory_gb": 128},
    # Legacy families (for current inventory)
    "r6i.8xlarge":   {"vcpus": 32, "cores": 16, "memory_gb": 256},
    "r6i.16xlarge":  {"vcpus": 64, "cores": 32, "memory_gb": 512},
    "r6in.8xlarge":  {"vcpus": 32, "cores": 16, "memory_gb": 256},
    "r6in.16xlarge": {"vcpus": 64, "cores": 32, "memory_gb": 512},
}

# Intel Xeon downsizing ladder (only within same family)
DOWNSIZE_MAP: dict[str, str] = {
    "r8i.16xlarge": "r8i.8xlarge",
    "r8i.8xlarge":  "r8i.4xlarge",
    "r8i.4xlarge":  "r8i.2xlarge",
    "r8i.2xlarge":  "r8i.xlarge",
    "m8i.8xlarge":  "m8i.4xlarge",
    "m8i.4xlarge":  "m8i.2xlarge",
    "m8i.2xlarge":  "m8i.xlarge",
    # Legacy → Intel 8th gen mapping (same or next smaller size)
    "r6i.16xlarge":  "r8i.8xlarge",
    "r6i.8xlarge":   "r8i.4xlarge",
    "r6in.16xlarge": "r8i.8xlarge",
    "r6in.8xlarge":  "r8i.4xlarge",
}

# Same-gen Intel 8th mapping (for gen upgrade without downsizing)
GEN_UPGRADE_MAP: dict[str, str] = {
    "r6i.16xlarge":  "r8i.16xlarge",
    "r6i.8xlarge":   "r8i.8xlarge",
    "r6in.16xlarge": "r8i.16xlarge",
    "r6in.8xlarge":  "r8i.8xlarge",
}


# ---------------------------------------------------------------------------
# Loader function
# ---------------------------------------------------------------------------

def load_config(path: str | Path) -> Config:
    """Load and validate a config.yaml file, returning a typed Config object.

    Environment variable substitution is supported for sensitive values:
      - ``${PMM_PASSWORD}`` in the YAML will be replaced by the env var.
    """
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"Config file not found: {path}")

    raw_text = path.read_text(encoding="utf-8")

    # Simple env-var substitution: ${VAR_NAME}
    import re
    def _env_replace(m: re.Match) -> str:
        var = m.group(1)
        return os.environ.get(var, m.group(0))  # keep original if not set

    raw_text = re.sub(r"\$\{(\w+)\}", _env_replace, raw_text)

    data = yaml.safe_load(raw_text) or {}
    return Config(**data)

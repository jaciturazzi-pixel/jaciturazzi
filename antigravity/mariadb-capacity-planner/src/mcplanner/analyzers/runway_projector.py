from __future__ import annotations

import numpy as np
from datetime import datetime

from mcplanner.config import DiskConfig, NodeEntry, RunwayThresholds
from mcplanner.models import RunwayAlert, RunwayResult
from mcplanner.collectors.pmm_client import PmmClient


class RunwayProjector:
    def __init__(
        self,
        pmm_client: PmmClient,
        node_entry: NodeEntry,
        disk_config: DiskConfig,
        thresholds: RunwayThresholds,
        current_total_gb: float = 0.0,
        total_volume_gb: float = 0.0,
    ):
        self.pmm_client = pmm_client
        self.node = node_entry
        self.disk_config = disk_config
        self.thresholds = thresholds
        self.current_total_gb = total_volume_gb or current_total_gb

    def analyze(self, start: datetime, end: datetime) -> RunwayResult:
        node_name = self.node.pmm_node_name
        mountpoint = self.disk_config.data_mountpoint
        
        # 1. Current filesystem usage
        total_query = f'node_filesystem_size_bytes{{node_name="{node_name}", mountpoint="{mountpoint}"}}'
        free_query = f'node_filesystem_free_bytes{{node_name="{node_name}", mountpoint="{mountpoint}"}}'
        
        total_bytes_val = self.pmm_client.scalar_query(total_query, end)
        free_bytes_val = self.pmm_client.scalar_query(free_query, end)
        
        if total_bytes_val is None or free_bytes_val is None:
            # Cannot determine
            return RunwayResult(current_total_gb=self.current_total_gb)
            
        current_used_gb = (total_bytes_val - free_bytes_val) / (1024**3)
        current_free_gb = free_bytes_val / (1024**3)
        current_occupancy_pct = (current_used_gb / self.current_total_gb) * 100 if self.current_total_gb else 0.0
        
        # 2. Time series for regression
        free_bytes_series = self.pmm_client.range_query(free_query, start, end)
        
        daily_growth_gb = 0.0
        monthly_growth_gb = 0.0
        
        if len(free_bytes_series) > 1:
            # 3. Linear regression
            x = [pt.timestamp for pt in free_bytes_series]
            y = [pt.value for pt in free_bytes_series]
            
            slope, intercept = np.polyfit(x, y, 1)
            # slope is change in free bytes per second. Negative means growing usage.
            # growth bytes per second is -slope
            growth_bps = -slope
            
            daily_growth_gb = (growth_bps * 86400) / (1024**3)
            monthly_growth_gb = daily_growth_gb * 30.44
        
        # 4. 6-month projection
        runway_months = self.thresholds.months
        growth_factor = self.thresholds.growth_factor
        
        projected_used_6m_gb = current_used_gb + (monthly_growth_gb * runway_months * growth_factor)
        projected_occupancy_6m_pct = (projected_used_6m_gb / self.current_total_gb * 100) if self.current_total_gb else 0.0
        
        # 5. Days remaining
        days_to_75_pct = None
        days_to_85_pct = None
        days_to_95_pct = None
        
        if daily_growth_gb > 0:
            bytes_to_target = lambda pct: max(0, ((pct / 100.0) * self.current_total_gb) - current_used_gb) * (1024**3)
            daily_growth_bytes = daily_growth_gb * (1024**3)
            
            days_to_75_pct = bytes_to_target(75.0) / daily_growth_bytes
            days_to_85_pct = bytes_to_target(85.0) / daily_growth_bytes
            days_to_95_pct = bytes_to_target(95.0) / daily_growth_bytes
            
        # 6. Alert logic
        alert = RunwayAlert.OK
        alert_detail = ""
        
        if projected_occupancy_6m_pct > self.thresholds.occupancy_alert_pct:
            alert = RunwayAlert.EXPAND
            alert_detail = "Schedule AWS Online Volume Expansion"
        elif current_free_gb > self.thresholds.free_space_downsize_gb and projected_occupancy_6m_pct < 50.0:
            alert = RunwayAlert.DOWNSIZE
            alert_detail = "Candidate for volume downsizing via migration"
            
        return RunwayResult(
            current_used_gb=current_used_gb,
            current_total_gb=self.current_total_gb,
            current_free_gb=current_free_gb,
            current_occupancy_pct=current_occupancy_pct,
            daily_growth_gb=daily_growth_gb,
            monthly_growth_gb=monthly_growth_gb,
            projected_used_6m_gb=projected_used_6m_gb,
            projected_occupancy_6m_pct=projected_occupancy_6m_pct,
            days_to_75_pct=days_to_75_pct,
            days_to_85_pct=days_to_85_pct,
            days_to_95_pct=days_to_95_pct,
            alert=alert,
            alert_detail=alert_detail
        )

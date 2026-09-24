from __future__ import annotations

from datetime import datetime

from mcplanner.config import NodeEntry, ThresholdsConfig
from mcplanner.models import ReplicationAnalysisResult, VetoReason
from mcplanner.collectors.pmm_client import PmmClient


class ReplicationAnalyzer:
    def __init__(
        self,
        pmm_client: PmmClient,
        node_entry: NodeEntry,
        thresholds: ThresholdsConfig,
    ):
        self.pmm_client = pmm_client
        self.node = node_entry
        self.thresholds = thresholds

    def analyze(self, start: datetime, end: datetime) -> ReplicationAnalysisResult:
        # 1. Check if replica
        if self.node.role == "writer":
            return ReplicationAnalysisResult(is_replica=False)
            
        svc = self.node.service_name
        
        # 2. SBM time series
        sbm_query = f'mysql_slave_status_seconds_behind_master{{service_name="{svc}"}}'
        sbm_summary = self.pmm_client.get_metric_summary(sbm_query, start, end)
        
        # 3. Micro-lag episode detection
        sbm_raw = self.pmm_client.range_query(sbm_query, start, end)
        
        micro_lag_threshold = self.thresholds.replication.sbm_micro_lag_threshold_sec
        min_duration = self.thresholds.replication.micro_lag_min_duration_sec
        
        episodes = 0
        max_duration = 0.0
        
        current_episode_start = None
        
        for pt in sbm_raw:
            if pt.value > micro_lag_threshold:
                if current_episode_start is None:
                    current_episode_start = pt.timestamp
            else:
                if current_episode_start is not None:
                    duration = pt.timestamp - current_episode_start
                    if duration >= min_duration:
                        episodes += 1
                        if duration > max_duration:
                            max_duration = duration
                    current_episode_start = None
        
        # If we end while still in an episode
        if current_episode_start is not None:
            duration = end.timestamp() - current_episode_start
            if duration >= min_duration:
                episodes += 1
                if duration > max_duration:
                    max_duration = duration
                    
        # 4. SBM P99
        sbm_p99 = sbm_summary.p99
        
        # 5. Veto logic
        vetoes = []
        veto_details = {}
        
        if sbm_p99 > micro_lag_threshold or episodes > 3:
            vetoes.append(VetoReason.REPLICATION_LAG)
            veto_details[VetoReason.REPLICATION_LAG.value] = (
                f"SBM P99 is {sbm_p99:.1f}s, and found {episodes} micro-lag episodes. "
                "Downsizing may worsen replication lag."
            )
            
        return ReplicationAnalysisResult(
            is_replica=True,
            sbm=sbm_summary,
            sbm_p99=sbm_p99,
            micro_lag_episodes=episodes,
            micro_lag_max_duration_sec=max_duration,
            lag_correlated_with_writer_io=False,
            vetoes=vetoes,
            veto_details=veto_details
        )

from __future__ import annotations

import datetime

from mcplanner.models import (
    Ec2Info, MemoryAnalysisResult, MetricSummary, VetoReason
)
from mcplanner.config import NodeEntry, ThresholdsConfig, DOWNSIZE_MAP, EC2_INSTANCE_SPECS
from mcplanner.collectors.pmm_client import PmmClient

class MemoryAnalyzer:
    def __init__(self, pmm_client: PmmClient, node: NodeEntry, ec2_info: Ec2Info, thresholds: ThresholdsConfig):
        self.pmm_client = pmm_client
        self.node = node
        self.ec2_info = ec2_info
        self.thresholds = thresholds

    def analyze(self, start: datetime.datetime, end: datetime.datetime) -> MemoryAnalysisResult:
        svc = self.node.service_name
        nn = self.node.pmm_node_name

        def bytes_to_gb(ms: MetricSummary) -> MetricSummary:
            return MetricSummary(
                avg=ms.avg / 1073741824,
                p50=ms.p50 / 1073741824,
                p95=ms.p95 / 1073741824,
                p99=ms.p99 / 1073741824,
                max=ms.max / 1073741824,
                min=ms.min / 1073741824,
                samples=ms.samples,
                raw=ms.raw
            )

        # 1. mysqld memory usage via MariaDB's own status variable.
        #    process_resident_memory_bytes{service_name=...} returns the
        #    *exporter* process RSS (~20 MB), NOT the mysqld process.
        #    mysql_global_status_memory_used is MariaDB 10.1+ internal tracking.
        q_mem_used = f'mysql_global_status_memory_used{{service_name="{svc}"}}'
        rss_summary_bytes = self.pmm_client.get_metric_summary(q_mem_used, start, end)

        # Fallback: if mysql_global_status_memory_used is empty, estimate from
        # MemTotal - MemAvailable (OS-level, includes mysqld + caches).
        if rss_summary_bytes.samples == 0:
            q_used_os = f'node_memory_MemTotal_bytes{{node_name="{nn}"}} - node_memory_MemAvailable_bytes{{node_name="{nn}"}}'
            rss_summary_bytes = self.pmm_client.get_metric_summary(q_used_os, start, end)

        rss_summary = bytes_to_gb(rss_summary_bytes)

        # 2. Buffer Pool configured size
        q_bp_conf = f'mysql_global_variables_innodb_buffer_pool_size{{service_name="{svc}"}}'
        bp_conf_bytes = float(self.pmm_client.scalar_query(q_bp_conf) or 0)
        bp_conf_gb = bp_conf_bytes / 1073741824

        # 3. Buffer Pool used% — PMM/MariaDB uses the multi-state metric:
        #    mysql_global_status_buffer_pool_pages{state="data"|"total"}
        #    NOT mysql_global_status_innodb_buffer_pool_pages_data/total.
        q_bp_data = f'mysql_global_status_buffer_pool_pages{{service_name="{svc}", state="data"}}'
        q_bp_total = f'mysql_global_status_buffer_pool_pages{{service_name="{svc}", state="total"}}'
        bp_pages_data = float(self.pmm_client.scalar_query(q_bp_data) or 0)
        bp_pages_total = float(self.pmm_client.scalar_query(q_bp_total) or 0)
        bp_used_pct = (bp_pages_data / bp_pages_total * 100) if bp_pages_total > 0 else 0.0

        # 4. Buffer Pool hit ratio
        q_bp_hit = (
            f'(1 - (rate(mysql_global_status_innodb_buffer_pool_reads{{service_name="{svc}"}}[5m])'
            f' / rate(mysql_global_status_innodb_buffer_pool_read_requests{{service_name="{svc}"}}[5m])))'
            f' * 100'
        )
        bp_hit_summary = self.pmm_client.get_metric_summary(q_bp_hit, start, end)

        # 5. Buffer Pool dirty — use dedicated metric
        q_bp_dirty_pages = f'mysql_global_status_buffer_pool_dirty_pages{{service_name="{svc}"}}'
        bp_dirty_pages = float(self.pmm_client.scalar_query(q_bp_dirty_pages) or 0)
        # dirty% relative to total pages
        bp_dirty_pct = (bp_dirty_pages / bp_pages_total * 100) if bp_pages_total > 0 else 0.0

        # 6. Wait free pages
        q_wait_free = f'mysql_global_status_innodb_buffer_pool_wait_free{{service_name="{svc}"}}'
        wait_free_summary = self.pmm_client.get_metric_summary(q_wait_free, start, end)

        # 7. LRU pages freed/s — try the standard metric, fallback to page_changes
        q_lru = f'rate(mysql_global_status_buffer_pool_page_changes_total{{service_name="{svc}", type="lru_freed"}}[5m])'
        lru_summary = self.pmm_client.get_metric_summary(q_lru, start, end)

        # 8. Session overhead
        q_sort = f'mysql_global_variables_sort_buffer_size{{service_name="{svc}"}}'
        q_read_buf = f'mysql_global_variables_read_buffer_size{{service_name="{svc}"}}'
        q_join = f'mysql_global_variables_join_buffer_size{{service_name="{svc}"}}'
        q_stack = f'mysql_global_variables_thread_stack{{service_name="{svc}"}}'

        sort_b = float(self.pmm_client.scalar_query(q_sort) or 0)
        read_b = float(self.pmm_client.scalar_query(q_read_buf) or 0)
        join_b = float(self.pmm_client.scalar_query(q_join) or 0)
        stack_b = float(self.pmm_client.scalar_query(q_stack) or 0)

        per_session_overhead = sort_b + read_b + join_b + stack_b

        q_max_conn = f'mysql_global_variables_max_connections{{service_name="{svc}"}}'
        q_max_used = f'mysql_global_status_max_used_connections{{service_name="{svc}"}}'
        max_conn = int(self.pmm_client.scalar_query(q_max_conn) or 0)
        max_used = int(self.pmm_client.scalar_query(q_max_used) or 0)

        session_ceiling_gb = (max_used * per_session_overhead) / 1073741824
        per_session_overhead_mb = per_session_overhead / 1048576

        # 9. OS memory
        q_mem_avail = f'node_memory_MemAvailable_bytes{{node_name="{nn}"}}'
        mem_avail_summary = bytes_to_gb(self.pmm_client.get_metric_summary(q_mem_avail, start, end))

        q_swap_free = f'node_memory_SwapFree_bytes{{node_name="{nn}"}}'
        q_swap_total = f'node_memory_SwapTotal_bytes{{node_name="{nn}"}}'

        swap_free = float(self.pmm_client.scalar_query(q_swap_free) or 0)
        swap_total = float(self.pmm_client.scalar_query(q_swap_total) or 0)
        swap_used_bytes = max(0, swap_total - swap_free)
        # Only flag swap activity if swap is configured AND usage > 100 MB threshold
        # to avoid false positives from negligible kernel swap (~10 MB).
        swap_activity_threshold = 100 * 1024 * 1024  # 100 MB
        swap_activity = swap_total > 0 and swap_used_bytes > swap_activity_threshold

        q_swap_used = f'node_memory_SwapTotal_bytes{{node_name="{nn}"}} - node_memory_SwapFree_bytes{{node_name="{nn}"}}'
        swap_used_summary = self.pmm_client.get_metric_summary(q_swap_used, start, end)

        # 10. OOM Risk Score — projected for the real downsize candidate, re-tuning the
        # buffer pool to the same RAM ratio instead of assuming it stays fixed at its current
        # absolute size. A well-tuned DB runs buffer_pool ~ 70-90% of RAM by design; keeping
        # that ratio while shrinking RAM is what actually happens on a resize, so reusing the
        # current RSS as-is against a smaller box makes every well-tuned instance unvetoable.
        current_ram_gb = self.ec2_info.memory_gb
        candidate_type = DOWNSIZE_MAP.get(self.ec2_info.instance_type)
        candidate_specs = EC2_INSTANCE_SPECS.get(candidate_type, {}) if candidate_type else {}
        target_ram_gb = candidate_specs.get("memory_gb") or (current_ram_gb / 2 if current_ram_gb else 0.0)

        bp_ratio = (bp_conf_gb / current_ram_gb) if current_ram_gb > 0 else 0.0
        projected_bp_gb = bp_ratio * target_ram_gb

        rss_peak_gb = rss_summary.max
        # Everything mysqld uses beyond the buffer pool itself (connections, temp tables,
        # parsing, replication buffers) — this is the real fixed cost, not the whole RSS.
        non_bp_overhead_gb = max(0.0, rss_peak_gb - bp_conf_gb)
        projected_rss_gb = projected_bp_gb + non_bp_overhead_gb

        oom_risk = ((projected_rss_gb + session_ceiling_gb) / target_ram_gb) * 100 if target_ram_gb > 0 else 0.0

        # Cache pressure is a performance trade-off (more disk reads/CPU), not a memory-safety
        # issue — InnoDB caps itself at the configured buffer pool size, it can't OOM on its
        # own. Surface it as a non-blocking advisory instead of a hard veto.
        cache_pressure_warning = bool(
            bp_hit_summary.avg >= self.thresholds.memory.buffer_pool_hit_ratio_min
            and bp_conf_gb > 0
            and projected_bp_gb < bp_conf_gb * 0.85
        )

        # 11. Effective free%
        effective_free_pct = ((target_ram_gb - projected_rss_gb - session_ceiling_gb) / target_ram_gb) * 100 if target_ram_gb > 0 else 0.0

        # 12. Vetoes
        vetoes = []
        veto_details = {}

        if oom_risk > self.thresholds.memory.oom_risk_score_veto:
            vetoes.append(VetoReason.OOM_RISK)
            veto_details[VetoReason.OOM_RISK.value] = (
                f"OOM Risk Score is {oom_risk:.1f}% > {self.thresholds.memory.oom_risk_score_veto}% "
                f"(buffer pool re-tuned to {projected_bp_gb:.1f}G for a {target_ram_gb:.0f}G target)"
            )

        if swap_activity:
            swap_used_mb = swap_used_bytes / (1024 * 1024)
            veto_details["swap_activity"] = f"Warning: Swap activity detected ({swap_used_mb:.0f} MB used)"

        if cache_pressure_warning:
            veto_details["cache_pressure_advisory"] = (
                f"Buffer pool would shrink from {bp_conf_gb:.1f}G to {projected_bp_gb:.1f}G — expect lower hit "
                f"ratio (currently {bp_hit_summary.avg:.2f}%) and higher disk IOPS/CPU after downsize"
            )

        return MemoryAnalysisResult(
            total_ram_gb=self.ec2_info.memory_gb,
            mysqld_rss_gb=rss_summary,
            buffer_pool_configured_gb=bp_conf_gb,
            buffer_pool_used_pct=bp_used_pct,
            buffer_pool_hit_ratio_pct=bp_hit_summary.avg,
            buffer_pool_dirty_pct=bp_dirty_pct,
            buffer_pool_wait_free=wait_free_summary,
            lru_pages_freed_per_sec=lru_summary,
            max_connections=max_conn,
            max_used_connections=max_used,
            per_session_overhead_mb=per_session_overhead_mb,
            session_ceiling_gb=session_ceiling_gb,
            mem_available_gb=mem_avail_summary,
            swap_used_bytes=swap_used_summary,
            swap_activity=swap_activity,
            oom_risk_score=oom_risk,
            effective_free_pct=effective_free_pct,
            projected_rss_after_downsize_gb=projected_rss_gb,
            cache_pressure_warning=cache_pressure_warning,
            vetoes=vetoes,
            veto_details=veto_details
        )

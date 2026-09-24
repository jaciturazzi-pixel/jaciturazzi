from __future__ import annotations

import datetime
import numpy as np

from mcplanner.models import (
    Ec2Info, CpuAnalysisResult, PerCoreUsage, VetoReason, MetricSummary
)
from mcplanner.config import NodeEntry, ThresholdsConfig
from mcplanner.collectors.pmm_client import PmmClient

class CpuAnalyzer:
    def __init__(self, pmm_client: PmmClient, node: NodeEntry, ec2_info: Ec2Info, thresholds: ThresholdsConfig):
        self.pmm_client = pmm_client
        self.node = node
        self.ec2_info = ec2_info
        self.thresholds = thresholds

    def analyze(self, start: datetime.datetime, end: datetime.datetime) -> CpuAnalysisResult:
        # 1. Per-core CPU usage
        query_cpu = f'rate(node_cpu_seconds_total{{node_name="{self.node.pmm_node_name}", mode!="idle"}}[5m]) * 100'
        per_core_data = self.pmm_client.range_query_per_label(query_cpu, start, end, label='cpu')
        
        # 2. Hottest core
        hottest_core_p95 = 0.0
        hottest_core_max = 0.0
        
        per_cores = []
        p95_values = []
        aggregate_sum_avg = 0.0
        aggregate_sum_max = 0.0
        aggregate_sum_p95 = 0.0
        
        for cpu_id, points in per_core_data.items():
            if not points:
                continue
            vals = np.array([p.value for p in points])
            if len(vals) == 0:
                continue
            summary = MetricSummary(
                avg=float(np.mean(vals)),
                p50=float(np.percentile(vals, 50)),
                p95=float(np.percentile(vals, 95)),
                p99=float(np.percentile(vals, 99)),
                max=float(np.max(vals)),
                min=float(np.min(vals)),
                samples=len(vals),
                raw=points,
            )
            per_cores.append(PerCoreUsage(cpu_id=cpu_id, usage_pct=summary))
            p95_values.append(summary.p95)
            if summary.p95 > hottest_core_p95:
                hottest_core_p95 = summary.p95
            if summary.max > hottest_core_max:
                hottest_core_max = summary.max

            aggregate_sum_avg += summary.avg
            aggregate_sum_max += summary.max
            aggregate_sum_p95 += summary.p95

        # 3. Skewness coefficient
        if len(p95_values) > 1:
            mean = float(np.mean(p95_values))
            std = float(np.std(p95_values))
            if std > 0:
                skewness = float(np.mean(((np.array(p95_values) - mean) / std) ** 3))
            else:
                skewness = 0.0
        else:
            skewness = 0.0

        # 4. Aggregate CPU
        core_count = len(per_cores) if per_cores else 1
        aggregate_avg = aggregate_sum_avg / core_count
        aggregate_max = aggregate_sum_max / core_count
        aggregate_p95 = aggregate_sum_p95 / core_count

        # 5. Context switches
        q_cs = f'rate(node_context_switches_total{{node_name="{self.node.pmm_node_name}"}}[5m])'
        cs_summary = self.pmm_client.get_metric_summary(q_cs, start, end)

        # 6. Run queue
        q_rq = f'node_procs_running{{node_name="{self.node.pmm_node_name}"}}'
        rq_summary = self.pmm_client.get_metric_summary(q_rq, start, end)

        # 7. MariaDB fixed threads
        q_read = f'mysql_global_variables_innodb_read_io_threads{{service_name="{self.node.service_name}"}}'
        q_write = f'mysql_global_variables_innodb_write_io_threads{{service_name="{self.node.service_name}"}}'
        q_clean = f'mysql_global_variables_innodb_page_cleaners{{service_name="{self.node.service_name}"}}'
        q_purge = f'mysql_global_variables_innodb_purge_threads{{service_name="{self.node.service_name}"}}'

        read_th = int(self.pmm_client.scalar_query(q_read) or 0)
        write_th = int(self.pmm_client.scalar_query(q_write) or 0)
        clean_th = int(self.pmm_client.scalar_query(q_clean) or 0)
        purge_th = int(self.pmm_client.scalar_query(q_purge) or 0)

        total_bg_threads = read_th + write_th + clean_th + purge_th

        # 8. Threads running
        q_tr = f'mysql_global_status_threads_running{{service_name="{self.node.service_name}"}}'
        tr_summary = self.pmm_client.get_metric_summary(q_tr, start, end)

        # 9. Projected downsized CPU
        projected_aggregate_max = aggregate_max * 2.0
        projected_hottest_core = hottest_core_max

        # 10. Vetoes
        vetoes = []
        veto_details = {}

        if hottest_core_p95 > self.thresholds.cpu.per_core_saturation_pct:
            vetoes.append(VetoReason.PER_CORE_SATURATION)
            veto_details[VetoReason.PER_CORE_SATURATION.value] = f"Hottest core P95 is {hottest_core_p95:.1f}% > {self.thresholds.cpu.per_core_saturation_pct}%"

        if cs_summary.p95 > self.thresholds.cpu.context_switches_per_sec_warn and self.ec2_info.physical_cores < 8:
            vetoes.append(VetoReason.CONTEXT_SWITCH_OVERLOAD)
            veto_details[VetoReason.CONTEXT_SWITCH_OVERLOAD.value] = f"Context switches P95 is {cs_summary.p95:.1f} > threshold"

        if rq_summary.p95 > (2 * self.ec2_info.physical_cores):
            vetoes.append(VetoReason.RUNQUEUE_SATURATION)
            veto_details[VetoReason.RUNQUEUE_SATURATION.value] = f"Run queue P95 is {rq_summary.p95:.1f} > {2 * self.ec2_info.physical_cores}"

        # Thread overcommit: InnoDB background threads are mostly SLEEPING/IO_WAIT,
        # so we apply an effective concurrency factor of 0.3 (only ~30% are active
        # simultaneously on average). We compare against current physical cores,
        # not the downsized target, to avoid false vetoes on correctly-sized nodes.
        bg_concurrency_factor = 0.3
        effective_bg_load = total_bg_threads * bg_concurrency_factor
        if effective_bg_load > self.ec2_info.physical_cores:
            vetoes.append(VetoReason.THREAD_OVERCOMMIT)
            veto_details[VetoReason.THREAD_OVERCOMMIT.value] = (
                f"Effective background thread load ({total_bg_threads} × {bg_concurrency_factor} = "
                f"{effective_bg_load:.1f}) > physical cores ({self.ec2_info.physical_cores})"
            )
            
        return CpuAnalysisResult(
            aggregate_avg_pct=aggregate_avg,
            aggregate_p95_pct=aggregate_p95,
            aggregate_max_pct=aggregate_max,
            per_core=per_cores,
            hottest_core_p95_pct=hottest_core_p95,
            hottest_core_max_pct=hottest_core_max,
            skewness_coefficient=skewness,
            context_switches_per_sec=cs_summary,
            procs_running=rq_summary,
            innodb_read_io_threads=read_th,
            innodb_write_io_threads=write_th,
            innodb_page_cleaners=clean_th,
            innodb_purge_threads=purge_th,
            total_background_threads=total_bg_threads,
            threads_running=tr_summary,
            projected_aggregate_max_pct=projected_aggregate_max,
            projected_hottest_core_pct=projected_hottest_core,
            vetoes=vetoes,
            veto_details=veto_details
        )

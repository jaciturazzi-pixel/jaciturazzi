from __future__ import annotations

import csv
from pathlib import Path

from mcplanner.models import RightsizingRecommendation

def _val(x) -> str:
    if x is None:
        return ""
    if hasattr(x, "value"):
        return str(x.value)
    return str(x)

def export_csv(recommendations: list[RightsizingRecommendation], output_path: str) -> str:
    headers = [
        "node_label", "cluster", "role", "aws_instance_id", "current_ec2_type", "recommended_ec2_type", "status", "confidence",
        "cpu_aggregate_avg_pct", "cpu_aggregate_p95_pct", "cpu_aggregate_max_pct", 
        "cpu_hottest_core_p95_pct", "cpu_hottest_core_max_pct", "cpu_skewness", 
        "cpu_context_switches_p95", "cpu_procs_running_p95", "cpu_background_threads", "cpu_projected_max_pct",
        "mem_total_ram_gb", "mem_rss_peak_gb", "mem_bp_configured_gb", "mem_bp_used_pct", 
        "mem_bp_hit_ratio_pct", "mem_session_ceiling_gb", "mem_oom_risk_score", "mem_swap_activity",
        "disk_volume_id", "disk_type", "disk_size_gb", "disk_prov_iops", "disk_peak_iops", 
        "disk_p95_iops", "disk_write_latency_p99_ms", "disk_verdict",
        "repl_is_replica", "repl_sbm_p99", "repl_micro_lag_episodes",
        "runway_current_occupancy_pct", "runway_projected_6m_pct", "runway_days_to_75pct", "runway_alert",
        "vetoes", "veto_count",
        "ec2_monthly_savings_usd", "ebs_monthly_savings_usd", "total_monthly_savings_usd", "total_annual_savings_usd"
    ]

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    
    with out.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        
        for r in recommendations:
            cpu = r.cpu
            mem = r.memory
            disk = r.disk
            repl = r.replication
            rw = r.runway
            
            row = [
                r.node_label,
                r.cluster,
                _val(r.role),
                r.aws_instance_id,
                r.current_ec2_type,
                r.recommended_ec2_type,
                r.status,
                _val(r.confidence),
                
                f"{cpu.aggregate_avg_pct:.2f}" if cpu else "",
                f"{cpu.aggregate_p95_pct:.2f}" if cpu else "",
                f"{cpu.aggregate_max_pct:.2f}" if cpu else "",
                f"{cpu.hottest_core_p95_pct:.2f}" if cpu else "",
                f"{cpu.hottest_core_max_pct:.2f}" if cpu else "",
                f"{cpu.skewness_coefficient:.2f}" if cpu else "",
                f"{cpu.context_switches_per_sec.p95:.2f}" if cpu else "",
                f"{cpu.procs_running.p95:.2f}" if cpu else "",
                str(cpu.total_background_threads) if cpu else "",
                f"{cpu.projected_aggregate_max_pct:.2f}" if cpu else "",
                
                f"{mem.total_ram_gb:.2f}" if mem else "",
                f"{mem.mysqld_rss_gb.max:.2f}" if mem else "",
                f"{mem.buffer_pool_configured_gb:.2f}" if mem else "",
                f"{mem.buffer_pool_used_pct:.2f}" if mem else "",
                f"{mem.buffer_pool_hit_ratio_pct:.2f}" if mem else "",
                f"{mem.session_ceiling_gb:.2f}" if mem else "",
                f"{mem.oom_risk_score:.2f}" if mem else "",
                str(mem.swap_activity) if mem else "",
                
                disk.volume_id if disk else "",
                disk.volume_type if disk else "",
                str(disk.size_gb) if disk else "",
                str(disk.provisioned_iops) if disk else "",
                f"{disk.total_iops_peak:.2f}" if disk else "",
                f"{disk.total_iops_p95:.2f}" if disk else "",
                f"{disk.write_latency_ms.p99:.2f}" if disk else "",
                _val(disk.verdict) if disk else "",
                
                str(repl.is_replica) if repl else "",
                f"{repl.sbm_p99:.2f}" if repl else "",
                str(repl.micro_lag_episodes) if repl else "",
                
                f"{rw.current_occupancy_pct:.2f}" if rw else "",
                f"{rw.projected_occupancy_6m_pct:.2f}" if rw else "",
                str(rw.days_to_75_pct) if rw and rw.days_to_75_pct else "",
                _val(rw.alert) if rw else "",
                
                "|".join(_val(v) for v in r.all_vetoes) if r.all_vetoes else "",
                str(len(r.all_vetoes)),
                
                f"{r.ec2_monthly_savings_usd:.2f}",
                f"{r.ebs_monthly_savings_usd:.2f}",
                f"{r.total_monthly_savings_usd:.2f}",
                f"{r.total_annual_savings_usd:.2f}"
            ]
            writer.writerow(row)
            
    return str(out.absolute())

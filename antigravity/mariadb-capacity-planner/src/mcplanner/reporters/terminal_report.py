from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import box

from mcplanner.models import RightsizingRecommendation, NodeRole

console = Console()

def _val(x) -> str:
    if x is None:
        return ""
    if hasattr(x, "value"):
        return str(x.value)
    return str(x)

def format_color(value: float, warning_thresh: float, critical_thresh: float, reverse: bool = False, is_pct: bool = False) -> str:
    """Format a value with a color based on thresholds."""
    val_str = f"{value:.2f}%" if is_pct else f"{value:.2f}"
    
    if not reverse:
        if value >= critical_thresh:
            return f"[red]{val_str}[/red]"
        elif value >= warning_thresh:
            return f"[yellow]{val_str}[/yellow]"
        else:
            return f"[green]{val_str}[/green]"
    else:
        if value <= critical_thresh:
            return f"[red]{val_str}[/red]"
        elif value <= warning_thresh:
            return f"[yellow]{val_str}[/yellow]"
        else:
            return f"[green]{val_str}[/green]"

def print_node_panel(rec: RightsizingRecommendation) -> None:
    # 1. Header
    header = Table.grid(padding=(0, 2))
    header.add_column(style="bold cyan")
    header.add_column()
    header.add_row("Node:", rec.node_label)
    header.add_row("Cluster:", rec.cluster)
    header.add_row("Role:", _val(rec.role))
    header.add_row("Instance ID:", rec.aws_instance_id)
    header.add_row("Current EC2:", rec.current_ec2_type)

    # 2. CPU Analysis
    cpu_table = Table(title="CPU Analysis", box=box.SIMPLE)
    cpu_table.add_column("Metric")
    cpu_table.add_column("Value")
    
    if rec.cpu:
        cpu = rec.cpu
        cpu_table.add_row("Aggregate (Avg / P95 / Max)", f"{cpu.aggregate_avg_pct:.1f}% / {cpu.aggregate_p95_pct:.1f}% / {cpu.aggregate_max_pct:.1f}%")
        cpu_table.add_row("Hottest Core (P95 / Max)", f"{cpu.hottest_core_p95_pct:.1f}% / {cpu.hottest_core_max_pct:.1f}%")
        cpu_table.add_row("Skewness", f"{cpu.skewness_coefficient:.2f}")
        cpu_table.add_row("Context Switches/s (P95)", f"{cpu.context_switches_per_sec.p95:.0f}")
        cpu_table.add_row("Run Queue (P95)", f"{cpu.procs_running.p95:.1f}")
        cpu_table.add_row("Background Threads", f"{cpu.total_background_threads}")
        cpu_table.add_row("Projected Downsized Max CPU", f"{cpu.projected_aggregate_max_pct:.1f}%")

    # 3. Memory Analysis
    mem_table = Table(title="Memory Analysis", box=box.SIMPLE)
    mem_table.add_column("Metric")
    mem_table.add_column("Value")
    
    if rec.memory:
        mem = rec.memory
        mem_table.add_row("Total RAM (GB)", f"{mem.total_ram_gb:.1f}")
        mem_table.add_row("mysqld RSS Peak (GB)", f"{mem.mysqld_rss_gb.max:.1f}")
        mem_table.add_row("Buffer Pool (Config/Used/Hit/Dirty)", f"{mem.buffer_pool_configured_gb:.1f}G / {mem.buffer_pool_used_pct:.1f}% / {mem.buffer_pool_hit_ratio_pct:.2f}% / {mem.buffer_pool_dirty_pct:.1f}%")
        mem_table.add_row("Buffer Pool Wait Free (Max)", f"{mem.buffer_pool_wait_free.max:.1f}")
        mem_table.add_row("LRU Evictions/s (P95)", f"{mem.lru_pages_freed_per_sec.p95:.1f}")
        mem_table.add_row("Session Overhead Ceiling (GB)", f"{mem.session_ceiling_gb:.1f}")
        mem_table.add_row("OOM Risk Score", f"{mem.oom_risk_score:.1f}")
        mem_table.add_row("Swap Activity", "Yes" if mem.swap_activity else "No")
        mem_table.add_row("Cache Pressure Post-Downsize", "Yes — expect more disk I/O/CPU" if mem.cache_pressure_warning else "No")

    # 4. Disk Analysis
    disk_table = Table(title="Disk Analysis", box=box.SIMPLE)
    disk_table.add_column("Metric")
    disk_table.add_column("Value")

    if rec.disk:
        disk = rec.disk
        disk_table.add_row("Volume ID / Type / Size", f"{disk.volume_id} / {disk.volume_type} / {disk.size_gb} GB")
        disk_table.add_row("Provisioned IOPS", f"{disk.provisioned_iops}")
        disk_table.add_row("Peak IOPS", f"{disk.total_iops_peak:.0f}")
        disk_table.add_row("P95 IOPS", f"{disk.total_iops_p95:.0f}")
        disk_table.add_row("Read / Write Latency P99 (ms)", f"{disk.read_latency_ms.p99:.2f} / {disk.write_latency_ms.p99:.2f}")
        disk_table.add_row("Read / Write Throughput (MB/s)", f"{disk.read_throughput_mbps.max:.1f} / {disk.write_throughput_mbps.max:.1f}")
        disk_table.add_row("InnoDB Fsyncs/s (Max)", f"{disk.innodb_data_fsyncs_per_sec.max:.0f}")
        disk_table.add_row("Verdict", _val(disk.verdict))

    # 5. Replication
    repl_table = None
    if rec.replication and rec.replication.is_replica:
        repl_table = Table(title="Replication Analysis", box=box.SIMPLE)
        repl_table.add_column("Metric")
        repl_table.add_column("Value")
        repl = rec.replication
        repl_table.add_row("SBM (Avg / P95 / P99 / Max)", f"{repl.sbm.avg:.1f} / {repl.sbm.p95:.1f} / {repl.sbm.p99:.1f} / {repl.sbm.max:.1f}")
        repl_table.add_row("Micro-lag Episodes", f"{repl.micro_lag_episodes}")
        repl_table.add_row("Max Episode Duration (s)", f"{repl.micro_lag_max_duration_sec:.1f}")

    # 6. Disk Runway
    runway_table = Table(title="Disk Runway", box=box.SIMPLE)
    runway_table.add_column("Metric")
    runway_table.add_column("Value")

    if rec.runway:
        rw = rec.runway
        runway_table.add_row("Current (Used / Free / Occ%)", f"{rw.current_used_gb:.1f} GB / {rw.current_free_gb:.1f} GB / {rw.current_occupancy_pct:.1f}%")
        runway_table.add_row("Growth (Daily / Monthly)", f"{rw.daily_growth_gb:.1f} GB / {rw.monthly_growth_gb:.1f} GB")
        runway_table.add_row("Projected 6m Occupancy", f"{rw.projected_occupancy_6m_pct:.1f}%")
        def _fmt_days(d) -> str:
            if d is None:
                return "N/A"
            if d > 3650:
                return "> 10yr"
            if d > 365:
                return f"{d / 365:.1f}yr"
            return f"{d:.0f}d"
        runway_table.add_row("Days to 75% / 85% / 95%", f"{_fmt_days(rw.days_to_75_pct)} / {_fmt_days(rw.days_to_85_pct)} / {_fmt_days(rw.days_to_95_pct)}")
        runway_table.add_row("Alert", _val(rw.alert))

    # 7. Recommendation
    rec_table = Table(title="Recommendation & Savings", box=box.SIMPLE)
    rec_table.add_column("Metric")
    rec_table.add_column("Value")
    
    rec_table.add_row("Status", f"[bold {'green' if rec.status == 'OK' else 'yellow' if rec.status == 'VETOED' else 'red'}]{rec.status}[/]")
    rec_table.add_row("Current -> Recommended", f"{rec.current_ec2_type} -> {rec.recommended_ec2_type}")
    rec_table.add_row("Confidence", _val(rec.confidence))
    
    if rec.all_vetoes:
        veto_text = "\\n".join(f"- {_val(v)}: {rec.all_veto_details.get(_val(v), rec.all_veto_details.get(v, ''))}" for v in rec.all_vetoes)
        rec_table.add_row("Vetoes", f"[red]{veto_text}[/red]")
        
    rec_table.add_row("EC2 Savings/mo", f"${rec.ec2_monthly_savings_usd:.2f}")
    rec_table.add_row("EBS Savings/mo", f"${rec.ebs_monthly_savings_usd:.2f}")
    rec_table.add_row("Total Savings/mo", f"[bold green]${rec.total_monthly_savings_usd:.2f}[/]")
    rec_table.add_row("Total Savings/yr", f"[bold green]${rec.total_annual_savings_usd:.2f}[/]")

    # Assembly
    content = [header, "\n", cpu_table, "\n", mem_table, "\n", disk_table]
    if repl_table:
        content.extend(["\n", repl_table])
    content.extend(["\n", runway_table, "\n", rec_table])

    from rich.console import Group
    panel = Panel(Group(*content), title=f"Node Inspection: {rec.node_label}", border_style="blue")
    console.print(panel)


def print_summary_table(recommendations: list[RightsizingRecommendation]) -> None:
    table = Table(title="Rightsizing Summary", show_header=True, header_style="bold magenta")
    
    table.add_column("Instance")
    table.add_column("Cluster")
    table.add_column("Current EC2")
    table.add_column("Recommended")
    table.add_column("Peak CPU%")
    table.add_column("Hottest Core%")
    table.add_column("Proj CPU%")
    table.add_column("RAM GB")
    table.add_column("OOM Risk")
    table.add_column("SBM P99")
    table.add_column("Disk Type")
    table.add_column("Peak IOPS")
    table.add_column("Latency P99ms")
    table.add_column("Runway 6m%")
    table.add_column("Vetoes")
    table.add_column("Status")
    table.add_column("EC2 $/mo", justify="right")
    table.add_column("EBS $/mo", justify="right")
    table.add_column("Total $/mo", justify="right")

    recs = sorted(recommendations, key=lambda x: (x.cluster, x.node_label))
    
    tot_ec2 = 0.0
    tot_ebs = 0.0
    tot_tot = 0.0

    for r in recs:
        cpu = r.cpu
        mem = r.memory
        disk = r.disk
        repl = r.replication
        runway = r.runway

        status_color = "green" if r.status == "OK" else "yellow" if r.status == "VETOED" else "red"
        status_text = f"[{status_color}]{r.status}[/{status_color}]"

        sbm_val = f"{repl.sbm_p99:.1f}" if repl and repl.is_replica else "-"
        
        vetoes_str = f"[red]{len(r.all_vetoes)}[/red]" if r.all_vetoes else "0"

        table.add_row(
            r.node_label,
            r.cluster,
            r.current_ec2_type,
            r.recommended_ec2_type,
            f"{cpu.aggregate_max_pct:.1f}" if cpu else "-",
            f"{cpu.hottest_core_max_pct:.1f}" if cpu else "-",
            f"{cpu.projected_aggregate_max_pct:.1f}" if cpu else "-",
            f"{mem.total_ram_gb:.1f}" if mem else "-",
            f"{mem.oom_risk_score:.1f}" if mem else "-",
            sbm_val,
            f"{disk.volume_type}" if disk else "-",
            f"{disk.total_iops_peak:.0f}" if disk else "-",
            f"{disk.write_latency_ms.p99:.2f}" if disk else "-",
            f"{runway.projected_occupancy_6m_pct:.1f}%" if runway else "-",
            vetoes_str,
            status_text,
            f"${r.ec2_monthly_savings_usd:.2f}",
            f"${r.ebs_monthly_savings_usd:.2f}",
            f"${r.total_monthly_savings_usd:.2f}"
        )

        tot_ec2 += r.ec2_monthly_savings_usd
        tot_ebs += r.ebs_monthly_savings_usd
        tot_tot += r.total_monthly_savings_usd

    table.add_row(
        "TOTAL", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "",
        f"[bold]${tot_ec2:.2f}[/]",
        f"[bold]${tot_ebs:.2f}[/]",
        f"[bold]${tot_tot:.2f}[/]",
        style="bold"
    )

    console.print(table)


def print_grand_total(recommendations: list[RightsizingRecommendation]) -> None:
    total_inst = len(recommendations)
    vetoed = sum(1 for r in recommendations if r.status == "VETOED")
    errors = sum(1 for r in recommendations if r.status == "ERROR")
    tot_monthly = sum(r.total_monthly_savings_usd for r in recommendations)
    tot_annual = sum(r.total_annual_savings_usd for r in recommendations)

    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold cyan")
    grid.add_column(style="bold green")
    
    grid.add_row("Total Instances Analyzed:", str(total_inst))
    grid.add_row("Vetoed / Errors:", f"{vetoed} / {errors}")
    grid.add_row("Total Monthly Savings:", f"${tot_monthly:,.2f}")
    grid.add_row("Total Annual Savings:", f"${tot_annual:,.2f}")

    panel = Panel(grid, title="Grand Total", border_style="green", expand=False)
    console.print(panel)

"""
mcplanner.cli — CLI entry point for the MariaDB Capacity Planning & Rightsizing tool.

Orchestrates the full analysis pipeline: config loading, PMM/AWS collection,
per-node analysis, rightsizing decisions, and multi-format reporting.

Supports dynamic node discovery via --instance or --tag (AWS EC2 filters),
SSM-based MariaDB/OS fallback queries, and io2 IOPS right-sizing mode.
"""

from __future__ import annotations

import datetime
import json
import os
import sys
import time
from pathlib import Path

import click
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn

from mcplanner.config import load_config, Config, NodeEntry, EC2_INSTANCE_SPECS, AwsConfig
from mcplanner.models import (
    NodeRole,
    Ec2Info,
    EbsVolumeInfo,
    EbsVerdict,
    RightsizingRecommendation,
    CpuAnalysisResult,
    MemoryAnalysisResult,
    DiskAnalysisResult,
    ReplicationAnalysisResult,
    RunwayResult,
    Confidence,
)
from mcplanner.collectors.pmm_client import PmmClient
from mcplanner.collectors.aws_client import AwsClient
from mcplanner.analyzers.cpu_analyzer import CpuAnalyzer
from mcplanner.analyzers.memory_analyzer import MemoryAnalyzer
from mcplanner.analyzers.disk_analyzer import DiskAnalyzer
from mcplanner.analyzers.replication_analyzer import ReplicationAnalyzer
from mcplanner.analyzers.runway_projector import RunwayProjector
from mcplanner.engine.rightsizing_engine import RightsizingEngine
from mcplanner.reporters.terminal_report import print_node_panel, print_summary_table, print_grand_total
from mcplanner.reporters.markdown_report import generate_markdown_report
from mcplanner.reporters.csv_export import export_csv

console = Console()


# ---------------------------------------------------------------------------
# Helper: build analysis window
# ---------------------------------------------------------------------------

def _build_time_window(days: int, start_date: str | None, end_date: str | None):
    """Return (start, end) as timezone-aware datetimes.

    Logic:
      - If both start-date and end-date are given, use them directly.
      - If only end-date is given, start = end - days.
      - If only start-date is given, end = today.
      - If neither is given, end = today, start = today - days.
    """
    end = datetime.datetime.now(datetime.timezone.utc)
    start = end - datetime.timedelta(days=days)

    if end_date:
        end = datetime.datetime.strptime(end_date, "%Y-%m-%d").replace(
            hour=23, minute=59, second=59, tzinfo=datetime.timezone.utc
        )
    if start_date:
        start = datetime.datetime.strptime(start_date, "%Y-%m-%d").replace(
            tzinfo=datetime.timezone.utc
        )
    elif end_date and not start_date:
        # end-date given but no start-date → derive from --days
        start = end - datetime.timedelta(days=days)

    return start, end


# ---------------------------------------------------------------------------
# Helper: parse tag filters for EC2 discovery
# ---------------------------------------------------------------------------

def _parse_tag_filter(tag_str: str) -> dict:
    """Parse a tag filter string like 'mc:service=awards' into an EC2 filter."""
    if "=" in tag_str:
        key, val = tag_str.split("=", 1)
        name = key if key.startswith("tag:") else f"tag:{key}"
        return {"Name": name, "Values": [f"*{val}*" if "*" not in val else val]}
    else:
        return {"Name": "tag:Name", "Values": [f"*{tag_str}*"]}


# ---------------------------------------------------------------------------
# Helper: discover nodes from AWS EC2 (--instance / --tag)
# ---------------------------------------------------------------------------

def _discover_nodes_from_aws(
    aws_session,
    region: str,
    instance_filter: str | None,
    tag_filter: str | None,
    config: Config,
) -> list[NodeEntry]:
    """Discover EC2 instances and build NodeEntry objects.

    Falls back to config.nodes if no dynamic filter is provided.
    If --instance or --tag is given, queries EC2 describe_instances.
    """
    import boto3

    ec2 = aws_session.client("ec2", region_name=region)

    filters = [{"Name": "instance-state-name", "Values": ["running"]}]

    if instance_filter:
        # Could be an instance ID (i-xxx) or a Name pattern
        if instance_filter.startswith("i-"):
            # Direct instance ID lookup
            try:
                resp = ec2.describe_instances(InstanceIds=[instance_filter])
            except Exception:
                # Fallback to name search
                filters.append({"Name": "tag:Name", "Values": [f"*{instance_filter}*"]})
                resp = ec2.describe_instances(Filters=filters)
        else:
            filters.append({"Name": "tag:Name", "Values": [f"*{instance_filter}*"]})
            resp = ec2.describe_instances(Filters=filters)
    elif tag_filter:
        filters.append(_parse_tag_filter(tag_filter))
        resp = ec2.describe_instances(Filters=filters)
    else:
        return config.nodes  # Use config.yaml inventory

    # Build NodeEntry list from discovered instances
    nodes: list[NodeEntry] = []
    for reservation in resp.get("Reservations", []):
        for inst in reservation.get("Instances", []):
            tags = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
            inst_id = inst["InstanceId"]
            name = tags.get("Name", inst_id)
            cluster = tags.get("mc:service", tags.get("Cluster",
                        name.split("0")[0] if "0" in name else name))

            # Try to find this node in existing config (label/cluster/role only —
            # aws_instance_id always comes from the live AWS lookup, never the config)
            config_match = next(
                (n for n in config.nodes
                 if n.aws_instance_id == inst_id or n.label == name),
                None,
            )

            if config_match:
                nodes.append(NodeEntry(
                    label=config_match.label,
                    pmm_node_name=config_match.pmm_node_name,
                    service_name=config_match.service_name,
                    aws_instance_id=inst_id,
                    cluster=config_match.cluster,
                    role=config_match.role,
                ))
            else:
                # Auto-create NodeEntry from EC2 metadata
                role = "writer"
                name_lower = name.lower()
                if "snapshot" in name_lower or "backup" in name_lower:
                    role = "snapshot"
                elif any(s in name_lower for s in ["slave", "reader", "replica"]):
                    role = "reader"
                # Heuristic: nodes ending in 010 are usually writers
                if name_lower.endswith("010"):
                    role = "writer"

                nodes.append(NodeEntry(
                    label=name,
                    pmm_node_name=name,
                    service_name=f"{name}-mysql",
                    aws_instance_id=inst_id,
                    cluster=cluster,
                    role=role,
                ))

    return nodes


# ---------------------------------------------------------------------------
# Helper: SSM-based MariaDB & OS queries (--use-ssm)
# ---------------------------------------------------------------------------

def _query_mariadb_via_ssm(ssm_client, instance_id: str, instance_name: str) -> dict | None:
    """Query MariaDB internal status and OS disk usage via AWS SSM.

    Returns a dict with buffer pool, thread, and disk metrics, or None on failure.
    """
    sql_cmd = (
        'mysql -u root -s -N -e "'
        "SELECT CONCAT_WS(';', "
        "@@hostname, "
        "ROUND(@@innodb_buffer_pool_size/(1024*1024*1024),2), "
        "CAST((SELECT VARIABLE_VALUE FROM INFORMATION_SCHEMA.GLOBAL_STATUS "
        "  WHERE VARIABLE_NAME='Innodb_buffer_pool_pages_data') AS UNSIGNED), "
        "CAST((SELECT VARIABLE_VALUE FROM INFORMATION_SCHEMA.GLOBAL_STATUS "
        "  WHERE VARIABLE_NAME='Innodb_buffer_pool_pages_total') AS UNSIGNED), "
        "ROUND((SELECT VARIABLE_VALUE FROM INFORMATION_SCHEMA.GLOBAL_STATUS "
        "  WHERE VARIABLE_NAME='Innodb_buffer_pool_pages_dirty')"
        "  *@@innodb_page_size/(1024*1024*1024),2), "
        "ROUND((1-((SELECT VARIABLE_VALUE FROM INFORMATION_SCHEMA.GLOBAL_STATUS "
        "  WHERE VARIABLE_NAME='Innodb_buffer_pool_reads')/"
        "  NULLIF((SELECT VARIABLE_VALUE FROM INFORMATION_SCHEMA.GLOBAL_STATUS "
        "  WHERE VARIABLE_NAME='Innodb_buffer_pool_read_requests'),0)))*100,3), "
        "CAST((SELECT VARIABLE_VALUE FROM INFORMATION_SCHEMA.GLOBAL_STATUS "
        "  WHERE VARIABLE_NAME='Innodb_buffer_pool_wait_free') AS UNSIGNED), "
        "CAST((SELECT VARIABLE_VALUE FROM INFORMATION_SCHEMA.GLOBAL_STATUS "
        "  WHERE VARIABLE_NAME='Threads_running') AS UNSIGNED), "
        "CAST((SELECT VARIABLE_VALUE FROM INFORMATION_SCHEMA.GLOBAL_STATUS "
        "  WHERE VARIABLE_NAME='Threads_connected') AS UNSIGNED), "
        "CAST((SELECT VARIABLE_VALUE FROM INFORMATION_SCHEMA.GLOBAL_STATUS "
        "  WHERE VARIABLE_NAME='Max_used_connections') AS UNSIGNED), "
        "CAST((SELECT VARIABLE_VALUE FROM INFORMATION_SCHEMA.GLOBAL_STATUS "
        "  WHERE VARIABLE_NAME='Innodb_row_lock_waits') AS UNSIGNED), "
        "@@version"
        ');"'
    )

    # df for data disk usage
    df_cmd = "df -BG /var/lib/mysql | tail -1 | awk '{print $2\";\"$3\";\"$4\";\"$5}'"

    try:
        resp = ssm_client.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": [sql_cmd, df_cmd]},
        )
        command_id = resp["Command"]["CommandId"]
        time.sleep(3)

        invocation = ssm_client.get_command_invocation(
            CommandId=command_id, InstanceId=instance_id
        )
        output = invocation.get("StandardOutputContent", "").strip()

        if not output:
            return None

        lines = output.strip().split("\n")
        db_data = {}

        # Parse MariaDB line (semicolon-separated)
        if lines and ";" in lines[0]:
            parts = lines[0].split(";")
            db_data = {
                "hostname": parts[0] if len(parts) > 0 else instance_id,
                "bp_config_gb": _safe_float(parts[1]) if len(parts) > 1 else 0.0,
                "bp_pages_data": _safe_int(parts[2]) if len(parts) > 2 else 0,
                "bp_pages_total": _safe_int(parts[3]) if len(parts) > 3 else 0,
                "bp_dirty_gb": _safe_float(parts[4]) if len(parts) > 4 else 0.0,
                "hit_ratio_pct": _safe_float(parts[5]) if len(parts) > 5 else 0.0,
                "wait_free_pages": _safe_int(parts[6]) if len(parts) > 6 else 0,
                "threads_running": _safe_int(parts[7]) if len(parts) > 7 else 0,
                "threads_connected": _safe_int(parts[8]) if len(parts) > 8 else 0,
                "max_used_connections": _safe_int(parts[9]) if len(parts) > 9 else 0,
                "row_lock_waits": _safe_int(parts[10]) if len(parts) > 10 else 0,
                "mariadb_version": parts[11] if len(parts) > 11 else "unknown",
            }
            if db_data["bp_pages_total"] > 0:
                occupancy = min(1.0, db_data["bp_pages_data"] / db_data["bp_pages_total"])
                db_data["bp_used_gb"] = round(occupancy * db_data["bp_config_gb"], 2)
            else:
                db_data["bp_used_gb"] = 0.0

        # Parse df line (semicolon-separated: total;used;avail;use%)
        if len(lines) > 1 and ";" in lines[1]:
            df_parts = lines[1].split(";")
            db_data["disk_total_gb"] = _safe_int(df_parts[0].replace("G", "")) if len(df_parts) > 0 else 0
            db_data["disk_used_gb"] = _safe_int(df_parts[1].replace("G", "")) if len(df_parts) > 1 else 0
            db_data["disk_avail_gb"] = _safe_int(df_parts[2].replace("G", "")) if len(df_parts) > 2 else 0
            db_data["disk_use_pct"] = df_parts[3].replace("%", "") if len(df_parts) > 3 else "0"

        return db_data

    except Exception as e:
        console.print(f"  [yellow]⚠ SSM unavailable for {instance_name} ({instance_id}): {e}[/yellow]")
        return None


def _safe_float(val, default=0.0) -> float:
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def _safe_int(val, default=0) -> int:
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return default


# ---------------------------------------------------------------------------
# Helper: analyze a single node
# ---------------------------------------------------------------------------

def _analyze_node(
    node: NodeEntry,
    pmm: PmmClient,
    aws: AwsClient,
    config: Config,
    start: datetime.datetime,
    end: datetime.datetime,
    keep_io2: bool = False,
    ssm_data: dict | None = None,
) -> RightsizingRecommendation:
    """Run the full analysis pipeline on a single node and return a recommendation."""

    # 1. AWS metadata
    try:
        ec2_info = aws.get_instance_info(node.aws_instance_id)
    except Exception as e:
        console.print(f"  [yellow]⚠ AWS metadata unavailable for {node.label}: {e}[/yellow]")
        ec2_info = Ec2Info(
            instance_id=node.aws_instance_id,
            instance_type="unknown",
            vcpus=0,
            physical_cores=0,
            memory_gb=0.0,
            name_tag=node.label,
            cluster_tag=node.cluster,
        )

    # 2. EBS data volumes
    try:
        data_volumes = aws.get_data_volumes(
            node.aws_instance_id, data_device=config.disk.data_device
        )
    except Exception:
        data_volumes = []

    primary_volume = data_volumes[0] if data_volumes else EbsVolumeInfo(
        volume_id="unknown", device=config.disk.data_device,
        volume_type="unknown", size_gb=0, provisioned_iops=0,
    )

    # 3. CPU Analysis
    try:
        cpu_analyzer = CpuAnalyzer(pmm, node, ec2_info, config.thresholds)
        cpu_result = cpu_analyzer.analyze(start, end)
    except Exception as e:
        console.print(f"  [yellow]⚠ CPU analysis failed for {node.label}: {e}[/yellow]")
        cpu_result = CpuAnalysisResult()

    # 4. Memory Analysis
    try:
        mem_analyzer = MemoryAnalyzer(pmm, node, ec2_info, config.thresholds)
        mem_result = mem_analyzer.analyze(start, end)
    except Exception as e:
        console.print(f"  [yellow]⚠ Memory analysis failed for {node.label}: {e}[/yellow]")
        mem_result = MemoryAnalysisResult(total_ram_gb=ec2_info.memory_gb)

    # Enrich memory with SSM data if available
    if ssm_data:
        if mem_result.buffer_pool_configured_gb == 0.0 and ssm_data.get("bp_config_gb"):
            mem_result.buffer_pool_configured_gb = ssm_data["bp_config_gb"]
        if mem_result.buffer_pool_hit_ratio_pct == 0.0 and ssm_data.get("hit_ratio_pct"):
            mem_result.buffer_pool_hit_ratio_pct = ssm_data["hit_ratio_pct"]
        if mem_result.max_used_connections == 0 and ssm_data.get("max_used_connections"):
            mem_result.max_used_connections = ssm_data["max_used_connections"]

    # 5. Disk Analysis
    try:
        disk_analyzer = DiskAnalyzer(
            pmm, node, primary_volume, config.thresholds, config.disk
        )
        disk_result = disk_analyzer.analyze(start, end)
    except Exception as e:
        console.print(f"  [yellow]⚠ Disk analysis failed for {node.label}: {e}[/yellow]")
        disk_result = DiskAnalysisResult(
            volume_id=primary_volume.volume_id,
            device=primary_volume.device,
            volume_type=primary_volume.volume_type,
            size_gb=primary_volume.size_gb,
            provisioned_iops=primary_volume.provisioned_iops,
        )

    # Apply --keep-io2 override: instead of migrating to gp3, right-size io2 IOPS
    if keep_io2 and disk_result.verdict == EbsVerdict.MIGRATE_GP3:
        disk_result.verdict = EbsVerdict.KEEP_IO2
        # Right-size provisioned IOPS: target = max(100, peak * safety_factor)
        peak = disk_result.total_iops_peak
        safety = config.thresholds.disk.iops_safety_factor
        optimized_iops = max(100, int(peak * safety))
        if optimized_iops < primary_volume.provisioned_iops:
            # Calculate savings from IOPS reduction on io2
            current_iops_cost = primary_volume.provisioned_iops * config.pricing.ebs.io2_per_iops
            new_iops_cost = optimized_iops * config.pricing.ebs.io2_per_iops
            disk_result.monthly_savings_usd = round(current_iops_cost - new_iops_cost, 2)
            disk_result.recommended_gp3_iops = optimized_iops  # reuse field for "optimized IOPS"
        else:
            disk_result.monthly_savings_usd = 0.0
            disk_result.recommended_gp3_iops = primary_volume.provisioned_iops

    # Enrich runway with SSM disk data if available
    if ssm_data and ssm_data.get("disk_used_gb"):
        if runway_result_uses_ssm := True:
            pass  # We'll set it in the runway result below

    # 6. Replication Analysis
    try:
        repl_analyzer = ReplicationAnalyzer(pmm, node, config.thresholds)
        repl_result = repl_analyzer.analyze(start, end)
    except Exception as e:
        console.print(f"  [yellow]⚠ Replication analysis failed for {node.label}: {e}[/yellow]")
        repl_result = ReplicationAnalysisResult(
            is_replica=(node.role != "writer")
        )

    # 7. Runway Projection
    try:
        runway_proj = RunwayProjector(
            pmm, node, config.disk, config.thresholds.runway,
            total_volume_gb=primary_volume.size_gb,
        )
        runway_result = runway_proj.analyze(start, end)
    except Exception as e:
        console.print(f"  [yellow]⚠ Runway projection failed for {node.label}: {e}[/yellow]")
        runway_result = RunwayResult()

    # Enrich runway with SSM df data if available and runway was empty
    if ssm_data and runway_result.current_used_gb == 0.0:
        if ssm_data.get("disk_used_gb"):
            runway_result.current_used_gb = float(ssm_data["disk_used_gb"])
            runway_result.current_total_gb = float(ssm_data.get("disk_total_gb", primary_volume.size_gb))
            runway_result.current_free_gb = float(ssm_data.get("disk_avail_gb", 0))
            if runway_result.current_total_gb > 0:
                runway_result.current_occupancy_pct = round(
                    (runway_result.current_used_gb / runway_result.current_total_gb) * 100, 2
                )

    # 8. Rightsizing decision
    engine = RightsizingEngine(config)
    recommendation = engine.evaluate(
        node=node,
        ec2_info=ec2_info,
        cpu=cpu_result,
        memory=mem_result,
        disk=disk_result,
        replication=repl_result,
        runway=runway_result,
    )

    return recommendation


# ---------------------------------------------------------------------------
# CLI Commands
# ---------------------------------------------------------------------------

@click.group()
@click.version_option(version="2.0.0", prog_name="mcplanner")
def main():
    """MariaDB Capacity Planning & Rightsizing CLI — PMM/Prometheus Edition."""
    pass


@main.command()
@click.option("--config", "-c", "config_path", required=True,
              type=click.Path(exists=True), help="Path to config.yaml")
@click.option("--profile", default=None,
              help="AWS CLI profile (overrides config, default: miniclippool-databases)")
@click.option("--region", default=None,
              help="AWS region (overrides config, default: us-west-2)")
@click.option("--instance", default=None,
              help="Target single instance name or instance ID (e.g., prod-sql-awards010 or i-07af...)")
@click.option("--tag", default=None,
              help="Target tag filter (e.g., mc:service=awards)")
@click.option("--days", "-d", default=90,
              help="Number of recent days to evaluate (default: 90)")
@click.option("--start-date", default=None,
              help="Start date (YYYY-MM-DD). Defaults to end-date minus --days if omitted.")
@click.option("--end-date", default=None,
              help="End date (YYYY-MM-DD). Defaults to today if omitted.")
@click.option("--use-ssm", is_flag=True, default=False,
              help="Enables AWS SSM remote MariaDB status & OS disk (df) queries")
@click.option("--keep-io2", is_flag=True, default=False,
              help="Keeps io2 volume type, but right-sizes provisioned IOPS based on peak usage")
@click.option("--output-dir", "-o", "output_dir", default="./reports",
              help="Output directory for timestamped Markdown reports (default: ./reports)")
def analyze(config_path, profile, region, instance, tag, days, start_date, end_date,
            use_ssm, keep_io2, output_dir):
    """Run full capacity planning analysis on MariaDB nodes."""
    import boto3

    # Load config
    console.print("\n[bold cyan]🚀 mcplanner v2.0 — MariaDB Capacity Planning & Rightsizing[/bold cyan]")
    console.print("[dim]   Powered by PMM/Prometheus telemetry[/dim]\n")

    try:
        config = load_config(config_path)
    except Exception as e:
        console.print(f"[bold red]❌ Failed to load config: {e}[/bold red]")
        sys.exit(1)

    # CLI overrides for AWS
    effective_profile = profile or config.aws.profile
    effective_region = region or config.aws.region
    config.aws.profile = effective_profile
    config.aws.region = effective_region

    console.print(f"🔑 AWS Profile: [bold]{effective_profile}[/bold]  |  Region: [bold]{effective_region}[/bold]")
    if use_ssm:
        console.print("🔐 [bold yellow]SSM Enabled[/bold yellow] — Querying MariaDB internally via aws ssm send-command")
    if keep_io2:
        console.print("💾 [bold yellow]--keep-io2[/bold yellow] — io2 volumes will be kept; IOPS right-sized based on peak usage")

    # Build time window
    start, end = _build_time_window(days, start_date, end_date)
    console.print(f"📅 Analysis window: [bold]{start.strftime('%Y-%m-%d')}[/bold] → "
                  f"[bold]{end.strftime('%Y-%m-%d')}[/bold] ({(end - start).days} days)")

    # Initialize AWS session
    try:
        aws_session = boto3.Session(profile_name=effective_profile, region_name=effective_region)
        aws = AwsClient(config.aws)
    except Exception as e:
        console.print(f"[bold yellow]⚠ AWS client init failed: {e}[/bold yellow]")
        aws_session = None
        aws = None

    # Discover nodes (--instance / --tag / config.yaml)
    if (instance or tag) and aws_session:
        console.print(f"🔍 Discovering nodes from AWS EC2...")
        nodes = _discover_nodes_from_aws(aws_session, effective_region, instance, tag, config)
    elif instance:
        # No AWS session but instance given → search config
        nodes = [n for n in config.nodes
                 if instance.lower() in n.label.lower()
                 or instance.lower() in n.aws_instance_id.lower()]
    elif tag:
        # No AWS session but tag given → search config by cluster
        tag_val = tag.split("=")[-1].lower() if "=" in tag else tag.lower()
        nodes = [n for n in config.nodes if tag_val in n.cluster.lower()]
    else:
        nodes = config.nodes

    if not nodes:
        console.print("[bold red]❌ No instances found with the specified filters.[/bold red]")
        sys.exit(1)

    console.print(f"🎯 Nodes to analyze: [bold]{len(nodes)}[/bold]\n")

    # Initialize PMM client
    try:
        pmm = PmmClient(config.pmm)
    except Exception as e:
        console.print(f"[bold red]❌ Failed to initialize PMM client: {e}[/bold red]")
        sys.exit(1)

    # SSM client (if enabled)
    ssm_client = None
    if use_ssm and aws_session:
        try:
            ssm_client = aws_session.client("ssm", region_name=effective_region)
            console.print("🔐 SSM client initialized\n")
        except Exception as e:
            console.print(f"[yellow]⚠ SSM client init failed: {e}[/yellow]\n")

    # Process nodes
    recommendations: list[RightsizingRecommendation] = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Analyzing nodes...", total=len(nodes))

        for node in nodes:
            progress.update(task, description=f"Analyzing [bold]{node.label}[/bold]...")

            # SSM queries (if enabled)
            ssm_data = None
            if ssm_client:
                progress.stop()
                console.print(f"  🔐 SSM: Querying MariaDB on {node.label}...")
                ssm_data = _query_mariadb_via_ssm(ssm_client, node.aws_instance_id, node.label)
                if ssm_data:
                    console.print(f"  ✅ SSM: Got {len(ssm_data)} metrics from {ssm_data.get('hostname', node.label)}")
                progress.start()

            try:
                rec = _analyze_node(
                    node, pmm, aws, config, start, end,
                    keep_io2=keep_io2, ssm_data=ssm_data,
                )
                recommendations.append(rec)

                # Print per-node panel
                progress.stop()
                print_node_panel(rec)
                progress.start()

            except Exception as e:
                console.print(f"\n[bold red]❌ Error analyzing {node.label}: {e}[/bold red]")
                error_rec = RightsizingRecommendation(
                    node_label=node.label,
                    cluster=node.cluster,
                    role=NodeRole(node.role),
                    aws_instance_id=node.aws_instance_id,
                    status="ERROR",
                    error_msg=str(e),
                    confidence=Confidence.LOW,
                )
                recommendations.append(error_rec)

            progress.advance(task)

    # Sort by cluster then label
    recommendations.sort(key=lambda r: (r.cluster, r.node_label))

    # Terminal summary
    console.print()
    print_summary_table(recommendations)
    print_grand_total(recommendations)

    # Output directory
    os.makedirs(output_dir, exist_ok=True)
    now_str = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

    # Markdown report
    md_path = os.path.join(output_dir, f"capacity_report_{now_str}.md")
    try:
        saved = generate_markdown_report(recommendations, md_path)
        console.print(f"\n📝 Markdown report: [bold green]{saved}[/bold green]")
    except Exception as e:
        console.print(f"[bold red]❌ Markdown report failed: {e}[/bold red]")

    # CSV export
    csv_path = os.path.join(output_dir, f"capacity_data_{now_str}.csv")
    try:
        saved = export_csv(recommendations, csv_path)
        console.print(f"📊 CSV export: [bold green]{saved}[/bold green]")
    except Exception as e:
        console.print(f"[bold red]❌ CSV export failed: {e}[/bold red]")

    console.print("\n[bold green]✅ Analysis complete![/bold green]\n")


@main.command("check-pmm")
@click.option("--config", "-c", "config_path", required=True,
              type=click.Path(exists=True), help="Path to config.yaml")
def check_pmm(config_path):
    """Validate PMM/Prometheus connectivity and list available nodes & services."""
    console.print("\n[bold cyan]🔍 Checking PMM connectivity...[/bold cyan]\n")

    try:
        config = load_config(config_path)
    except Exception as e:
        console.print(f"[bold red]❌ Config error: {e}[/bold red]")
        sys.exit(1)

    console.print(f"   URL:  [bold]{config.pmm.prometheus_url}[/bold]")
    console.print(f"   Auth: [bold]{config.pmm.auth_method}[/bold]")
    console.print(f"   User: [bold]{config.pmm.username}[/bold]")

    try:
        pmm = PmmClient(config.pmm)
        ok, detail_msg = pmm.check_connectivity()
        if ok:
            console.print(f"\n[bold green]✅ PMM connectivity OK![/bold green] ({detail_msg})")

            # List available nodes
            console.print("\n[dim]Querying available node_name labels...[/dim]")
            try:
                result = pmm.instant_query('count by (node_name) (up)')
                if result.get("data", {}).get("result"):
                    found_nodes = [r["metric"].get("node_name", "?") for r in result["data"]["result"]]
                    console.print(f"\n📋 Found [bold]{len(found_nodes)}[/bold] PMM nodes:")
                    for n in sorted(found_nodes):
                        console.print(f"   • {n}")
            except Exception:
                pass

            # List MySQL services
            console.print("\n[dim]Querying available service_name labels...[/dim]")
            try:
                result = pmm.instant_query('count by (service_name) (mysql_up)')
                if result.get("data", {}).get("result"):
                    services = [r["metric"].get("service_name", "?") for r in result["data"]["result"]]
                    console.print(f"\n🐬 Found [bold]{len(services)}[/bold] MySQL services:")
                    for s in sorted(services):
                        console.print(f"   • {s}")
            except Exception:
                pass

        else:
            console.print(f"\n[bold red]❌ PMM connectivity FAILED[/bold red]")
            console.print(f"   {detail_msg}")
            sys.exit(1)
    except Exception as e:
        console.print(f"\n[bold red]❌ PMM connection error: {e}[/bold red]")
        sys.exit(1)


@main.command()
@click.option("--config", "-c", "config_path", required=True,
              type=click.Path(exists=True), help="Path to config.yaml")
@click.option("--instance", "-n", "target_node", required=True,
              help="Node label to collect metrics from")
@click.option("--days", "-d", default=90, help="Days of data to collect (default: 90)")
@click.option("--format", "output_format", default="json",
              type=click.Choice(["json", "text"]), help="Output format")
def collect(config_path, target_node, days, output_format):
    """Collect and dump raw PMM metrics for a specific node (debug/exploration)."""

    console.print(f"\n[bold cyan]📡 Collecting metrics for {target_node}...[/bold cyan]\n")

    try:
        config = load_config(config_path)
    except Exception as e:
        console.print(f"[bold red]❌ Config error: {e}[/bold red]")
        sys.exit(1)

    node = next(
        (n for n in config.nodes
         if n.label == target_node
         or target_node.lower() in n.label.lower()
         or target_node.lower() in n.aws_instance_id.lower()),
        None,
    )
    if not node:
        console.print(f"[bold red]❌ Node '{target_node}' not found in config[/bold red]")
        sys.exit(1)

    pmm = PmmClient(config.pmm)
    end_dt = datetime.datetime.now(datetime.timezone.utc)
    start_dt = end_dt - datetime.timedelta(days=days)

    queries = {
        "cpu_per_core": f'rate(node_cpu_seconds_total{{node_name="{node.pmm_node_name}", mode!="idle"}}[5m]) * 100',
        "context_switches": f'rate(node_context_switches_total{{node_name="{node.pmm_node_name}"}}[5m])',
        "procs_running": f'node_procs_running{{node_name="{node.pmm_node_name}"}}',
        "mem_available": f'node_memory_MemAvailable_bytes{{node_name="{node.pmm_node_name}"}}',
        "mysqld_rss": f'process_resident_memory_bytes{{service_name="{node.service_name}"}}',
        "sbm": f'mysql_slave_status_seconds_behind_master{{service_name="{node.service_name}"}}',
        "threads_running": f'mysql_global_status_threads_running{{service_name="{node.service_name}"}}',
        "disk_write_latency": (
            f'rate(node_disk_write_time_seconds_total{{node_name="{node.pmm_node_name}", '
            f'device="{config.disk.data_device}"}}[5m]) / '
            f'rate(node_disk_writes_completed_total{{node_name="{node.pmm_node_name}", '
            f'device="{config.disk.data_device}"}}[5m]) * 1000'
        ),
        "fs_free": (
            f'node_filesystem_free_bytes{{node_name="{node.pmm_node_name}", '
            f'mountpoint="{config.disk.data_mountpoint}"}}'
        ),
    }

    results = {}
    for name, promql in queries.items():
        console.print(f"  📊 {name}...")
        try:
            summary = pmm.get_metric_summary(promql, start_dt, end_dt, step="15m")
            results[name] = {
                "avg": round(summary.avg, 4),
                "p50": round(summary.p50, 4),
                "p95": round(summary.p95, 4),
                "p99": round(summary.p99, 4),
                "max": round(summary.max, 4),
                "min": round(summary.min, 4),
                "samples": summary.samples,
            }
        except Exception as e:
            results[name] = {"error": str(e)}

    if output_format == "json":
        console.print_json(json.dumps(results, indent=2))
    else:
        for name, data in results.items():
            console.print(f"\n[bold]{name}[/bold]:")
            if "error" in data:
                console.print(f"  [red]Error: {data['error']}[/red]")
            else:
                for k, v in data.items():
                    console.print(f"  {k}: {v}")

    console.print("\n[bold green]✅ Collection complete![/bold green]\n")


if __name__ == "__main__":
    main()

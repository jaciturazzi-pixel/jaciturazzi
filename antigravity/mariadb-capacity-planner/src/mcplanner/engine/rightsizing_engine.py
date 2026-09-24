from __future__ import annotations

from typing import List, Tuple

from mcplanner.config import Config, DOWNSIZE_MAP, GEN_UPGRADE_MAP, EC2_INSTANCE_SPECS, NodeEntry
from mcplanner.models import (
    Ec2Info,
    CpuAnalysisResult,
    MemoryAnalysisResult,
    DiskAnalysisResult,
    ReplicationAnalysisResult,
    RunwayResult,
    RightsizingRecommendation,
    Confidence,
    VetoReason,
)


class RightsizingEngine:
    """Decision-making core that aggregates analysis results into a final recommendation."""

    def __init__(self, config: Config):
        self.config = config
        self.cpu_thresholds = config.thresholds.cpu
        self.pricing = config.pricing

    def evaluate(
        self,
        node: NodeEntry,
        ec2_info: Ec2Info,
        cpu: CpuAnalysisResult,
        memory: MemoryAnalysisResult,
        disk: DiskAnalysisResult,
        replication: ReplicationAnalysisResult,
        runway: RunwayResult,
    ) -> RightsizingRecommendation:
        # 1. Aggregate vetoes
        all_vetoes: list[VetoReason] = []
        all_veto_details: dict[str, str] = {}

        for analyzer_vetoes, analyzer_details in [
            (cpu.vetoes, cpu.veto_details),
            (memory.vetoes, memory.veto_details),
            (replication.vetoes, replication.veto_details),
        ]:
            if analyzer_vetoes:
                all_vetoes.extend(analyzer_vetoes)
            if analyzer_details:
                all_veto_details.update(analyzer_details)

        # Remove duplicates from vetoes while preserving order
        all_vetoes = list(dict.fromkeys(all_vetoes))

        # 2. EC2 downsizing decision
        current_type = ec2_info.instance_type
        recommended_type = current_type
        status = "OK"

        if all_vetoes:
            status = "VETOED"
        else:
            downsize_cond = (
                cpu.projected_aggregate_max_pct <= self.cpu_thresholds.projected_max_after_downsize
                and cpu.hottest_core_p95_pct < self.cpu_thresholds.per_core_saturation_pct
            )
            if downsize_cond:
                candidate_type = DOWNSIZE_MAP.get(current_type)
                if candidate_type and candidate_type in EC2_INSTANCE_SPECS:
                    candidate_ram = EC2_INSTANCE_SPECS[candidate_type]["memory_gb"]
                    # Uses the buffer-pool-re-tuned projection (see MemoryAnalyzer), not the
                    # raw current RSS — the buffer pool is a knob that gets resized with RAM.
                    required_ram = memory.projected_rss_after_downsize_gb + memory.session_ceiling_gb
                    if required_ram <= candidate_ram:
                        recommended_type = candidate_type

        # Gen upgrade check (r6i/r6in -> r8i)
        if recommended_type == current_type and current_type in GEN_UPGRADE_MAP:
            recommended_type = GEN_UPGRADE_MAP[current_type]

        # Get instance specs
        rec_specs = EC2_INSTANCE_SPECS.get(recommended_type, {})
        recommended_vcpus = rec_specs.get("vcpus", ec2_info.vcpus)
        recommended_memory_gb = rec_specs.get("memory_gb", ec2_info.memory_gb)

        # 3. Cost calculation
        prices = self.pricing.ec2_hourly.prices
        current_hourly = prices.get(current_type, 0.0)
        recommended_hourly = prices.get(recommended_type, 0.0)
        hours_per_month = self.pricing.hours_per_month

        ec2_savings = (current_hourly - recommended_hourly) * hours_per_month

        # EBS savings: if verdict is MIGRATE_GP3, compute io2→gp3 cost delta
        ebs_savings = disk.monthly_savings_usd
        if disk.verdict.value == "migrate_gp3" and disk.volume_type == "io2" and ebs_savings == 0.0:
            ebs_pricing = self.pricing.ebs
            io2_cost = (disk.size_gb * ebs_pricing.io2_per_gb) + (disk.provisioned_iops * ebs_pricing.io2_per_iops)
            gp3_storage = disk.size_gb * ebs_pricing.gp3_per_gb
            gp3_iops_extra = max(0, disk.recommended_gp3_iops - ebs_pricing.gp3_base_iops)
            gp3_iops_cost = gp3_iops_extra * ebs_pricing.gp3_per_extra_iops
            gp3_throughput_extra = max(0, disk.recommended_gp3_throughput_mbps - ebs_pricing.gp3_base_throughput_mbps)
            gp3_throughput_cost = gp3_throughput_extra * ebs_pricing.gp3_per_extra_throughput_mbps
            gp3_cost = gp3_storage + gp3_iops_cost + gp3_throughput_cost
            ebs_savings = max(0.0, io2_cost - gp3_cost)
            disk.monthly_savings_usd = round(ebs_savings, 2)

        total_monthly = ec2_savings + ebs_savings
        total_annual = total_monthly * 12

        # 4. Confidence score
        # Collect samples to evaluate data density
        samples_list = [
            cpu.context_switches_per_sec.samples,
            memory.mysqld_rss_gb.samples,
            disk.read_iops.samples,
        ]
        if replication.is_replica:
            samples_list.append(replication.sbm.samples)

        min_samples = min(samples_list) if samples_list else 0

        if all_vetoes:
            confidence = Confidence.LOW
        elif min_samples < 50:
            confidence = Confidence.LOW
        elif min_samples > 100 and cpu.projected_aggregate_max_pct < 40.0:
            confidence = Confidence.HIGH
        else:
            confidence = Confidence.MEDIUM

        # 5. Populate recommendation
        return RightsizingRecommendation(
            node_label=node.label,
            cluster=node.cluster,
            role=node.role,
            aws_instance_id=node.aws_instance_id,
            current_ec2_type=current_type,
            current_vcpus=ec2_info.vcpus,
            current_memory_gb=ec2_info.memory_gb,
            recommended_ec2_type=recommended_type,
            recommended_vcpus=recommended_vcpus,
            recommended_memory_gb=recommended_memory_gb,
            cpu=cpu,
            memory=memory,
            disk=disk,
            replication=replication,
            runway=runway,
            all_vetoes=all_vetoes,
            all_veto_details=all_veto_details,
            confidence=confidence,
            ec2_monthly_savings_usd=ec2_savings,
            ebs_monthly_savings_usd=ebs_savings,
            total_monthly_savings_usd=total_monthly,
            total_annual_savings_usd=total_annual,
            status=status,
            error_msg=""
        )

    def evaluate_batch(
        self,
        nodes: list[tuple[
            NodeEntry,
            Ec2Info,
            CpuAnalysisResult,
            MemoryAnalysisResult,
            DiskAnalysisResult,
            ReplicationAnalysisResult,
            RunwayResult
        ]]
    ) -> list[RightsizingRecommendation]:
        """Processes multiple nodes and returns recommendations sorted by cluster then node label."""
        results = []
        for node_tuple in nodes:
            res = self.evaluate(*node_tuple)
            results.append(res)
        
        # Sort by cluster, then by node label
        results.sort(key=lambda x: (x.cluster, x.node_label))
        return results

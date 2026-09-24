from __future__ import annotations

import logging
import re
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from mcplanner.config import AwsConfig, PricingConfig, EC2_INSTANCE_SPECS
from mcplanner.models import Ec2Info, EbsVolumeInfo


logger = logging.getLogger(__name__)


class AwsClient:
    """AWS metadata collector via boto3."""

    def __init__(self, config: AwsConfig):
        self.config = config
        self.session = boto3.Session(profile_name=self.config.profile, region_name=self.config.region)
        self._ec2_client = None

    @property
    def ec2_client(self) -> Any:
        if self._ec2_client is None:
            self._ec2_client = self.session.client("ec2")
        return self._ec2_client

    def get_instance_info(self, instance_id: str) -> Ec2Info:
        """Calls describe_instances, extracts instance type, name tag, cluster tag."""
        try:
            response = self.ec2_client.describe_instances(InstanceIds=[instance_id])
        except (BotoCoreError, ClientError) as e:
            logger.error(f"Failed to describe instance {instance_id}: {e}")
            raise

        instances = response.get("Reservations", [])
        if not instances:
            raise ValueError(f"Instance {instance_id} not found.")

        instance = instances[0]["Instances"][0]
        instance_type = instance.get("InstanceType", "")

        tags = {t["Key"]: t["Value"] for t in instance.get("Tags", [])}
        name_tag = tags.get("Name", "")
        cluster_tag = tags.get("cluster", "")

        specs = EC2_INSTANCE_SPECS.get(instance_type, {})
        vcpus = specs.get("vcpus", 0)
        cores = specs.get("cores", 0)
        memory_gb = specs.get("memory_gb", 0.0)

        # Fallback if not in specs config
        if not vcpus:
            vcpus = instance.get("CpuOptions", {}).get("CoreCount", 0) * instance.get("CpuOptions", {}).get("ThreadsPerCore", 1)
        if not cores:
            cores = instance.get("CpuOptions", {}).get("CoreCount", 0)

        return Ec2Info(
            instance_id=instance_id,
            instance_type=instance_type,
            vcpus=vcpus,
            physical_cores=cores,
            memory_gb=memory_gb,
            name_tag=name_tag,
            cluster_tag=cluster_tag
        )

    def get_all_volumes(self, instance_id: str) -> list[EbsVolumeInfo]:
        """Returns ALL attached volumes without filtering."""
        try:
            response = self.ec2_client.describe_volumes(
                Filters=[{"Name": "attachment.instance-id", "Values": [instance_id]}]
            )
        except (BotoCoreError, ClientError) as e:
            logger.error(f"Failed to describe volumes for instance {instance_id}: {e}")
            raise

        volumes = []
        for vol in response.get("Volumes", []):
            vol_id = vol.get("VolumeId", "")
            vol_type = vol.get("VolumeType", "")
            size_gb = vol.get("Size", 0)
            iops = vol.get("Iops", 0)
            throughput = vol.get("Throughput", 0)

            # Find the attachment device
            device = ""
            for attachment in vol.get("Attachments", []):
                if attachment.get("InstanceId") == instance_id:
                    device = attachment.get("Device", "")
                    break

            volumes.append(EbsVolumeInfo(
                volume_id=vol_id,
                device=device,
                volume_type=vol_type,
                size_gb=size_gb,
                provisioned_iops=iops,
                provisioned_throughput_mbps=throughput
            ))

        return volumes

    def get_data_volumes(self, instance_id: str, data_device: str = "xvdh") -> list[EbsVolumeInfo]:
        """Filters to only data volumes (matching device name pattern, io2 type, or size >= 100GB, excluding boot)."""
        volumes = self.get_all_volumes(instance_id)
        data_volumes = []
        
        # Typically boot volumes are /dev/xvda or /dev/sda1
        boot_pattern = re.compile(r"^/dev/(xvda\d*|sda\d*)$")
        
        for vol in volumes:
            if boot_pattern.match(vol.device):
                continue
                
            if (
                data_device in vol.device or 
                vol.volume_type == "io2" or 
                vol.size_gb >= 100
            ):
                data_volumes.append(vol)

        return data_volumes

    def calculate_ebs_savings(self, current_volume: EbsVolumeInfo, target_iops: int, pricing: PricingConfig) -> float:
        """Calculates monthly savings from io2 -> gp3 migration."""
        if current_volume.volume_type != "io2":
            return 0.0

        # Current io2 cost
        current_cost = (current_volume.size_gb * pricing.ebs.io2_per_gb) + (current_volume.provisioned_iops * pricing.ebs.io2_per_iops)

        # Target gp3 cost
        gp3_storage_cost = current_volume.size_gb * pricing.ebs.gp3_per_gb
        gp3_iops_cost = 0.0
        if target_iops > pricing.ebs.gp3_base_iops:
            gp3_iops_cost = (target_iops - pricing.ebs.gp3_base_iops) * pricing.ebs.gp3_per_extra_iops
        
        # Throughput cost (default to base)
        gp3_throughput_cost = 0.0 
        
        target_cost = gp3_storage_cost + gp3_iops_cost + gp3_throughput_cost
        
        savings = current_cost - target_cost
        return max(0.0, savings)

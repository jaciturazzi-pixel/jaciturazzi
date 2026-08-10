import boto3
import datetime
import json
import csv
import argparse
import os
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_NOTES_DIR = "/Users/jaci.turazzi/Documents/Backup Macbook M1 IXC/Anotações/Projetos/RigthSizing"

PRICES_EC2_HOURLY = {
    'r8i.16xlarge': 4.170,
    'r6i.16xlarge': 4.032,
    'r6in.16xlarge': 4.608,
    'r8i.8xlarge': 2.085,
    'r6i.8xlarge': 2.016,
    'r8i.4xlarge': 1.0425,
    'r8i.2xlarge': 0.52125,
    'm8i.2xlarge': 0.384,
    'm8i.xlarge': 0.192,
}

def parse_tags_arg(tag_arg):
    filters = [{'Name': 'instance-state-name', 'Values': ['running']}]
    if not tag_arg:
        return filters

    if isinstance(tag_arg, str) and tag_arg.strip().startswith('['):
        try:
            parsed_json = json.loads(tag_arg)
            for item in parsed_json:
                key = item.get('Key', '')
                values = item.get('Values', [])
                if key and values:
                    name = key if key.startswith('tag:') else f'tag:{key}'
                    filters.append({'Name': name, 'Values': values})
            return filters
        except Exception:
            pass

    tag_items = tag_arg if isinstance(tag_arg, list) else [tag_arg]
    for item in tag_items:
        if isinstance(item, str) and '=' in item:
            k, v = item.split('=', 1)
            name = k if k.startswith('tag:') else f'tag:{k}'
            filters.append({'Name': name, 'Values': [v]})
        elif isinstance(item, str):
            filters.append({'Name': 'tag:Name', 'Values': [f'*{item}*']})
    
    return filters

def calculate_optimal_period(start_time: datetime.datetime, end_time: datetime.datetime, force_fine_period: bool = False) -> int:
    total_seconds = (end_time - start_time).total_seconds()
    if force_fine_period and total_seconds <= 432000:
        return 300
    
    candidate_periods = [300, 900, 1800, 3600, 7200, 14400, 86400]
    for p in candidate_periods:
        if (total_seconds / p) <= 1400:
            return p
    return 86400

def get_metric_stats(cloudwatch, namespace, metric_name, dimensions, start_time, end_time, force_fine_period: bool = False):
    period = calculate_optimal_period(start_time, end_time, force_fine_period)
    try:
        resp = cloudwatch.get_metric_statistics(
            Namespace=namespace, MetricName=metric_name, Dimensions=dimensions,
            StartTime=start_time, EndTime=end_time, Period=period, ExtendedStatistics=['p95'], Statistics=['Maximum', 'Average']
        )
        datapoints = resp.get('Datapoints', [])
        if not datapoints:
            return {'max': 0.0, 'p95': 0.0, 'avg': 0.0}
        
        max_val = max(dp.get('Maximum', 0.0) for dp in datapoints)
        avg_val = sum(dp.get('Average', 0.0) for dp in datapoints) / len(datapoints)
        p95_vals = [dp.get('ExtendedStatistics', {}).get('p95', 0.0) for dp in datapoints]
        p95_vals.sort()
        p95_val = p95_vals[int(len(p95_vals)*0.95)] if p95_vals else 0.0
        
        return {'max': max_val, 'p95': p95_val, 'avg': avg_val}
    except Exception:
        return {'max': 0.0, 'p95': 0.0, 'avg': 0.0}

def get_ebs_io_stats(cloudwatch, volume_id, start_time, end_time, period=300):
    total_seconds = (end_time - start_time).total_seconds()
    if (total_seconds / period) > 1400:
        period = calculate_optimal_period(start_time, end_time)

    try:
        dimensions = [{'Name': 'VolumeId', 'Value': volume_id}]
        resp_r = cloudwatch.get_metric_statistics(Namespace='AWS/EBS', MetricName='VolumeReadOps', Dimensions=dimensions, StartTime=start_time, EndTime=end_time, Period=period, Statistics=['Sum'])
        resp_w = cloudwatch.get_metric_statistics(Namespace='AWS/EBS', MetricName='VolumeWriteOps', Dimensions=dimensions, StartTime=start_time, EndTime=end_time, Period=period, Statistics=['Sum'])
        resp_rb = cloudwatch.get_metric_statistics(Namespace='AWS/EBS', MetricName='VolumeReadBytes', Dimensions=dimensions, StartTime=start_time, EndTime=end_time, Period=period, Statistics=['Sum'])
        resp_wb = cloudwatch.get_metric_statistics(Namespace='AWS/EBS', MetricName='VolumeWriteBytes', Dimensions=dimensions, StartTime=start_time, EndTime=end_time, Period=period, Statistics=['Sum'])

        dps_r = {dp['Timestamp']: dp['Sum'] for dp in resp_r.get('Datapoints', [])}
        dps_w = {dp['Timestamp']: dp['Sum'] for dp in resp_w.get('Datapoints', [])}
        dps_rb = {dp['Timestamp']: dp['Sum'] for dp in resp_rb.get('Datapoints', [])}
        dps_wb = {dp['Timestamp']: dp['Sum'] for dp in resp_wb.get('Datapoints', [])}

        all_timestamps = set(dps_r.keys()).union(set(dps_w.keys()))

        if not all_timestamps:
            return {'peak_iops': 0.0, 'p95_iops': 0.0, 'read_mb': 0.0, 'write_mb': 0.0}

        iops_series = []
        for ts in all_timestamps:
            r_ops = dps_r.get(ts, 0.0)
            w_ops = dps_w.get(ts, 0.0)
            total_iops = (r_ops + w_ops) / float(period)
            iops_series.append(total_iops)

        iops_series.sort()
        peak_iops = iops_series[-1] if iops_series else 0.0
        p95_iops = iops_series[int(len(iops_series)*0.95)] if iops_series else 0.0

        max_rb = max(dps_rb.values()) if dps_rb else 0.0
        max_wb = max(dps_wb.values()) if dps_wb else 0.0
        read_mb = max_rb / (float(period) * 1024.0 * 1024.0)
        write_mb = max_wb / (float(period) * 1024.0 * 1024.0)

        return {
            'peak_iops': round(peak_iops, 2),
            'p95_iops': round(p95_iops, 2),
            'read_mb': round(read_mb, 2),
            'write_mb': round(write_mb, 2)
        }
    except Exception as e:
        print(f"  ⚠️ CloudWatch EBS error for {volume_id}: {e}")
        return {'peak_iops': 0.0, 'p95_iops': 0.0, 'read_mb': 0.0, 'write_mb': 0.0}

def safe_float(val, default=0.0):
    try:
        return float(val)
    except (ValueError, TypeError):
        return default

def safe_int(val, default=0):
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return default

def get_mariadb_metrics_via_ssm(ssm_client, instance_id):
    sql_cmd = """mysql -u root -s -N -e "SELECT CONCAT_WS(';', @@hostname, ROUND(@@innodb_buffer_pool_size/(1024*1024*1024),2), CAST((SELECT VARIABLE_VALUE FROM INFORMATION_SCHEMA.GLOBAL_STATUS WHERE VARIABLE_NAME='Innodb_buffer_pool_pages_data') AS UNSIGNED), CAST((SELECT VARIABLE_VALUE FROM INFORMATION_SCHEMA.GLOBAL_STATUS WHERE VARIABLE_NAME='Innodb_buffer_pool_pages_total') AS UNSIGNED), ROUND((SELECT VARIABLE_VALUE FROM INFORMATION_SCHEMA.GLOBAL_STATUS WHERE VARIABLE_NAME='Innodb_buffer_pool_pages_dirty')*@@innodb_page_size/(1024*1024*1024),2), ROUND((1-((SELECT VARIABLE_VALUE FROM INFORMATION_SCHEMA.GLOBAL_STATUS WHERE VARIABLE_NAME='Innodb_buffer_pool_reads')/NULLIF((SELECT VARIABLE_VALUE FROM INFORMATION_SCHEMA.GLOBAL_STATUS WHERE VARIABLE_NAME='Innodb_buffer_pool_read_requests'),0)))*100,3), CAST((SELECT VARIABLE_VALUE FROM INFORMATION_SCHEMA.GLOBAL_STATUS WHERE VARIABLE_NAME='Innodb_buffer_pool_wait_free') AS UNSIGNED), CAST((SELECT VARIABLE_VALUE FROM INFORMATION_SCHEMA.GLOBAL_STATUS WHERE VARIABLE_NAME='Threads_running') AS UNSIGNED), CAST((SELECT VARIABLE_VALUE FROM INFORMATION_SCHEMA.GLOBAL_STATUS WHERE VARIABLE_NAME='Threads_connected') AS UNSIGNED), CAST((SELECT VARIABLE_VALUE FROM INFORMATION_SCHEMA.GLOBAL_STATUS WHERE VARIABLE_NAME='Max_used_connections') AS UNSIGNED), CAST((SELECT VARIABLE_VALUE FROM INFORMATION_SCHEMA.GLOBAL_STATUS WHERE VARIABLE_NAME='Innodb_row_lock_waits') AS UNSIGNED), @@version);" """
    
    try:
        resp = ssm_client.send_command(
            InstanceIds=[instance_id],
            DocumentName='AWS-RunShellScript',
            Parameters={'commands': [sql_cmd]}
        )
        command_id = resp['Command']['CommandId']
        time.sleep(2.5)
        
        invocation = ssm_client.get_command_invocation(CommandId=command_id, InstanceId=instance_id)
        out = invocation.get('StandardOutputContent', '').strip()
        
        if out and ';' in out:
            parts = out.split(';')
            bp_cfg_gb = safe_float(parts[1]) if len(parts) > 1 else 0.0
            pages_data = safe_int(parts[2]) if len(parts) > 2 else 0
            pages_total = safe_int(parts[3]) if len(parts) > 3 else 0
            
            if pages_total > 0:
                occupancy_ratio = min(1.0, float(pages_data) / float(pages_total))
                bp_used_gb = round(occupancy_ratio * bp_cfg_gb, 2)
            else:
                bp_used_gb = 0.0

            return {
                'hostname': parts[0] if len(parts) > 0 and parts[0] else instance_id,
                'bp_config_gb': bp_cfg_gb,
                'bp_usado_gb': bp_used_gb,
                'bp_dirty_gb': safe_float(parts[4]) if len(parts) > 4 else 0.0,
                'hit_ratio_pct': safe_float(parts[5]) if len(parts) > 5 else 0.0,
                'wait_free_pages': safe_int(parts[6]) if len(parts) > 6 else 0,
                'threads_running': safe_int(parts[7]) if len(parts) > 7 else 0,
                'threads_connected': safe_int(parts[8]) if len(parts) > 8 else 0,
                'max_used_connections': safe_int(parts[9]) if len(parts) > 9 else 0,
                'row_lock_waits': safe_int(parts[10]) if len(parts) > 10 else 0,
                'mariadb_version': parts[11] if len(parts) > 11 and parts[11] else '10.11'
            }
    except Exception as e:
        print(f"  ⚠️ SSM Unavailable for {instance_id}: {e}")
    return None

def is_main_data_volume(vol_details: dict) -> bool:
    attachments = vol_details.get('Attachments', [])
    if not attachments:
        return False
    
    device_name = attachments[0].get('Device', '').lower()

    if not device_name.startswith('/dev/'):
        return False

    if any(boot_dev in device_name for boot_dev in ['xvda', 'sda1', 'sda']):
        return False

    is_target_dev = any(target in device_name for target in ['xvdh', 'xvdf', 'sdh', 'sdf', 'nvme1', 'nvme2'])
    is_io2 = vol_details.get('VolumeType') == 'io2'
    is_large_data = vol_details.get('Size', 0) >= 100

    return is_target_dev or is_io2 or is_large_data

def print_senior_dba_dashboard(node_res):
    name = node_res.get('name', 'Unknown')
    inst_id = node_res.get('instance_id', 'Unknown')
    curr_type = node_res.get('current_type', 'Unknown')
    rec_type = node_res.get('recommended_type', 'Unknown')
    cluster = node_res.get('cluster', 'Unknown')
    db = node_res.get('db_internal')

    print("\n" + "═" * 95)
    print(f"🖥️  SENIOR DBA TECHNICAL INSPECTION PANEL: {name} ({cluster})")
    print("═" * 95)
    print(f"📌 Instance ID: {inst_id}  |  Current EC2 Type: {curr_type}")
    
    if node_res.get('status') == 'ERROR':
        print(f"⚠️  NODE STATUS: COLLECTION ERROR ({node_res.get('error_msg', 'Instance Stopped or Invalid SSM State')})")
        print("═" * 95)
        return

    if db and isinstance(db, dict):
        version = db.get('mariadb_version', '10.11')
        hostname = db.get('hostname', inst_id)
        print(f"🐬 MariaDB Version: {version}  |  Internal Hostname: {hostname}")
    
    print("\n🧠 --- INNODB BUFFER POOL & MEMORY HEALTH ---")
    if db and isinstance(db, dict) and ('bp_config_gb' in db or 'buffer_pool_config_gb' in db):
        bp_cfg = db.get('bp_config_gb', db.get('buffer_pool_config_gb', 0.0))
        bp_used = db.get('bp_usado_gb', db.get('buffer_pool_usado_gb', 0.0))
        bp_dirty = db.get('bp_dirty_gb', 0.0)
        hit_ratio = db.get('hit_ratio_pct', db.get('buffer_pool_hit_ratio_pct', 0.0))
        wait_free = db.get('wait_free_pages', 0)

        dirty_pct = round((bp_dirty / max(1.0, bp_cfg)) * 100, 2)
        used_pct = round((bp_used / max(1.0, bp_cfg)) * 100, 2)
        print(f"   • Configured Buffer Pool: {bp_cfg} GB")
        print(f"   • Real Used Buffer Pool:  {bp_used} GB ({used_pct}% occupied)")
        print(f"   • Dirty Pages:            {bp_dirty} GB ({dirty_pct}%)")
        print(f"   • Buffer Pool Hit Ratio:  {hit_ratio}%  (DBA Target > 99.5%)")
        print(f"   • Wait Free Pages:        {wait_free}  (If > 0, RAM memory pressure lock)")
    else:
        print("   ⚠️ (Internal MariaDB metrics not collected for this node. SSM access unavailable or instance stopped)")

    print("\n⚡ --- THREAD CONCURRENCY & CONNECTION HEALTH ---")
    if db and isinstance(db, dict) and ('threads_running' in db or 'max_used_connections' in db):
        print(f"   • Threads Running (Active vCPUs): {db.get('threads_running', 0)}  (Queries processing in current millisecond)")
        print(f"   • Threads Connected:             {db.get('threads_connected', 0)}  (Open connections)")
        print(f"   • Max Used Connections (Peak):   {db.get('max_used_connections', 0)}")
        print(f"   • InnoDB Row Lock Waits:         {db.get('row_lock_waits', 0)}")
    else:
        print("   ⚠️ (Run with --use-ssm and an active instance to collect thread metrics)")

    print("\n📊 --- CPU PROCESSING & HEADROOM CAPACITY (HIGH RESOLUTION) ---")
    print(f"   • Average CPU (30d):                {node_res.get('cpu_30d_avg', 0.0)}%")
    print(f"   • P95 CPU (30d):                    {node_res.get('cpu_30d_p95', 0.0)}%")
    print(f"   • 5-Min Peak CPU (3d):              {node_res.get('cpu_5min_peak_max', 0.0)}%  (Captures fast cron/routine spikes)")
    print(f"   • 1-Hour Max CPU (30d):             {node_res.get('cpu_30d_max', 0.0)}%")
    if node_res.get('cpu_peak_max', 0.0) > 0:
        print(f"   • Year-End Peak CPU (2025):         {node_res['cpu_peak_max']}%  (Black Friday / Holiday Peak 2025)")
    else:
        print(f"   • Year-End Peak CPU (2025):         ℹ️  Instance launched in 2026 (No datapoints in 2025)")

    print(f"   • Absolute Max CPU Evaluated:       {node_res.get('highest_cpu_max', 0.0)}%")
    print(f"   • Projected Downsized Max CPU:      {node_res.get('projected_cpu_max', 0.0)}%  (Half vCPUs under worst peak)")
    print(f"   • 🟢 FREE INTEL VCPU HEADROOM AT PEAK: {node_res.get('intel_headroom_left', 0.0)}%  (Guaranteed spare vCPU headroom)")

    print("\n💾 --- MARIADB MAIN DATA VOLUME (Strict Filter: /dev/xvdh, /dev/xvdf, /dev/sdh, /dev/sdf) ---")
    for vol in node_res.get('volumes', []):
        v_id = vol.get('volume_id', 'N/A')
        v_type = vol.get('volume_type', 'N/A')
        v_size = vol.get('size_gb', 0)
        dev_name = vol.get('device', 'N/A')
        prov_iops = vol.get('provisioned_iops', 0)
        peak_iops = vol.get('peak_used_iops', 0.0)
        p95_iops = vol.get('p95_used_iops', 0.0)
        read_mb = vol.get('peak_read_mb', 0.0)
        write_mb = vol.get('peak_write_mb', 0.0)

        print(f"   • Data Volume `{v_id}` (Device: {dev_name} | Type: {v_type} {v_size} GB):")
        print(f"     - Provisioned IOPS:   {prov_iops} IOPS")
        print(f"     - 30d Peak Used IOPS: {vol.get('peak_used_iops_30d', peak_iops)} IOPS (P95: {p95_iops} IOPS)")
        print(f"     - Year-End Peak IOPS: {peak_iops} IOPS")
        print(f"     - Peak Throughput:    {read_mb:.2f} MB/s Read | {write_mb:.2f} MB/s Write")
        if v_type == 'io2':
            target_iops = max(4000, int(peak_iops * 1.4))
            print(f"     👉 Storage Proposal: Migrate `io2` ➡️ `gp3` provisioned at {target_iops} IOPS (Savings: ${vol.get('monthly_ebs_savings', 0.0):.2f}/mo)")
        else:
            print(f"     👉 Storage Status: Volume is already {v_type} (No migration required)")

    print("\n💵 --- FINANCIAL DIAGNOSIS & INTEL DOWNSIZING PROPOSAL ---")
    print(f"   • Current Instance:  {curr_type}")
    print(f"   • Proposed Intel:    {rec_type}  (8th Gen Intel Xeon)")
    print(f"   • EC2 Monthly Savings:      ${node_res.get('ec2_monthly_savings', 0.0):.2f} USD / month")
    print(f"   • Storage Monthly Savings:  ${node_res.get('ebs_monthly_savings', 0.0):.2f} USD / month")
    print(f"   🟢 TOTAL SERVER SAVINGS:    ${node_res.get('total_monthly_savings', 0.0):.2f} USD / month (${node_res.get('total_monthly_savings', 0.0)*12:.2f} USD / year)")
    print("═" * 95)

def print_final_consolidated_summary_table(results):
    results.sort(key=lambda x: x.get('name', ''))

    print("\n" + "═" * 168)
    print("📊 FINAL CONSOLIDATED SUMMARY - MARIADB CAPACITY, PERFORMANCE & DOWNSIZING RECOMMENDATIONS")
    print("═" * 168)
    
    header = f"| {'Instance':<28} | {'Current EC2':<13} | {'Proposed Intel':<20} | {'Peak CPU%':<9} | {'Proj CPU%':<9} | {'Data Volume':<12} | {'Prov IOPS':<10} | {'Peak Used IOPS':<15} | {'Storage Recommendation':<22} | {'EC2 Savings':<12} | {'EBS Savings':<12} | {'Total Savings ($/mo)':<18} |"
    print(header)
    print("|" + "-"*30 + "|" + "-"*15 + "|" + "-"*22 + "|" + "-"*11 + "|" + "-"*11 + "|" + "-"*14 + "|" + "-"*12 + "|" + "-"*17 + "|" + "-"*24 + "|" + "-"*14 + "|" + "-"*14 + "|" + "-"*20 + "|")

    total_savings = 0.0
    total_ec2_savings = 0.0
    total_ebs_savings = 0.0

    for r in results:
        name = r.get('name', 'Unknown')[:28]
        curr_t = r.get('current_type', 'Unknown')
        
        if r.get('status') == 'ERROR':
            rec_t = "⚠️ ERROR (Instance/SSM)"
            cpu_peak = "N/A"
            cpu_proj = "N/A"
            main_vol_str = "N/A"
            prov_iops_str = "N/A"
            peak_iops_str = "N/A"
            storage_rec = "N/A"
            ec2_sav_str = "$0.00"
            ebs_sav_str = "$0.00"
            savings_str = "$0.00"
        else:
            rec_t = r.get('recommended_type', 'Unknown')[:20]
            cpu_peak = f"{r.get('highest_cpu_max', 0.0):.1f}%"
            cpu_proj = f"{r.get('projected_cpu_max', 0.0):.1f}%"

            if r.get('volumes'):
                v = r['volumes'][0]
                main_vol_str = f"{v.get('size_gb', 0)}GB {v.get('volume_type', '')}"
                prov_iops_str = f"{v.get('provisioned_iops', 0)} IOPS"
                peak_iops_str = f"{v.get('peak_used_iops', 0.0):.1f} IOPS"
                
                if v.get('volume_type') == 'io2':
                    target_iops = max(4000, int(v.get('peak_used_iops', 0) * 1.4))
                    storage_rec = f"gp3 ({target_iops} IOPS)"
                else:
                    storage_rec = f"Keep {v.get('volume_type', 'gp3')}"
            else:
                main_vol_str = "N/A"
                prov_iops_str = "N/A"
                peak_iops_str = "N/A"
                storage_rec = "N/A"

            ec2_sav = r.get('ec2_monthly_savings', 0.0)
            ebs_sav = r.get('ebs_monthly_savings', 0.0)
            tot_sav = r.get('total_monthly_savings', 0.0)

            ec2_sav_str = f"${ec2_sav:,.2f}"
            ebs_sav_str = f"${ebs_sav:,.2f}"
            savings_str = f"${tot_sav:,.2f}"

            total_ec2_savings += ec2_sav
            total_ebs_savings += ebs_sav
            total_savings += tot_sav

        print(f"| {name:<28} | {curr_t:<13} | {rec_t:<20} | {cpu_peak:<9} | {cpu_proj:<9} | {main_vol_str:<12} | {prov_iops_str:<10} | {peak_iops_str:<15} | {storage_rec:<22} | {ec2_sav_str:<12} | {ebs_sav_str:<12} | {savings_str:<18} |")

    print("═" * 168)
    print(f"🏆 TOTAL EVALUATED INSTANCES: {len(results)}")
    print(f"💵 ESTIMATED EC2 MONTHLY SAVINGS:     ${total_ec2_savings:,.2f} USD / month")
    print(f"💵 ESTIMATED STORAGE MONTHLY SAVINGS: ${total_ebs_savings:,.2f} USD / month")
    print(f"💵 TOTAL ESTIMATED MONTHLY SAVINGS:   ${total_savings:,.2f} USD / month")
    print(f"💵 TOTAL ESTIMATED ANNUAL SAVINGS:    ${total_savings*12:,.2f} USD / year")
    print("═" * 168 + "\n")

def save_markdown_report(results, profile_name, total_monthly_savings_all, notes_dir):
    results.sort(key=lambda x: x.get('name', ''))
    now_str = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    filename = f"{now_str}.md"
    
    os.makedirs(notes_dir, exist_ok=True)
    target_path = os.path.join(notes_dir, filename)

    with open(target_path, 'w', encoding='utf-8') as f:
        f.write(f"# 🏆 MariaDB Sizing & Cost Optimization Report ({now_str})\n\n")
        f.write(f"**Execution Date**: `{now_str}` | **AWS Profile**: `{profile_name}`\n")
        f.write(f"**Architecture Directive**: 100% **Intel Xeon** (`r8i` / `m8i`) | **Storage**: `io2` ➡️ `gp3`\n\n")
        f.write("---\n\n")
        f.write("## 💵 Executive Financial Summary\n\n")
        f.write(f"- 🟢 **Estimated Monthly Savings**: **`${total_monthly_savings_all:,.2f} USD / month`**\n")
        f.write(f"- 🟢 **Estimated Annual Savings**: **`${total_monthly_savings_all*12:,.2f} USD / year`**\n\n")
        f.write("---\n\n")
        f.write("## 📋 Consolidated Instance Assessment & Recommendations\n\n")
        f.write("| Instance | Current EC2 | Proposed Intel | Peak CPU% | Proj CPU% | Data Volume | Prov IOPS | Peak Used IOPS | Storage Recommendation | EC2 Savings | EBS Savings | Total Monthly Savings |\n")
        f.write("| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |\n")
        for r in results:
            if r.get('status') == 'ERROR':
                f.write(f"| {r.get('name')} | {r.get('current_type')} | `⚠️ ERROR (Instance/SSM)` | N/A | N/A | N/A | N/A | N/A | N/A | $0.00 | $0.00 | **$0.00** |\n")
            else:
                v_str = "N/A"
                prov_iops_str = "N/A"
                peak_iops_str = "N/A"
                storage_rec = "N/A"

                if r.get('volumes'):
                    v = r['volumes'][0]
                    v_str = f"{v.get('size_gb', 0)}GB {v.get('volume_type', '')}"
                    prov_iops_str = f"{v.get('provisioned_iops', 0)} IOPS"
                    peak_iops_str = f"{v.get('peak_used_iops', 0.0):.1f} IOPS"

                    if v.get('volume_type') == 'io2':
                        target_iops = max(4000, int(v.get('peak_used_iops', 0) * 1.4))
                        storage_rec = f"`gp3 ({target_iops} IOPS)`"
                    else:
                        storage_rec = f"Keep {v.get('volume_type', 'gp3')}"

                f.write(f"| {r.get('name')} | {r.get('current_type')} | `{r.get('recommended_type')}` | {r.get('highest_cpu_max', 0.0)}% | {r.get('projected_cpu_max', 0.0)}% | {v_str} | {prov_iops_str} | **{peak_iops_str}** | {storage_rec} | ${r.get('ec2_monthly_savings', 0.0):.2f} | ${r.get('ebs_monthly_savings', 0.0):.2f} | **${r.get('total_monthly_savings', 0.0):.2f}/mo** |\n")
        
        f.write("\n---\n\n")
        f.write("## 🔍 Technical Details Per Instance\n\n")
        for r in results:
            f.write(f"### 🖥️ {r.get('name')} ({r.get('cluster')})\n")
            f.write(f"- **Instance ID**: `{r.get('instance_id')}`\n")
            if r.get('status') == 'ERROR':
                f.write(f"- ⚠️ **Status**: `COLLECTION ERROR ({r.get('error_msg', 'Instance in invalid state or SSM unavailable')})`\n\n")
                continue
            f.write(f"- **EC2 Type**: `{r.get('current_type')}` ➡️ **Proposed**: `{r.get('recommended_type')}`\n")
            f.write(f"- **CPU Metrics**: 5-Min Peak (3d): {r.get('cpu_5min_peak_max', 0.0)}% | 30d Max: {r.get('cpu_30d_max', 0.0)}% | Year-End Peak 2025: {r.get('cpu_peak_max', 0.0)}%\n")
            f.write(f"- **Downsize Projection**: Projected Max CPU: {r.get('projected_cpu_max', 0.0)}% | Free Headroom: **{r.get('intel_headroom_left', 0.0)}%**\n")
            
            db = r.get('db_internal')
            if db and isinstance(db, dict):
                bp_cfg = db.get('bp_config_gb', db.get('buffer_pool_config_gb', 'N/A'))
                bp_used = db.get('bp_usado_gb', db.get('buffer_pool_usado_gb', 'N/A'))
                hit_ratio = db.get('hit_ratio_pct', db.get('buffer_pool_hit_ratio_pct', 'N/A'))
                f.write(f"- **Internal MariaDB**: Buffer Pool: Config {bp_cfg} GB / Used {bp_used} GB | Hit Ratio: {hit_ratio}%\n")
            else:
                f.write(f"- **Internal MariaDB**: Metrics not collected via SSM\n")

            if r.get('volumes'):
                v = r['volumes'][0]
                f.write(f"- **Main Data Volume**: Device `{v.get('device', 'N/A')}` | {v.get('size_gb', 0)}GB {v.get('volume_type', 'N/A')} ({v.get('provisioned_iops', 0)} IOPS prov / {v.get('peak_used_iops', 0.0)} IOPS peak)\n")
            f.write(f"- **Savings**: EC2: ${r.get('ec2_monthly_savings', 0.0):.2f}/mo | Storage: ${r.get('ebs_monthly_savings', 0.0):.2f}/mo | **Total: ${r.get('total_monthly_savings', 0.0):.2f}/month**\n\n")

    print(f"📝 Markdown report successfully saved to:\n   ➡️  {target_path}")

def run_master_assessment(
    profile_name="miniclippool-databases",
    region_name="us-west-2",
    use_ssm=False,
    target_instance=None,
    target_tag=None,
    days=30,
    start_date=None,
    end_date=None,
    notes_dir=DEFAULT_NOTES_DIR
):
    print("🚀 STARTING MARIADB CLUSTER CAPACITY & DOWNSIZING ASSESSMENT...")
    if use_ssm:
        print("🔐 AWS SSM Enabled! Querying MariaDB internally via `aws ssm send-command`...")

    session = boto3.Session(profile_name=profile_name, region_name=region_name)
    ec2 = session.client('ec2')
    cloudwatch = session.client('cloudwatch')
    ssm = session.client('ssm') if use_ssm else None

    if start_date and end_date:
        start_time_custom = datetime.datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=datetime.timezone.utc)
        end_time_custom = datetime.datetime.strptime(end_date, "%Y-%m-%d").replace(hour=23, minute=59, second=59, tzinfo=datetime.timezone.utc)
    else:
        end_time_custom = datetime.datetime.now(datetime.timezone.utc)
        start_time_custom = end_time_custom - datetime.timedelta(days=days)

    start_peak = datetime.datetime(2025, 11, 1, 0, 0, 0, tzinfo=datetime.timezone.utc)
    end_peak = datetime.datetime(2025, 12, 31, 23, 59, 59, tzinfo=datetime.timezone.utc)

    start_fine_3d = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=3)
    end_fine_3d = datetime.datetime.now(datetime.timezone.utc)

    inventory = []

    if target_tag:
        ec2_filters = parse_tags_arg(target_tag)
    elif target_instance:
        ec2_filters = [{'Name': 'instance-state-name', 'Values': ['running']}, {'Name': 'tag:Name', 'Values': [f'*{target_instance}*']}]
    else:
        ec2_filters = [{'Name': 'instance-state-name', 'Values': ['running']}, {'Name': 'tag:Name', 'Values': ['*prod-sql*']}]

    resp = ec2.describe_instances(Filters=ec2_filters)
    for r in resp['Reservations']:
        for inst in r['Instances']:
            inst_tags = {t['Key']: t['Value'] for t in inst.get('Tags', [])}
            cluster_val = inst_tags.get('mc:service', inst_tags.get('Cluster', inst_tags.get('Name', '').split('0')[0]))
            inventory.append({
                'instance_id': inst['InstanceId'],
                'name': inst_tags.get('Name', inst['InstanceId']),
                'instance_type': inst['InstanceType'],
                'cluster': cluster_val
            })

    if not inventory and os.path.exists(os.path.join(BASE_DIR, "cluster_inventory.json")):
        with open(os.path.join(BASE_DIR, "cluster_inventory.json"), 'r') as f:
            all_nodes = json.load(f)
            if target_instance:
                inventory = [i for i in all_nodes if target_instance.lower() in i['name'].lower() or target_instance.lower() in i['instance_id'].lower()]
            else:
                inventory = all_nodes

    if not inventory:
        print(f"⚠️ No instances found for the specified filters.")
        return

    manual_db_file = os.path.join(BASE_DIR, "manual_db_inputs.json")
    manual_db_data = {}
    if os.path.exists(manual_db_file):
        try:
            with open(manual_db_file, 'r') as f:
                manual_db_data = json.load(f)
        except Exception:
            pass

    results = []
    total_monthly_savings_all = 0.0

    print(f"📊 Processing {len(inventory)} instance(s) in {region_name}...")

    for idx, node in enumerate(inventory, 1):
        inst_id = node['instance_id']
        name = node['name']
        cluster = node.get('cluster', 'Unknown')
        curr_type = node['instance_type']

        try:
            cpu_recent = get_metric_stats(cloudwatch, 'AWS/EC2', 'CPUUtilization', [{'Name': 'InstanceId', 'Value': inst_id}], start_time_custom, end_time_custom)
            cpu_peak = get_metric_stats(cloudwatch, 'AWS/EC2', 'CPUUtilization', [{'Name': 'InstanceId', 'Value': inst_id}], start_peak, end_peak)
            cpu_5min_fine = get_metric_stats(cloudwatch, 'AWS/EC2', 'CPUUtilization', [{'Name': 'InstanceId', 'Value': inst_id}], start_fine_3d, end_fine_3d, force_fine_period=True)

            highest_cpu_max = max(cpu_recent['max'], cpu_peak['max'], cpu_5min_fine['max'])

            db_internal = None
            if use_ssm:
                db_internal = get_mariadb_metrics_via_ssm(ssm, inst_id)
            
            if not db_internal and name in manual_db_data:
                db_internal = manual_db_data[name]

            volumes = []
            node_ebs_savings = 0.0
            try:
                vol_resp = ec2.describe_volumes(Filters=[{'Name': 'attachment.instance-id', 'Values': [inst_id]}])
                all_vols = vol_resp.get('Volumes', [])
                
                main_data_vols = [v for v in all_vols if is_main_data_volume(v)]
                if not main_data_vols:
                    main_data_vols = sorted(all_vols, key=lambda x: x.get('Size', 0), reverse=True)[:1]

                for v in main_data_vols:
                    v_id = v['VolumeId']
                    v_type = v['VolumeType']
                    size_gb = v.get('Size', 0)
                    prov_iops = v.get('Iops', 0)
                    attachments = v.get('Attachments', [])
                    dev_name = attachments[0].get('Device', 'N/A') if attachments else 'N/A'

                    ebs_peak_stats = get_ebs_io_stats(cloudwatch, v_id, start_peak, end_peak)
                    ebs_30d_stats = get_ebs_io_stats(cloudwatch, v_id, start_time_custom, end_time_custom)

                    peak_iops = max(ebs_peak_stats['peak_iops'], ebs_30d_stats['peak_iops'])
                    p95_iops = max(ebs_peak_stats['p95_iops'], ebs_30d_stats['p95_iops'])
                    peak_read_mb = max(ebs_peak_stats['read_mb'], ebs_30d_stats['read_mb'])
                    peak_write_mb = max(ebs_peak_stats['write_mb'], ebs_30d_stats['write_mb'])

                    v_savings = 0.0
                    if v_type == 'io2':
                        curr_cost = (size_gb * 0.125) + (prov_iops * 0.065)
                        target_iops = max(4000, int(peak_iops * 1.4))
                        extra_iops = max(0, target_iops - 3000)
                        new_cost = (size_gb * 0.08) + (extra_iops * 0.005)
                        v_savings = curr_cost - new_cost
                        node_ebs_savings += v_savings

                    volumes.append({
                        'volume_id': v_id,
                        'device': dev_name,
                        'volume_type': v_type,
                        'size_gb': size_gb,
                        'provisioned_iops': prov_iops,
                        'peak_used_iops': round(peak_iops, 2),
                        'peak_used_iops_30d': round(ebs_30d_stats['peak_iops'], 2),
                        'p95_used_iops': round(p95_iops, 2),
                        'peak_read_mb': round(peak_read_mb, 2),
                        'peak_write_mb': round(peak_write_mb, 2),
                        'monthly_ebs_savings': round(v_savings, 2)
                    })
            except Exception:
                pass

            proj_downsized_cpu_max = highest_cpu_max * 2.0
            headroom = 100.0 - proj_downsized_cpu_max

            ec2_savings = 0.0
            rec_ec2_type = f"Keep {curr_type} (Intel)"

            if proj_downsized_cpu_max <= 50.0 and highest_cpu_max <= 25.0:
                family, size = curr_type.split('.')[0], curr_type.split('.')[1]
                intel_fam = "r8i" if "r" in family else "m8i"
                
                if "16xlarge" in size:
                    smaller = "8xlarge"
                elif "8xlarge" in size:
                    smaller = "4xlarge"
                elif "4xlarge" in size:
                    smaller = "2xlarge"
                elif "2xlarge" in size:
                    smaller = "xlarge"
                else:
                    smaller = size
                
                rec_ec2_type = f"{intel_fam}.{smaller}"
                curr_h = PRICES_EC2_HOURLY.get(curr_type, 4.0)
                new_h = PRICES_EC2_HOURLY.get(rec_ec2_type, curr_h / 2.0)
                ec2_savings = (curr_h - new_h) * 730

            total_node_savings = node_ebs_savings + ec2_savings
            total_monthly_savings_all += total_node_savings

            node_result = {
                'cluster': cluster,
                'name': name,
                'instance_id': inst_id,
                'current_type': curr_type,
                'recommended_type': rec_ec2_type,
                'status': 'OK',
                'highest_cpu_max': round(highest_cpu_max, 2),
                'cpu_30d_avg': round(cpu_recent['avg'], 2),
                'cpu_30d_p95': round(cpu_recent['p95'], 2),
                'cpu_30d_max': round(cpu_recent['max'], 2),
                'cpu_5min_peak_max': round(cpu_5min_fine['max'], 2),
                'cpu_peak_max': round(cpu_peak['max'], 2),
                'projected_cpu_max': round(proj_downsized_cpu_max, 2),
                'intel_headroom_left': round(headroom, 2),
                'ebs_monthly_savings': round(node_ebs_savings, 2),
                'ec2_monthly_savings': round(ec2_savings, 2),
                'total_monthly_savings': round(total_node_savings, 2),
                'db_internal': db_internal,
                'volumes': volumes
            }
            results.append(node_result)
            print_senior_dba_dashboard(node_result)
        
        except Exception as err:
            print(f"  ❌ Error collecting metrics for instance {name} ({inst_id}): {err}")
            err_node = {
                'cluster': cluster,
                'name': name,
                'instance_id': inst_id,
                'current_type': curr_type,
                'recommended_type': '⚠️ COLLECTION ERROR',
                'status': 'ERROR',
                'error_msg': str(err),
                'highest_cpu_max': 0.0,
                'projected_cpu_max': 0.0,
                'intel_headroom_left': 0.0,
                'ebs_monthly_savings': 0.0,
                'ec2_monthly_savings': 0.0,
                'total_monthly_savings': 0.0,
                'db_internal': None,
                'volumes': []
            }
            results.append(err_node)
            print_senior_dba_dashboard(err_node)

    print_final_consolidated_summary_table(results)
    save_markdown_report(results, profile_name, total_monthly_savings_all, notes_dir)

    csv_file = os.path.join(BASE_DIR, "metricas_consolidadas_30instancias.csv")
    with open(csv_file, 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(["Instance Name", "Instance ID", "Current Type", "Intel Recommendation", "Status", "Absolute Peak CPU %", "30d Max CPU %", "5min Peak CPU %", "Year-End Peak CPU %", "Projected CPU %", "Free Intel Headroom %", "Data Device", "Size GB", "Provisioned IOPS", "Peak Used IOPS", "EBS Savings ($/mo)", "EC2 Savings ($/mo)", "Total Monthly Savings ($/mo)", "Total Annual Savings ($/yr)"])
        results.sort(key=lambda x: x.get('name', ''))
        for r in results:
            v_dev = r['volumes'][0]['device'] if r.get('volumes') else "N/A"
            v_size = r['volumes'][0]['size_gb'] if r.get('volumes') else "N/A"
            v_prov = r['volumes'][0]['provisioned_iops'] if r.get('volumes') else "N/A"
            v_peak = r['volumes'][0]['peak_used_iops'] if r.get('volumes') else "N/A"

            w.writerow([
                r.get('name'), r.get('instance_id'), r.get('current_type'), r.get('recommended_type'),
                r.get('status', 'OK'), r.get('highest_cpu_max', 0.0), r.get('cpu_30d_max', 0.0), r.get('cpu_5min_peak_max', 0.0), r.get('cpu_peak_max', 0.0), r.get('projected_cpu_max', 0.0), r.get('intel_headroom_left', 0.0),
                v_dev, v_size, v_prov, v_peak,
                r.get('ebs_monthly_savings', 0.0), r.get('ec2_monthly_savings', 0.0), r.get('total_monthly_savings', 0.0), round(r.get('total_monthly_savings', 0.0)*12, 2)
            ])

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Unified Master Tool for MariaDB AWS Capacity & Downsizing Assessment.")
    parser.add_argument("--profile", default="miniclippool-databases", help="AWS CLI Profile")
    parser.add_argument("--region", default="us-west-2", help="AWS Region")
    parser.add_argument("--instance", default=None, help="Specific Instance Name/ID (e.g., prod-sql-secureinventory002)")
    parser.add_argument("--tag", action="append", default=None, help="AWS Tag Filter (e.g., mc:service=secureinventory or JSON)")
    parser.add_argument("--days", type=int, default=30, help="Recent days to query")
    parser.add_argument("--start-date", "--start-data", dest="start_date", default=None, help="Start Date (YYYY-MM-DD)")
    parser.add_argument("--end-date", "--end-data", dest="end_date", default=None, help="Start Date (YYYY-MM-DD)")
    parser.add_argument("--use-ssm", action="store_true", help="Enable AWS SSM to query MariaDB internally")
    parser.add_argument("--output-dir", default=DEFAULT_NOTES_DIR, help="Directory to save timestamped Markdown report")
    args = parser.parse_args()

    run_master_assessment(
        profile_name=args.profile,
        region_name=args.region,
        use_ssm=args.use_ssm,
        target_instance=args.instance,
        target_tag=args.tag,
        days=args.days,
        start_date=args.start_date,
        end_date=args.end_date,
        notes_dir=args.output_dir
    )

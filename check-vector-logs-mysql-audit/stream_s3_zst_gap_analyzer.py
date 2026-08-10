#!/usr/bin/env python3
import argparse
import io
import json
import re
from datetime import datetime
from typing import Callable, Iterator, Optional

import boto3
import zstandard
from botocore.exceptions import BotoCoreError, ClientError


DEFAULT_BUCKET = "prod-singularity-pool-databases"
DEFAULT_PREFIX = "mysqlaudit/year=2026/month=06/"
DEFAULT_SERVICE_FILTER = "service=secureinventory"
DEFAULT_HOST_FILTER = "host=prod-sql-secureinventory002"
DEFAULT_PROFILE = "8bp-infrastructure"


DATE_KEY_PATTERN = re.compile(r"year=(\d{4})/month=(\d{2})/day=(\d{2})/")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Stream .log.zst JSON logs from S3 and alert on timestamp gaps."
    )
    parser.add_argument("--bucket", default=DEFAULT_BUCKET)
    parser.add_argument("--prefix", default=DEFAULT_PREFIX)
    parser.add_argument("--service-filter", default=DEFAULT_SERVICE_FILTER)
    parser.add_argument("--host-filter", default=DEFAULT_HOST_FILTER)
    parser.add_argument("--profile", default=DEFAULT_PROFILE)
    parser.add_argument("--gap-seconds", type=int, default=60)
    parser.add_argument("--output-file", default="gap_output.log")
    return parser.parse_args()


def iter_matching_keys(
    s3_client,
    bucket: str,
    prefix: str,
    service_filter: str,
    host_filter: str,
    emit: Callable[[str], None],
) -> Iterator[str]:
    page_count = 0
    scanned_objects = 0
    matched_objects = 0
    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        page_count += 1
        contents = page.get("Contents", [])
        scanned_objects += len(contents)

        if page_count == 1 or page_count % 25 == 0:
            emit(
                f"[INFO] Listing S3 objects... pages={page_count} scanned={scanned_objects} matched={matched_objects}",
            )

        for obj in contents:
            key = obj["Key"]
            if (
                service_filter in key
                and host_filter in key
                and key.endswith(".log.zst")
            ):
                matched_objects += 1
                yield key

    emit(f"[INFO] Listing complete. pages={page_count} scanned={scanned_objects} matched={matched_objects}")


def parse_timestamp(raw_value) -> Optional[datetime]:
    if not isinstance(raw_value, str):
        return None

    value = raw_value.strip()
    if not value:
        return None

    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S.%fZ",
    ):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue

    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def extract_date_label_from_key(key: str) -> Optional[str]:
    match = DATE_KEY_PATTERN.search(key)
    if not match:
        return None

    year, month, day = match.groups()
    return f"{day}/{month}/{year}"


def analyze_file(
    s3_client,
    bucket: str,
    key: str,
    previous_timestamp: Optional[datetime],
    gap_seconds: int,
    emit: Callable[[str], None],
) -> tuple[Optional[datetime], int]:
    out_of_order = 0
    try:
        response = s3_client.get_object(Bucket=bucket, Key=key)
    except (BotoCoreError, ClientError) as exc:
        emit(f"[WARN] Failed to fetch {key}: {exc}")
        return previous_timestamp, out_of_order

    body = response["Body"]
    dctx = zstandard.ZstdDecompressor()

    with dctx.stream_reader(body) as reader:
        text_stream = io.TextIOWrapper(reader, encoding="utf-8")
        for line_number, line in enumerate(text_stream, start=1):
            if not line.strip():
                continue

            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue

            current_timestamp = parse_timestamp(payload.get("timestamp"))
            if current_timestamp is None:
                continue

            if previous_timestamp is not None:
                if current_timestamp < previous_timestamp:
                    # Skip backward timestamps to preserve ascending progression.
                    out_of_order += 1
                    continue
                gap = (current_timestamp - previous_timestamp).total_seconds()
                if gap > gap_seconds:
                    emit(
                        "[ALERT] Gap detected: "
                        f"{gap:.0f}s | prev={previous_timestamp} | curr={current_timestamp} "
                        f"| file={key} | line={line_number}"
                    )

            previous_timestamp = current_timestamp

    return previous_timestamp, out_of_order


def main() -> int:
    args = parse_args()

    with open(args.output_file, "w", encoding="utf-8") as output_file:
        def emit(message: str) -> None:
            print(message, flush=True)
            output_file.write(message + "\n")
            output_file.flush()

        session = boto3.Session(profile_name=args.profile)
        s3_client = session.client("s3")

        emit(
            f"[INFO] Starting analysis | bucket={args.bucket} | prefix={args.prefix} | "
            f"service={args.service_filter} | host={args.host_filter} | output={args.output_file}"
        )

        previous_timestamp: Optional[datetime] = None
        out_of_order_total = 0
        matched_files = 0
        current_date_label: Optional[str] = None

        for key in iter_matching_keys(
            s3_client=s3_client,
            bucket=args.bucket,
            prefix=args.prefix,
            service_filter=args.service_filter,
            host_filter=args.host_filter,
            emit=emit,
        ):
            date_label = extract_date_label_from_key(key)
            if date_label and date_label != current_date_label:
                current_date_label = date_label
                emit(f"[INFO] Analyzing date: {current_date_label}")

            matched_files += 1
            previous_timestamp, out_of_order = analyze_file(
                s3_client=s3_client,
                bucket=args.bucket,
                key=key,
                previous_timestamp=previous_timestamp,
                gap_seconds=args.gap_seconds,
                emit=emit,
            )
            out_of_order_total += out_of_order

        if matched_files == 0:
            emit("[INFO] No files matched the provided filters.")
        else:
            emit(
                f"[INFO] Finished. Processed {matched_files} file(s). "
                f"Ignored out-of-order lines: {out_of_order_total}."
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
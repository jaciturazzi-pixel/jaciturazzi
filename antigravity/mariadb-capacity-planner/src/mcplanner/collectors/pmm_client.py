from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any

import numpy as np
import requests
import urllib3
from requests.exceptions import RequestException

from mcplanner.config import PmmConfig
from mcplanner.models import MetricSummary, TimeSeriesPoint


logger = logging.getLogger(__name__)


class PmmQueryError(Exception):
    """Exception raised for PMM/Prometheus query errors."""
    pass


class PmmClient:
    """A robust HTTP client for the Prometheus API embedded in PMM Server."""

    def __init__(self, config: PmmConfig):
        self.config = config
        self.session = requests.Session()
        self.session.verify = self.config.verify_ssl
        if not self.config.verify_ssl:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        if self.config.auth_method == "basic":
            self.session.auth = (self.config.username, self.config.password)
        elif self.config.auth_method == "token" and self.config.token:
            self.session.headers.update({"Authorization": f"Bearer {self.config.token}"})

    def _request(self, endpoint: str, data: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.config.prometheus_url}/{endpoint}"
        logger.debug(f"PMM Query [{endpoint}]: {data.get('query')}")

        max_retries = 3
        backoff_factor = 2

        for attempt in range(max_retries + 1):
            try:
                response = self.session.post(url, data=data, timeout=self.config.timeout_seconds)
                response.raise_for_status()
                result = response.json()
                if result.get("status") != "success":
                    raise PmmQueryError(f"Prometheus API error: {result.get('errorType')} - {result.get('error')}")
                return result.get("data", {})
            except requests.exceptions.HTTPError as e:
                if 500 <= e.response.status_code < 600 and attempt < max_retries:
                    sleep_time = backoff_factor ** attempt
                    logger.warning(f"PMM API 5xx error. Retrying in {sleep_time}s... (Attempt {attempt + 1}/{max_retries})")
                    time.sleep(sleep_time)
                else:
                    raise PmmQueryError(f"HTTP Error querying PMM: {e}") from e
            except RequestException as e:
                if attempt < max_retries:
                    sleep_time = backoff_factor ** attempt
                    logger.warning(f"PMM API connection error. Retrying in {sleep_time}s... (Attempt {attempt + 1}/{max_retries})")
                    time.sleep(sleep_time)
                else:
                    raise PmmQueryError(f"Connection Error querying PMM: {e}") from e
        return {}

    def instant_query(self, promql: str) -> dict:
        """POST to /query with query=promql. Returns the parsed JSON result."""
        data = {"query": promql, "time": datetime.utcnow().timestamp()}
        return self._request("query", data)

    def range_query(self, promql: str, start: datetime, end: datetime, step: str = "5m") -> list[TimeSeriesPoint]:
        """POST to /query_range. Converts Prometheus matrix result into a flat list of TimeSeriesPoint."""
        data = {
            "query": promql,
            "start": start.timestamp(),
            "end": end.timestamp(),
            "step": step,
        }
        res_data = self._request("query_range", data)
        results = res_data.get("result", [])
        if not results:
            return []
        
        # Typically one metric in the results list if not grouped by label
        points = []
        for value in results[0].get("values", []):
            try:
                points.append(TimeSeriesPoint(timestamp=float(value[0]), value=float(value[1])))
            except (ValueError, TypeError):
                continue
        return points

    def range_query_per_label(self, promql: str, start: datetime, end: datetime, step: str = "5m", label: str = "cpu") -> dict[str, list[TimeSeriesPoint]]:
        """Like range_query but returns a dict keyed by the specified label value."""
        data = {
            "query": promql,
            "start": start.timestamp(),
            "end": end.timestamp(),
            "step": step,
        }
        res_data = self._request("query_range", data)
        results = res_data.get("result", [])
        
        per_label_data = {}
        for metric_data in results:
            label_value = metric_data.get("metric", {}).get(label)
            if not label_value:
                continue
                
            points = []
            for value in metric_data.get("values", []):
                try:
                    points.append(TimeSeriesPoint(timestamp=float(value[0]), value=float(value[1])))
                except (ValueError, TypeError):
                    continue
            per_label_data[label_value] = points
            
        return per_label_data

    def scalar_query(self, promql: str, time_val: datetime | float | None = None) -> float:
        """Convenience: instant_query that returns a single float value, or 0.0 if empty."""
        data: dict[str, Any] = {"query": promql}
        if isinstance(time_val, datetime):
            data["time"] = time_val.timestamp()
        elif isinstance(time_val, (int, float)):
            data["time"] = float(time_val)
        res_data = self._request("query", data)
        results = res_data.get("result", [])
        if not results:
            return 0.0
        
        try:
            val = results[0].get("value", [])
            if len(val) == 2:
                return float(val[1])
        except (ValueError, TypeError, IndexError):
            pass
        return 0.0

    def get_metric_summary(self, promql: str, start: datetime, end: datetime, step: str = "5m") -> MetricSummary:
        """Does a range_query and computes stats using numpy percentile. Returns a MetricSummary."""
        points = self.range_query(promql, start, end, step)
        if not points:
            return MetricSummary()
        
        values = np.array([p.value for p in points])
        if len(values) == 0:
            return MetricSummary()
        
        return MetricSummary(
            avg=float(np.mean(values)),
            p50=float(np.percentile(values, 50)),
            p95=float(np.percentile(values, 95)),
            p99=float(np.percentile(values, 99)),
            max=float(np.max(values)),
            min=float(np.min(values)),
            samples=len(values),
            raw=points
        )

    def check_connectivity(self) -> tuple[bool, str]:
        """GET to /query?query=up and verify response. Returns (success, detail_message)."""
        url = f"{self.config.prometheus_url}/query"
        params = {"query": "up"}
        try:
            response = self.session.get(url, params=params, timeout=self.config.timeout_seconds)
            if response.status_code == 401:
                return False, "HTTP 401 Unauthorized — Please check username and password in config.yaml"
            if response.status_code == 403:
                return False, (
                    "HTTP 403 Access Denied — PMM user has insufficient permissions for Prometheus API (/prometheus/api/v1).\n"
                    "   [yellow]👉 Action Required: In PMM Server, upgrade user role to 'Admin' / 'PMM Admin',\n"
                    "      or generate an API Token under PMM -> API Keys / Service Accounts and set 'auth_method: token' in config.yaml.[/yellow]"
                )
            response.raise_for_status()
            data = response.json()
            if data.get("status") == "success":
                return True, "Successfully connected to PMM Prometheus API"
            return False, f"Prometheus API response status: {data.get('status')}"
        except requests.exceptions.SSLError as e:
            return False, (
                f"SSL Certificate Verification Failed!\n"
                f"   [yellow]👉 PMM uses a self-signed certificate. Set 'verify_ssl: false' under 'pmm' in config.yaml.[/yellow]\n"
                f"   Error details: {e}"
            )
        except RequestException as e:
            return False, f"Connection Failed: {e}"

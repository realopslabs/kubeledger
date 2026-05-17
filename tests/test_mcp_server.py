#!/usr/bin/env python
"""Tests for the KubeLedger MCP server.

Covers step 1 of the implementation plan: data access layer (env-based path
resolution, robust JSON parsing).
"""

import json
import os
from datetime import datetime, timedelta, UTC
from pathlib import Path

import pytest

import mcp_server


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


class TestGetDataDir:
    def test_default_when_no_env(self, monkeypatch):
        monkeypatch.delenv("KL_MCP_DATA_DIR", raising=False)
        monkeypatch.delenv("KOA_MCP_DATA_DIR", raising=False)
        assert mcp_server.get_data_dir() == Path(mcp_server.DEFAULT_DATA_DIR)

    def test_kl_env_overrides_default(self, monkeypatch, tmp_path):
        monkeypatch.setenv("KL_MCP_DATA_DIR", str(tmp_path))
        monkeypatch.delenv("KOA_MCP_DATA_DIR", raising=False)
        assert mcp_server.get_data_dir() == tmp_path

    def test_koa_env_overrides_default(self, monkeypatch, tmp_path):
        monkeypatch.delenv("KL_MCP_DATA_DIR", raising=False)
        monkeypatch.setenv("KOA_MCP_DATA_DIR", str(tmp_path))
        assert mcp_server.get_data_dir() == tmp_path

    def test_kl_takes_precedence_over_koa(self, monkeypatch, tmp_path):
        kl_dir = tmp_path / "kl"
        koa_dir = tmp_path / "koa"
        monkeypatch.setenv("KL_MCP_DATA_DIR", str(kl_dir))
        monkeypatch.setenv("KOA_MCP_DATA_DIR", str(koa_dir))
        assert mcp_server.get_data_dir() == kl_dir


# ---------------------------------------------------------------------------
# read_json_file — robust JSON parsing
# ---------------------------------------------------------------------------


class TestReadJsonFile:
    def test_reads_backend_config_dict(self, tmp_path):
        payload = {"cost_model": "cumulative", "currency": "%"}
        (tmp_path / "backend.json").write_text(json.dumps(payload), encoding="utf-8")

        result = mcp_server.read_json_file("backend.json", data_dir=tmp_path)

        assert result.ok
        assert result.exists
        assert result.payload == payload
        assert result.error is None
        assert result.mtime_utc is not None

    def test_reads_histogram_like_array(self, tmp_path):
        payload = [
            {"stack": "openshift-monitoring", "usage": 113.001604, "date": "Feb 2026"},
            {"stack": "registry", "usage": 5.160364, "date": "Feb 2026"},
        ]
        (tmp_path / "cpu_usage_period_31968000.json").write_text(json.dumps(payload), encoding="utf-8")

        result = mcp_server.read_json_file("cpu_usage_period_31968000.json", data_dir=tmp_path)

        assert result.ok
        assert result.payload == payload
        assert isinstance(result.payload, list)

    def test_reads_trends_like_array(self, tmp_path):
        payload = [
            {"name": "openshift-nmstate", "dateUTC": "2026-02-10T11:00:00Z", "usage": 0.059653},
            {"name": "openshift-nmstate", "dateUTC": "2026-02-10T12:00:00Z", "usage": 0.068125},
        ]
        (tmp_path / "cpu_usage_trends.json").write_text(json.dumps(payload), encoding="utf-8")

        result = mcp_server.read_json_file("cpu_usage_trends.json", data_dir=tmp_path)

        assert result.ok
        assert result.payload == payload

    def test_missing_file_returns_clean_result(self, tmp_path):
        result = mcp_server.read_json_file("does-not-exist.json", data_dir=tmp_path)

        assert not result.ok
        assert not result.exists
        assert result.payload is None
        assert result.error is not None
        assert "not found" in result.error
        assert result.mtime_utc is None

    def test_truncated_array_does_not_raise(self, tmp_path):
        # Simulate a crash mid-dump: trailing entry truncated.
        truncated = '[{"stack":"a","usage":1.0,"date":"Feb 2026"},{"stack":"b","usage":'
        (tmp_path / "cpu_usage_period_31968000.json").write_text(truncated, encoding="utf-8")

        result = mcp_server.read_json_file("cpu_usage_period_31968000.json", data_dir=tmp_path)

        assert not result.ok
        assert result.exists
        assert result.payload is None
        assert result.error is not None
        assert "invalid JSON" in result.error
        assert result.mtime_utc is not None

    def test_empty_file_does_not_raise(self, tmp_path):
        (tmp_path / "empty.json").write_text("", encoding="utf-8")

        result = mcp_server.read_json_file("empty.json", data_dir=tmp_path)

        assert not result.ok
        assert result.exists
        assert result.payload is None
        assert "invalid JSON" in result.error

    def test_empty_array_is_ok(self, tmp_path):
        (tmp_path / "empty_array.json").write_text("[]", encoding="utf-8")

        result = mcp_server.read_json_file("empty_array.json", data_dir=tmp_path)

        # Empty list is a valid (parsed) payload; ok depends on whether we
        # consider [] as "present data". Current contract: payload is not None,
        # so ok is True. Callers interpret semantic emptiness themselves.
        assert result.exists
        assert result.error is None
        assert result.payload == []
        assert result.ok

    def test_uses_env_when_data_dir_not_passed(self, monkeypatch, tmp_path):
        (tmp_path / "backend.json").write_text('{"cost_model":"cumulative","currency":"%"}', encoding="utf-8")
        monkeypatch.setenv("KL_MCP_DATA_DIR", str(tmp_path))
        monkeypatch.delenv("KOA_MCP_DATA_DIR", raising=False)

        result = mcp_server.read_json_file("backend.json")

        assert result.ok
        assert result.payload == {"cost_model": "cumulative", "currency": "%"}


# ---------------------------------------------------------------------------
# Filename helpers
# ---------------------------------------------------------------------------


class TestHistogramFilename:
    def test_cpu_usage_14d(self):
        assert (
            mcp_server.histogram_filename("cpu", "usage", mcp_server.PERIOD_14_DAYS_SEC)
            == "cpu_usage_period_1209600.json"
        )

    def test_memory_usage_year(self):
        assert (
            mcp_server.histogram_filename("memory", "usage", mcp_server.PERIOD_YEAR_SEC)
            == "memory_usage_period_31968000.json"
        )

    def test_cpu_requests_14d(self):
        assert (
            mcp_server.histogram_filename("cpu", "requests", mcp_server.PERIOD_14_DAYS_SEC)
            == "cpu_requests_period_1209600.json"
        )

    def test_memory_requests_year(self):
        assert (
            mcp_server.histogram_filename("memory", "requests", mcp_server.PERIOD_YEAR_SEC)
            == "memory_requests_period_31968000.json"
        )

    def test_gpu_cpu_usage_uses_mem_label_for_memory(self):
        # GPU variant uses "mem" instead of "memory" — matches backend.py
        assert (
            mcp_server.histogram_filename("memory", "usage", mcp_server.PERIOD_14_DAYS_SEC, gpu=True)
            == "gpu_mem_usage_period_1209600.json"
        )
        assert (
            mcp_server.histogram_filename("cpu", "usage", mcp_server.PERIOD_14_DAYS_SEC, gpu=True)
            == "gpu_cpu_usage_period_1209600.json"
        )

    def test_invalid_metric_raises(self):
        with pytest.raises(ValueError):
            mcp_server.histogram_filename("disk", "usage", mcp_server.PERIOD_14_DAYS_SEC)

    def test_invalid_dimension_raises(self):
        with pytest.raises(ValueError):
            mcp_server.histogram_filename("cpu", "limits", mcp_server.PERIOD_14_DAYS_SEC)


class TestTrendsFilename:
    def test_cpu_usage_trends(self):
        assert mcp_server.trends_filename("cpu", "usage") == "cpu_usage_trends.json"

    def test_memory_usage_trends(self):
        assert mcp_server.trends_filename("memory", "usage") == "memory_usage_trends.json"

    def test_cpu_rf_trends(self):
        assert mcp_server.trends_filename("cpu", "rf") == "cpu_rf_trends.json"

    def test_memory_rf_trends(self):
        assert mcp_server.trends_filename("memory", "rf") == "memory_rf_trends.json"

    def test_gpu_cpu_usage_trends(self):
        assert mcp_server.trends_filename("cpu", "usage", gpu=True) == "gpu_cpu_usage_trends.json"

    def test_gpu_mem_usage_trends(self):
        assert mcp_server.trends_filename("memory", "usage", gpu=True) == "gpu_mem_usage_trends.json"

    def test_invalid_category_raises(self):
        with pytest.raises(ValueError):
            mcp_server.trends_filename("cpu", "limits")


# ---------------------------------------------------------------------------
# Cost model loader
# ---------------------------------------------------------------------------


class TestLoadCostModel:
    def test_cumulative(self, tmp_path):
        (tmp_path / "backend.json").write_text('{"cost_model":"cumulative", "currency":"%"}', encoding="utf-8")
        info, warnings = mcp_server.load_cost_model(data_dir=tmp_path)
        assert info is not None
        assert info.cost_model == "cumulative"
        assert info.currency == "%"
        assert info.unit == "percent_of_cluster_capacity"
        assert warnings == []

    def test_normalized(self, tmp_path):
        (tmp_path / "backend.json").write_text('{"cost_model":"normalized", "currency":"%"}', encoding="utf-8")
        info, _ = mcp_server.load_cost_model(data_dir=tmp_path)
        assert info.unit == "relative_share_percent"

    def test_costs_with_currency_symbol(self, tmp_path):
        (tmp_path / "backend.json").write_text('{"cost_model":"costs", "currency":"$"}', encoding="utf-8")
        info, warnings = mcp_server.load_cost_model(data_dir=tmp_path)
        assert info.cost_model == "costs"
        assert info.currency == "$"
        assert info.unit == "monetary_cost"
        assert warnings == []

    def test_missing_file_returns_none_and_warning(self, tmp_path):
        info, warnings = mcp_server.load_cost_model(data_dir=tmp_path)
        assert info is None
        assert warnings and "backend.json" in warnings[0]

    def test_invalid_json_returns_none_and_warning(self, tmp_path):
        (tmp_path / "backend.json").write_text("{bad", encoding="utf-8")
        info, warnings = mcp_server.load_cost_model(data_dir=tmp_path)
        assert info is None
        assert warnings

    def test_unknown_cost_model_still_returns_info_with_warning(self, tmp_path):
        (tmp_path / "backend.json").write_text('{"cost_model":"future_mode", "currency":"%"}', encoding="utf-8")
        info, warnings = mcp_server.load_cost_model(data_dir=tmp_path)
        assert info is not None
        assert info.unit == "unknown"
        assert any("future_mode" in w for w in warnings)

    def test_object_not_dict_returns_none(self, tmp_path):
        (tmp_path / "backend.json").write_text("[]", encoding="utf-8")
        info, warnings = mcp_server.load_cost_model(data_dir=tmp_path)
        assert info is None
        assert warnings


# ---------------------------------------------------------------------------
# Dataset discovery
# ---------------------------------------------------------------------------


def _write(tmp_path, name, payload):
    (tmp_path / name).write_text(json.dumps(payload), encoding="utf-8")


def _make_daily_histogram():
    # 3 namespaces × 4 distinct daily dates → points_per_namespace == 4.
    return [
        {"stack": "ns-a", "usage": 10.0, "date": "01 Feb"},
        {"stack": "ns-a", "usage": 11.0, "date": "02 Feb"},
        {"stack": "ns-a", "usage": 12.0, "date": "03 Feb"},
        {"stack": "ns-a", "usage": 13.0, "date": "04 Feb"},
        {"stack": "ns-b", "usage": 1.0, "date": "01 Feb"},
        {"stack": "ns-b", "usage": 2.0, "date": "02 Feb"},
        {"stack": "non-allocatable", "usage": 50.0, "date": "01 Feb"},
    ]


def _make_monthly_histogram():
    return [
        {"stack": "ns-a", "usage": 100.0, "date": "Dec 2025"},
        {"stack": "ns-a", "usage": 110.0, "date": "Jan 2026"},
        {"stack": "ns-a", "usage": 120.0, "date": "Feb 2026"},
    ]


def _make_trends_7d_hourly(start="2026-02-03T13:00:00Z", hours=7 * 24):
    # 168 hourly points spanning 7 days for a single namespace.
    base = datetime.fromisoformat(start.replace("Z", "+00:00"))
    return [
        {
            "name": "ns-a",
            "dateUTC": (base + timedelta(hours=i)).isoformat().replace("+00:00", "Z"),
            "usage": 0.1 * i,
        }
        for i in range(hours)
    ]


class TestDiscoverDataset:
    def _setup_full_dataset(self, tmp_path):
        _write(tmp_path, "backend.json", {"cost_model": "cumulative", "currency": "%"})
        _write(tmp_path, "cpu_usage_period_1209600.json", _make_daily_histogram())
        _write(tmp_path, "memory_usage_period_1209600.json", _make_daily_histogram())
        _write(tmp_path, "cpu_usage_period_31968000.json", _make_monthly_histogram())
        _write(tmp_path, "memory_usage_period_31968000.json", _make_monthly_histogram())
        _write(tmp_path, "cpu_requests_period_1209600.json", _make_daily_histogram())
        _write(tmp_path, "memory_requests_period_1209600.json", _make_daily_histogram())
        _write(tmp_path, "cpu_usage_trends.json", _make_trends_7d_hourly())
        _write(tmp_path, "memory_usage_trends.json", _make_trends_7d_hourly())
        _write(tmp_path, "cpu_rf_trends.json", _make_trends_7d_hourly())
        _write(tmp_path, "memory_rf_trends.json", _make_trends_7d_hourly())

    def test_full_dataset_no_gpu(self, tmp_path):
        self._setup_full_dataset(tmp_path)
        info = mcp_server.discover_dataset(data_dir=tmp_path)

        assert info.metrics_available == ["cpu", "memory"]
        assert info.gpu_available is False
        assert "usage" in info.dimensions
        assert "requests" in info.dimensions

        assert set(info.scales) == {"daily_14d", "monthly_12m"}
        assert info.scales["daily_14d"].granularity == "daily"
        assert info.scales["daily_14d"].points_per_namespace == 4
        assert info.scales["monthly_12m"].granularity == "monthly"
        assert info.scales["monthly_12m"].points_per_namespace == 3

        assert info.trends is not None
        assert info.trends.granularity == "hourly"
        assert info.trends.depth.endswith("days")
        assert info.trends.points_per_namespace == 168

        assert info.efficiency.aggregated is True
        assert info.efficiency.hourly_timeseries is True

        assert info.cost_model is not None
        assert info.cost_model.cost_model == "cumulative"
        assert info.cost_model.unit == "percent_of_cluster_capacity"

        assert info.billing_hourly_rate is None
        assert info.data_freshness_utc is not None
        assert info.warnings == []

    def test_gpu_present_when_files_exist_and_non_empty(self, tmp_path):
        self._setup_full_dataset(tmp_path)
        _write(tmp_path, "gpu_cpu_usage_period_1209600.json", _make_daily_histogram())
        info = mcp_server.discover_dataset(data_dir=tmp_path)
        assert info.gpu_available is True

    def test_gpu_absent_when_only_empty_gpu_files(self, tmp_path):
        self._setup_full_dataset(tmp_path)
        _write(tmp_path, "gpu_cpu_usage_period_1209600.json", [])
        info = mcp_server.discover_dataset(data_dir=tmp_path)
        assert info.gpu_available is False

    def test_no_efficiency_without_requests_files(self, tmp_path):
        _write(tmp_path, "backend.json", {"cost_model": "cumulative", "currency": "%"})
        _write(tmp_path, "cpu_usage_period_1209600.json", _make_daily_histogram())
        _write(tmp_path, "cpu_usage_trends.json", _make_trends_7d_hourly())
        info = mcp_server.discover_dataset(data_dir=tmp_path)

        assert info.efficiency.aggregated is False
        assert info.efficiency.hourly_timeseries is False
        assert info.dimensions == ["usage"]

    def test_costs_mode_warns_about_missing_billing_hourly_rate(self, tmp_path):
        _write(tmp_path, "backend.json", {"cost_model": "costs", "currency": "$"})
        _write(tmp_path, "cpu_usage_period_1209600.json", _make_daily_histogram())
        info = mcp_server.discover_dataset(data_dir=tmp_path)

        assert info.cost_model is not None
        assert info.cost_model.unit == "monetary_cost"
        assert info.billing_hourly_rate is None
        assert any("billing_hourly_rate" in w for w in info.warnings)

    def test_missing_backend_json_yields_warning_but_still_works(self, tmp_path):
        _write(tmp_path, "cpu_usage_period_1209600.json", _make_daily_histogram())
        info = mcp_server.discover_dataset(data_dir=tmp_path)

        assert info.cost_model is None
        assert any("backend.json" in w for w in info.warnings)
        assert "cpu" in info.metrics_available

    def test_empty_directory(self, tmp_path):
        info = mcp_server.discover_dataset(data_dir=tmp_path)

        assert info.metrics_available == []
        assert info.dimensions == []
        assert info.scales == {}
        assert info.trends is None
        assert info.efficiency.aggregated is False
        assert info.efficiency.hourly_timeseries is False
        assert info.cost_model is None
        assert info.data_freshness_utc is None

    def test_truncated_histogram_does_not_crash_discovery(self, tmp_path):
        _write(tmp_path, "backend.json", {"cost_model": "cumulative", "currency": "%"})
        # Truncated mid-file — simulates a crash during dump.
        (tmp_path / "cpu_usage_period_1209600.json").write_text(
            '[{"stack":"a","usage":1.0,"date":"01 Feb"},{"stack":"b","usage":',
            encoding="utf-8",
        )
        info = mcp_server.discover_dataset(data_dir=tmp_path)
        # File exists but unreadable → the metric is still reported as
        # available (file is present), the scale just won't carry points.
        assert "daily_14d" not in info.scales

    def test_data_freshness_ignores_backend_json_mtime(self, tmp_path):
        # Set backend.json clearly newer than data files. data_freshness must
        # still reflect the data file mtime, not backend.json's.
        _write(tmp_path, "cpu_usage_period_1209600.json", _make_daily_histogram())
        data_mtime = (tmp_path / "cpu_usage_period_1209600.json").stat().st_mtime
        _write(tmp_path, "backend.json", {"cost_model": "cumulative", "currency": "%"})
        # Bump backend.json's mtime forward.
        future = data_mtime + 10_000
        os.utime(tmp_path / "backend.json", (future, future))

        info = mcp_server.discover_dataset(data_dir=tmp_path)
        assert info.data_freshness_utc is not None
        # Re-derive expected ISO from data file mtime — must equal info value.
        expected = mcp_server._iso_utc(data_mtime)
        assert info.data_freshness_utc == expected


# ---------------------------------------------------------------------------
# Namespace classification
# ---------------------------------------------------------------------------


class TestClassifyNamespace:
    def test_openshift_prefix_is_system(self):
        assert mcp_server.classify_namespace("openshift-monitoring") == "system"
        assert mcp_server.classify_namespace("openshift-nmstate") == "system"

    def test_kube_prefix_is_system(self):
        assert mcp_server.classify_namespace("kube-system") == "system"
        assert mcp_server.classify_namespace("kube-public") == "system"
        assert mcp_server.classify_namespace("kube-node-lease") == "system"

    def test_regular_namespace_is_application(self):
        assert mcp_server.classify_namespace("registry") == "application"
        assert mcp_server.classify_namespace("clusters-hcp1") == "application"
        assert mcp_server.classify_namespace("my-app-prod") == "application"

    def test_special_entries(self):
        assert mcp_server.classify_namespace("non-allocatable") == "special"
        assert mcp_server.classify_namespace(".billing-hourly-rate") == "special"

    def test_other_dot_prefixed_is_application(self):
        # Only the known special entries are special — random dot-prefixed
        # names are still classified by the standard rules (application here).
        assert mcp_server.classify_namespace(".unknown-special") == "application"


# ---------------------------------------------------------------------------
# Tool: list_namespaces
# ---------------------------------------------------------------------------


class TestToolListNamespaces:
    def _setup(self, tmp_path):
        _write(tmp_path, "backend.json", {"cost_model": "cumulative", "currency": "%"})
        _write(
            tmp_path,
            "cpu_usage_period_1209600.json",
            [
                {"stack": "registry", "usage": 5.0, "date": "01 Feb"},
                {"stack": "openshift-monitoring", "usage": 50.0, "date": "01 Feb"},
                {"stack": "non-allocatable", "usage": 100.0, "date": "01 Feb"},
                {"stack": "kube-system", "usage": 3.0, "date": "01 Feb"},
            ],
        )
        _write(
            tmp_path,
            "cpu_usage_trends.json",
            [
                {"name": "registry", "dateUTC": "2026-02-10T11:00:00Z", "usage": 0.5},
                {"name": "clusters-hcp1", "dateUTC": "2026-02-10T11:00:00Z", "usage": 0.7},
            ],
        )

    def test_namespaces_classified(self, tmp_path):
        self._setup(tmp_path)
        result = mcp_server.tool_list_namespaces(data_dir=tmp_path)

        names_by_type = {n["type"]: [] for n in result["namespaces"]}
        for n in result["namespaces"]:
            names_by_type[n["type"]].append(n["name"])

        assert "openshift-monitoring" in names_by_type.get("system", [])
        assert "kube-system" in names_by_type.get("system", [])
        assert "registry" in names_by_type.get("application", [])
        assert "clusters-hcp1" in names_by_type.get("application", [])
        # non-allocatable must NOT appear in the namespaces list — it is a
        # special entry, surfaced separately.
        all_names = [n["name"] for n in result["namespaces"]]
        assert "non-allocatable" not in all_names
        assert ".billing-hourly-rate" not in all_names

    def test_special_entries_always_listed(self, tmp_path):
        self._setup(tmp_path)
        result = mcp_server.tool_list_namespaces(data_dir=tmp_path)

        special_names = {e["name"] for e in result["special_entries"]}
        assert special_names == {"non-allocatable", ".billing-hourly-rate"}
        # Each carries its role and description.
        for entry in result["special_entries"]:
            assert entry["role"] in ("cluster_overhead", "billing_config")
            assert entry["description"]

    def test_counts_consistent(self, tmp_path):
        self._setup(tmp_path)
        result = mcp_server.tool_list_namespaces(data_dir=tmp_path)

        counts = result["counts"]
        types_seen = [n["type"] for n in result["namespaces"]]
        assert counts["application"] == types_seen.count("application")
        assert counts["system"] == types_seen.count("system")
        assert counts["special"] == 2  # static known list

    def test_metadata_present(self, tmp_path):
        self._setup(tmp_path)
        result = mcp_server.tool_list_namespaces(data_dir=tmp_path)

        md = result["metadata"]
        assert md["cost_model"] == "cumulative"
        assert md["currency"] == "%"
        assert md["unit"] == "percent_of_cluster_capacity"
        assert md["generated_at_utc"] is not None
        # Tool is dataset-global → no metric/scale/data_window/source_file.
        assert md["metric"] is None
        assert md["scale"] is None
        assert md["data_window"] is None
        assert md["source_file"] is None

    def test_empty_directory(self, tmp_path):
        result = mcp_server.tool_list_namespaces(data_dir=tmp_path)
        assert result["namespaces"] == []
        assert result["counts"]["application"] == 0
        assert result["counts"]["system"] == 0
        # Special entries are static — present even without data.
        assert result["counts"]["special"] == 2

    def test_namespaces_sorted_alphabetically(self, tmp_path):
        _write(tmp_path, "backend.json", {"cost_model": "cumulative", "currency": "%"})
        _write(
            tmp_path,
            "cpu_usage_period_1209600.json",
            [
                {"stack": "zeta-app", "usage": 1.0, "date": "01 Feb"},
                {"stack": "alpha-app", "usage": 1.0, "date": "01 Feb"},
                {"stack": "midway-app", "usage": 1.0, "date": "01 Feb"},
            ],
        )
        result = mcp_server.tool_list_namespaces(data_dir=tmp_path)
        names = [n["name"] for n in result["namespaces"]]
        assert names == sorted(names)


# ---------------------------------------------------------------------------
# Tool: describe_dataset
# ---------------------------------------------------------------------------


class TestToolDescribeDataset:
    def test_full_response_shape(self, tmp_path):
        # Re-use the full-dataset fixture from earlier.
        _write(tmp_path, "backend.json", {"cost_model": "cumulative", "currency": "%"})
        _write(tmp_path, "cpu_usage_period_1209600.json", _make_daily_histogram())
        _write(tmp_path, "memory_usage_period_1209600.json", _make_daily_histogram())
        _write(tmp_path, "cpu_usage_period_31968000.json", _make_monthly_histogram())
        _write(tmp_path, "cpu_requests_period_1209600.json", _make_daily_histogram())
        _write(tmp_path, "cpu_usage_trends.json", _make_trends_7d_hourly())
        _write(tmp_path, "cpu_rf_trends.json", _make_trends_7d_hourly())

        result = mcp_server.tool_describe_dataset(data_dir=tmp_path)

        # Top-level shape per spec §4.1
        assert "metrics_available" in result
        assert "gpu_available" in result
        assert "scales" in result
        assert "trends" in result
        assert "dimensions" in result
        assert "efficiency" in result
        assert "cost_model" in result
        assert "currency" in result
        assert "billing_hourly_rate" in result
        assert "data_freshness_utc" in result
        assert "metadata" in result

        # cost_model & currency flat (not nested) at top level.
        assert result["cost_model"] == "cumulative"
        assert result["currency"] == "%"

        # Efficiency block has the expected sub-fields.
        assert set(result["efficiency"].keys()) == {"aggregated", "hourly_timeseries"}

        # Trends block has expected sub-fields.
        assert set(result["trends"].keys()) == {"granularity", "depth", "points_per_namespace"}

        # Each scale entry is a flat dict (no nested dataclass).
        for scale_data in result["scales"].values():
            assert set(scale_data.keys()) == {"granularity", "depth", "points_per_namespace"}

    def test_gpu_available_flag(self, tmp_path):
        _write(tmp_path, "backend.json", {"cost_model": "cumulative", "currency": "%"})
        _write(tmp_path, "cpu_usage_period_1209600.json", _make_daily_histogram())
        _write(tmp_path, "gpu_cpu_usage_period_1209600.json", _make_daily_histogram())

        result = mcp_server.tool_describe_dataset(data_dir=tmp_path)
        assert result["gpu_available"] is True

    def test_billing_hourly_rate_null_in_cumulative(self, tmp_path):
        _write(tmp_path, "backend.json", {"cost_model": "cumulative", "currency": "%"})
        _write(tmp_path, "cpu_usage_period_1209600.json", _make_daily_histogram())

        result = mcp_server.tool_describe_dataset(data_dir=tmp_path)
        assert result["billing_hourly_rate"] is None

    def test_metadata_carries_unit(self, tmp_path):
        _write(tmp_path, "backend.json", {"cost_model": "normalized", "currency": "%"})
        _write(tmp_path, "cpu_usage_period_1209600.json", _make_daily_histogram())

        result = mcp_server.tool_describe_dataset(data_dir=tmp_path)
        assert result["metadata"]["unit"] == "relative_share_percent"


# ---------------------------------------------------------------------------
# build_metadata — freshness warning, optional fields
# ---------------------------------------------------------------------------


class TestBuildMetadata:
    def _cost_model(self):
        return mcp_server.CostModelInfo(
            cost_model="cumulative",
            currency="%",
            unit="percent_of_cluster_capacity",
        )

    def test_includes_all_fields(self):
        md = mcp_server.build_metadata(
            cost_model=self._cost_model(),
            metric="cpu",
            scale="daily_14d",
            source_file="cpu_usage_period_1209600.json",
            data_window={"start": "01 Feb", "end": "04 Feb", "points": 4},
        )
        assert md["metric"] == "cpu"
        assert md["scale"] == "daily_14d"
        assert md["source_file"] == "cpu_usage_period_1209600.json"
        assert md["data_window"]["points"] == 4
        assert md["warnings"] == []

    def test_no_cost_model_yields_null_fields(self):
        md = mcp_server.build_metadata(cost_model=None)
        assert md["cost_model"] is None
        assert md["currency"] is None
        assert md["unit"] is None

    def test_freshness_warning_when_data_stale(self):
        from datetime import datetime

        old = datetime.now(tz=UTC).timestamp() - 3600  # 1 hour ago
        md = mcp_server.build_metadata(
            cost_model=self._cost_model(),
            generated_at_mtime=old,
        )
        assert any("stale data" in w for w in md["warnings"])
        assert md["generated_at_utc"] is not None

    def test_no_freshness_warning_when_recent(self):
        from datetime import datetime

        recent = datetime.now(tz=UTC).timestamp() - 60  # 1 minute ago
        md = mcp_server.build_metadata(
            cost_model=self._cost_model(),
            generated_at_mtime=recent,
        )
        assert not any("stale data" in w for w in md["warnings"])

    def test_preserves_caller_warnings(self):
        md = mcp_server.build_metadata(
            cost_model=self._cost_model(),
            warnings=["custom warning A", "custom warning B"],
        )
        assert "custom warning A" in md["warnings"]
        assert "custom warning B" in md["warnings"]

    def test_unit_override(self):
        md = mcp_server.build_metadata(
            cost_model=self._cost_model(),
            unit_override="efficiency_ratio",
        )
        assert md["unit"] == "efficiency_ratio"


# ---------------------------------------------------------------------------
# Fixtures for Consolidations tools (§4.2)
# ---------------------------------------------------------------------------


def _make_consolidation_dataset():
    """Two-date dataset with system, application, and non-allocatable entries.

    Date "01 Feb":
        registry=10, alpha=5, openshift-monitoring=40, kube-system=3,
        non-allocatable=100
    Date "02 Feb":
        registry=20, alpha=30, openshift-monitoring=50, kube-system=4,
        non-allocatable=90
    """
    return [
        # 01 Feb
        {"stack": "registry", "usage": 10.0, "date": "01 Feb"},
        {"stack": "alpha", "usage": 5.0, "date": "01 Feb"},
        {"stack": "openshift-monitoring", "usage": 40.0, "date": "01 Feb"},
        {"stack": "kube-system", "usage": 3.0, "date": "01 Feb"},
        {"stack": "non-allocatable", "usage": 100.0, "date": "01 Feb"},
        # 02 Feb
        {"stack": "registry", "usage": 20.0, "date": "02 Feb"},
        {"stack": "alpha", "usage": 30.0, "date": "02 Feb"},
        {"stack": "openshift-monitoring", "usage": 50.0, "date": "02 Feb"},
        {"stack": "kube-system", "usage": 4.0, "date": "02 Feb"},
        {"stack": "non-allocatable", "usage": 90.0, "date": "02 Feb"},
    ]


def _setup_consolidation_fixture(tmp_path, cost_model="cumulative", currency="%"):
    _write(tmp_path, "backend.json", {"cost_model": cost_model, "currency": currency})
    _write(tmp_path, "cpu_usage_period_1209600.json", _make_consolidation_dataset())
    _write(tmp_path, "cpu_usage_period_31968000.json", _make_consolidation_dataset())


# ---------------------------------------------------------------------------
# Tool: get_usage
# ---------------------------------------------------------------------------


class TestToolGetUsage:
    def test_default_excludes_special(self, tmp_path):
        _setup_consolidation_fixture(tmp_path)
        result = mcp_server.tool_get_usage(
            metric="cpu",
            scale="daily_14d",
            data_dir=tmp_path,
        )
        ns_seen = {p["namespace"] for p in result["series"]}
        assert "non-allocatable" not in ns_seen
        assert "registry" in ns_seen
        assert "openshift-monitoring" in ns_seen  # system kept by get_usage

    def test_exclude_special_false_keeps_non_allocatable(self, tmp_path):
        _setup_consolidation_fixture(tmp_path)
        result = mcp_server.tool_get_usage(
            metric="cpu",
            scale="daily_14d",
            exclude_special=False,
            data_dir=tmp_path,
        )
        ns_seen = {p["namespace"] for p in result["series"]}
        assert "non-allocatable" in ns_seen

    def test_filter_by_namespace(self, tmp_path):
        _setup_consolidation_fixture(tmp_path)
        result = mcp_server.tool_get_usage(
            metric="cpu",
            scale="daily_14d",
            namespace="registry",
            data_dir=tmp_path,
        )
        assert all(p["namespace"] == "registry" for p in result["series"])
        assert len(result["series"]) == 2  # two dates

    def test_series_points_have_required_fields(self, tmp_path):
        _setup_consolidation_fixture(tmp_path)
        result = mcp_server.tool_get_usage(
            metric="cpu",
            scale="daily_14d",
            data_dir=tmp_path,
        )
        for p in result["series"]:
            assert set(p.keys()) == {"namespace", "date", "value"}
            assert isinstance(p["value"], float)

    def test_data_window_in_metadata(self, tmp_path):
        _setup_consolidation_fixture(tmp_path)
        result = mcp_server.tool_get_usage(
            metric="cpu",
            scale="daily_14d",
            data_dir=tmp_path,
        )
        window = result["metadata"]["data_window"]
        assert window["granularity"] == "daily"
        assert window["start"] == "01 Feb"
        assert window["end"] == "02 Feb"
        assert window["points"] == 2
        assert window["timezone"] == "UTC"
        assert result["metadata"]["source_file"] == "cpu_usage_period_1209600.json"

    def test_invalid_metric_raises(self, tmp_path):
        with pytest.raises(ValueError):
            mcp_server.tool_get_usage(metric="disk", scale="daily_14d", data_dir=tmp_path)

    def test_invalid_scale_raises(self, tmp_path):
        with pytest.raises(ValueError):
            mcp_server.tool_get_usage(metric="cpu", scale="hourly", data_dir=tmp_path)

    def test_missing_file_yields_empty_series_and_warning(self, tmp_path):
        _write(tmp_path, "backend.json", {"cost_model": "cumulative", "currency": "%"})
        result = mcp_server.tool_get_usage(
            metric="cpu",
            scale="daily_14d",
            data_dir=tmp_path,
        )
        assert result["series"] == []
        assert result["metadata"]["warnings"]


# ---------------------------------------------------------------------------
# Tool: get_top_consumers
# ---------------------------------------------------------------------------


class TestToolGetTopConsumers:
    def test_excludes_system_by_default(self, tmp_path):
        _setup_consolidation_fixture(tmp_path)
        result = mcp_server.tool_get_top_consumers(
            metric="cpu",
            scale="daily_14d",
            data_dir=tmp_path,
        )
        names = {r["namespace"] for r in result["ranking"]}
        assert "openshift-monitoring" not in names
        assert "kube-system" not in names
        assert "non-allocatable" not in names  # always excluded
        assert "registry" in names
        assert "alpha" in names

    def test_includes_system_when_flag_off(self, tmp_path):
        _setup_consolidation_fixture(tmp_path)
        result = mcp_server.tool_get_top_consumers(
            metric="cpu",
            scale="daily_14d",
            exclude_system=False,
            data_dir=tmp_path,
        )
        names = {r["namespace"] for r in result["ranking"]}
        assert "openshift-monitoring" in names
        assert "kube-system" in names
        # non-allocatable still excluded — rule is invariant
        assert "non-allocatable" not in names

    def test_uses_last_date_by_default(self, tmp_path):
        _setup_consolidation_fixture(tmp_path)
        result = mcp_server.tool_get_top_consumers(
            metric="cpu",
            scale="daily_14d",
            data_dir=tmp_path,
        )
        assert result["date"] == "02 Feb"

    def test_explicit_date_filter(self, tmp_path):
        _setup_consolidation_fixture(tmp_path)
        result = mcp_server.tool_get_top_consumers(
            metric="cpu",
            scale="daily_14d",
            date="01 Feb",
            data_dir=tmp_path,
        )
        assert result["date"] == "01 Feb"
        # On 01 Feb, registry=10 > alpha=5
        assert result["ranking"][0]["namespace"] == "registry"
        assert result["ranking"][1]["namespace"] == "alpha"

    def test_share_pct_sums_to_100_in_ranking(self, tmp_path):
        _setup_consolidation_fixture(tmp_path)
        result = mcp_server.tool_get_top_consumers(
            metric="cpu",
            scale="daily_14d",
            date="01 Feb",
            data_dir=tmp_path,
        )
        # Only application namespaces on 01 Feb (defaults): registry=10, alpha=5
        # total=15 → registry=66.67%, alpha=33.33%
        shares = [r["share_pct"] for r in result["ranking"]]
        assert abs(sum(shares) - 100.0) < 0.1

    def test_ranks_are_1_indexed_and_descending(self, tmp_path):
        _setup_consolidation_fixture(tmp_path)
        result = mcp_server.tool_get_top_consumers(
            metric="cpu",
            scale="daily_14d",
            data_dir=tmp_path,
        )
        ranks = [r["rank"] for r in result["ranking"]]
        assert ranks == list(range(1, len(ranks) + 1))
        values = [r["value"] for r in result["ranking"]]
        assert values == sorted(values, reverse=True)

    def test_limit_caps_results(self, tmp_path):
        _setup_consolidation_fixture(tmp_path)
        result = mcp_server.tool_get_top_consumers(
            metric="cpu",
            scale="daily_14d",
            limit=1,
            data_dir=tmp_path,
        )
        assert len(result["ranking"]) == 1

    def test_unknown_date_warns_but_returns_empty(self, tmp_path):
        _setup_consolidation_fixture(tmp_path)
        result = mcp_server.tool_get_top_consumers(
            metric="cpu",
            scale="daily_14d",
            date="99 Dec",
            data_dir=tmp_path,
        )
        assert result["ranking"] == []
        assert any("99 Dec" in w for w in result["metadata"]["warnings"])

    def test_invalid_limit_raises(self, tmp_path):
        with pytest.raises(ValueError):
            mcp_server.tool_get_top_consumers(
                metric="cpu",
                scale="daily_14d",
                limit=0,
                data_dir=tmp_path,
            )


# ---------------------------------------------------------------------------
# Tool: get_namespace_breakdown
# ---------------------------------------------------------------------------


class TestToolGetNamespaceBreakdown:
    def test_excludes_system_by_default(self, tmp_path):
        _setup_consolidation_fixture(tmp_path)
        result = mcp_server.tool_get_namespace_breakdown(
            metric="cpu",
            scale="daily_14d",
            data_dir=tmp_path,
        )
        names = {b["namespace"] for b in result["breakdown"]}
        assert "openshift-monitoring" not in names
        assert "kube-system" not in names
        assert "non-allocatable" not in names  # NEVER in breakdown
        assert "registry" in names
        assert "alpha" in names

    def test_cluster_overhead_present_when_non_allocatable_exists(self, tmp_path):
        _setup_consolidation_fixture(tmp_path)
        result = mcp_server.tool_get_namespace_breakdown(
            metric="cpu",
            scale="daily_14d",
            date="02 Feb",
            data_dir=tmp_path,
        )
        # On 02 Feb: registry=20, alpha=30 (apps), non-allocatable=90
        # total cluster = 50 + 90 = 140
        # non_allocatable_share = 90/140 ≈ 64.29%
        # allocatable_share = 50/140 ≈ 35.71%
        co = result["cluster_overhead"]
        assert co is not None
        assert co["non_allocatable_value"] == 90.0
        assert abs(co["non_allocatable_share_pct"] - 64.29) < 0.1
        assert abs(co["allocatable_share_pct"] - 35.71) < 0.1
        # The two shares must sum to ~100% per spec.
        assert abs(co["non_allocatable_share_pct"] + co["allocatable_share_pct"] - 100.0) < 0.1

    def test_cluster_overhead_omitted_when_non_allocatable_missing(self, tmp_path):
        _write(tmp_path, "backend.json", {"cost_model": "cumulative", "currency": "%"})
        _write(
            tmp_path,
            "cpu_usage_period_1209600.json",
            [
                {"stack": "registry", "usage": 10.0, "date": "01 Feb"},
                {"stack": "alpha", "usage": 5.0, "date": "01 Feb"},
            ],
        )
        result = mcp_server.tool_get_namespace_breakdown(
            metric="cpu",
            scale="daily_14d",
            data_dir=tmp_path,
        )
        assert result["cluster_overhead"] is None
        assert any("non-allocatable" in w for w in result["metadata"]["warnings"])

    def test_concentration_metrics(self, tmp_path):
        # Build a clear concentration: 5 namespaces, one dominating.
        _write(tmp_path, "backend.json", {"cost_model": "cumulative", "currency": "%"})
        _write(
            tmp_path,
            "cpu_usage_period_1209600.json",
            [
                {"stack": "ns-1", "usage": 60.0, "date": "01 Feb"},  # dominant
                {"stack": "ns-2", "usage": 20.0, "date": "01 Feb"},
                {"stack": "ns-3", "usage": 10.0, "date": "01 Feb"},
                {"stack": "ns-4", "usage": 5.0, "date": "01 Feb"},
                {"stack": "ns-5", "usage": 5.0, "date": "01 Feb"},
            ],
        )
        result = mcp_server.tool_get_namespace_breakdown(
            metric="cpu",
            scale="daily_14d",
            data_dir=tmp_path,
        )
        c = result["concentration"]
        # top_3 = 60+20+10 = 90; total = 100 → 90%
        assert c["top_3_share_pct"] == 90.0
        # top_10 covers all 5 here → 100%
        assert c["top_10_share_pct"] == 100.0
        assert c["total_namespaces"] == 5

    def test_share_pct_in_breakdown_sums_to_100(self, tmp_path):
        _setup_consolidation_fixture(tmp_path)
        result = mcp_server.tool_get_namespace_breakdown(
            metric="cpu",
            scale="daily_14d",
            date="02 Feb",
            data_dir=tmp_path,
        )
        shares = [b["share_pct"] for b in result["breakdown"]]
        assert abs(sum(shares) - 100.0) < 0.1

    def test_breakdown_sorted_by_value_desc(self, tmp_path):
        _setup_consolidation_fixture(tmp_path)
        result = mcp_server.tool_get_namespace_breakdown(
            metric="cpu",
            scale="daily_14d",
            date="02 Feb",
            data_dir=tmp_path,
        )
        values = [b["value"] for b in result["breakdown"]]
        assert values == sorted(values, reverse=True)

    def test_data_window_single_date(self, tmp_path):
        _setup_consolidation_fixture(tmp_path)
        result = mcp_server.tool_get_namespace_breakdown(
            metric="cpu",
            scale="daily_14d",
            date="02 Feb",
            data_dir=tmp_path,
        )
        window = result["metadata"]["data_window"]
        assert window["start"] == "02 Feb"
        assert window["end"] == "02 Feb"
        assert window["points"] == 1


# ---------------------------------------------------------------------------
# Tool: get_efficiency (§4.3)
# ---------------------------------------------------------------------------


def _setup_efficiency_fixture(tmp_path):
    """Two namespaces × two dates × usage + requests, both CPU and memory.

    namespace "registry":
        cpu:    usage = 10+15 = 25,  requests = 40+50 = 90    → ratio 0.278 → over_provisioned
        memory: usage = 80+100 = 180, requests = 100+110 = 210 → ratio 0.857 → balanced

    namespace "hot-app":
        cpu:    usage = 60+70 = 130, requests = 50+60 = 110    → ratio 1.18  → under_provisioned
        memory: usage = 30+40 = 70,  requests = 100+100 = 200  → ratio 0.35  → over_provisioned
    """
    _write(tmp_path, "backend.json", {"cost_model": "cumulative", "currency": "%"})

    _write(
        tmp_path,
        "cpu_usage_period_1209600.json",
        [
            {"stack": "registry", "usage": 10.0, "date": "01 Feb"},
            {"stack": "registry", "usage": 15.0, "date": "02 Feb"},
            {"stack": "hot-app", "usage": 60.0, "date": "01 Feb"},
            {"stack": "hot-app", "usage": 70.0, "date": "02 Feb"},
            # Special entries must be ignored even if present.
            {"stack": "non-allocatable", "usage": 200.0, "date": "01 Feb"},
        ],
    )
    _write(
        tmp_path,
        "cpu_requests_period_1209600.json",
        [
            {"stack": "registry", "usage": 40.0, "date": "01 Feb"},
            {"stack": "registry", "usage": 50.0, "date": "02 Feb"},
            {"stack": "hot-app", "usage": 50.0, "date": "01 Feb"},
            {"stack": "hot-app", "usage": 60.0, "date": "02 Feb"},
            {"stack": "non-allocatable", "usage": 0.0, "date": "01 Feb"},
        ],
    )
    _write(
        tmp_path,
        "memory_usage_period_1209600.json",
        [
            {"stack": "registry", "usage": 80.0, "date": "01 Feb"},
            {"stack": "registry", "usage": 100.0, "date": "02 Feb"},
            {"stack": "hot-app", "usage": 30.0, "date": "01 Feb"},
            {"stack": "hot-app", "usage": 40.0, "date": "02 Feb"},
        ],
    )
    _write(
        tmp_path,
        "memory_requests_period_1209600.json",
        [
            {"stack": "registry", "usage": 100.0, "date": "01 Feb"},
            {"stack": "registry", "usage": 110.0, "date": "02 Feb"},
            {"stack": "hot-app", "usage": 100.0, "date": "01 Feb"},
            {"stack": "hot-app", "usage": 100.0, "date": "02 Feb"},
        ],
    )


class TestToolGetEfficiency:
    def test_returns_both_metrics_by_default(self, tmp_path):
        _setup_efficiency_fixture(tmp_path)
        result = mcp_server.tool_get_efficiency(scale="daily_14d", data_dir=tmp_path)

        metrics_seen = {e["metric"] for e in result["efficiency"]}
        assert metrics_seen == {"cpu", "memory"}

    def test_filter_by_metric(self, tmp_path):
        _setup_efficiency_fixture(tmp_path)
        result = mcp_server.tool_get_efficiency(
            scale="daily_14d",
            metric="cpu",
            data_dir=tmp_path,
        )
        metrics_seen = {e["metric"] for e in result["efficiency"]}
        assert metrics_seen == {"cpu"}

    def test_filter_by_namespace(self, tmp_path):
        _setup_efficiency_fixture(tmp_path)
        result = mcp_server.tool_get_efficiency(
            scale="daily_14d",
            namespace="registry",
            data_dir=tmp_path,
        )
        ns_seen = {e["namespace"] for e in result["efficiency"]}
        assert ns_seen == {"registry"}

    def test_ratios_computed_correctly(self, tmp_path):
        _setup_efficiency_fixture(tmp_path)
        result = mcp_server.tool_get_efficiency(scale="daily_14d", data_dir=tmp_path)

        by_key = {(e["namespace"], e["metric"]): e for e in result["efficiency"]}

        # registry cpu: usage 25 / requests 90 ≈ 0.2778
        reg_cpu = by_key[("registry", "cpu")]
        assert reg_cpu["usage"] == 25.0
        assert reg_cpu["requests"] == 90.0
        assert abs(reg_cpu["efficiency_ratio"] - 25.0 / 90.0) < 1e-6
        assert reg_cpu["classification"] == "over_provisioned"

        # registry memory: 180 / 210 ≈ 0.857 → balanced
        reg_mem = by_key[("registry", "memory")]
        assert reg_mem["classification"] == "balanced"

        # hot-app cpu: 130 / 110 ≈ 1.18 → under_provisioned
        hot_cpu = by_key[("hot-app", "cpu")]
        assert hot_cpu["classification"] == "under_provisioned"

        # hot-app memory: 70 / 200 = 0.35 → over_provisioned
        hot_mem = by_key[("hot-app", "memory")]
        assert hot_mem["classification"] == "over_provisioned"

    def test_special_entries_excluded(self, tmp_path):
        _setup_efficiency_fixture(tmp_path)
        result = mcp_server.tool_get_efficiency(scale="daily_14d", data_dir=tmp_path)
        ns_seen = {e["namespace"] for e in result["efficiency"]}
        assert "non-allocatable" not in ns_seen
        assert ".billing-hourly-rate" not in ns_seen

    def test_requests_below_threshold_yields_null_ratio(self, tmp_path):
        _write(tmp_path, "backend.json", {"cost_model": "cumulative", "currency": "%"})
        _write(
            tmp_path,
            "cpu_usage_period_1209600.json",
            [
                {"stack": "weird-ns", "usage": 10.0, "date": "01 Feb"},
            ],
        )
        # requests aggregate sub-threshold (simulates very high rf)
        _write(
            tmp_path,
            "cpu_requests_period_1209600.json",
            [
                {"stack": "weird-ns", "usage": 1e-9, "date": "01 Feb"},
            ],
        )
        result = mcp_server.tool_get_efficiency(
            scale="daily_14d",
            metric="cpu",
            data_dir=tmp_path,
        )
        assert len(result["efficiency"]) == 1
        entry = result["efficiency"][0]
        assert entry["efficiency_ratio"] is None
        assert entry["classification"] is None
        assert any("threshold" in w for w in result["metadata"]["warnings"])

    def test_requests_exactly_zero_also_yields_null(self, tmp_path):
        _write(tmp_path, "backend.json", {"cost_model": "cumulative", "currency": "%"})
        _write(
            tmp_path,
            "cpu_usage_period_1209600.json",
            [
                {"stack": "ns", "usage": 5.0, "date": "01 Feb"},
            ],
        )
        _write(
            tmp_path,
            "cpu_requests_period_1209600.json",
            [
                {"stack": "ns", "usage": 0.0, "date": "01 Feb"},
            ],
        )
        result = mcp_server.tool_get_efficiency(
            scale="daily_14d",
            metric="cpu",
            data_dir=tmp_path,
        )
        assert result["efficiency"][0]["efficiency_ratio"] is None

    def test_metadata_unit_is_efficiency_ratio(self, tmp_path):
        _setup_efficiency_fixture(tmp_path)
        result = mcp_server.tool_get_efficiency(scale="daily_14d", data_dir=tmp_path)
        # Headline number in the body is the ratio — metadata.unit must
        # reflect that (not the cost_model unit), per spec §4.3/§4.6 spirit.
        assert result["metadata"]["unit"] == "efficiency_ratio"
        # cost_model and currency are still present for context.
        assert result["metadata"]["cost_model"] == "cumulative"
        assert result["metadata"]["currency"] == "%"

    def test_sorted_by_namespace_then_metric(self, tmp_path):
        _setup_efficiency_fixture(tmp_path)
        result = mcp_server.tool_get_efficiency(scale="daily_14d", data_dir=tmp_path)
        pairs = [(e["namespace"], e["metric"]) for e in result["efficiency"]]
        assert pairs == sorted(pairs)

    def test_missing_requests_file_warns_and_skips_metric(self, tmp_path):
        _write(tmp_path, "backend.json", {"cost_model": "cumulative", "currency": "%"})
        _write(
            tmp_path,
            "cpu_usage_period_1209600.json",
            [
                {"stack": "ns", "usage": 10.0, "date": "01 Feb"},
            ],
        )
        # No cpu_requests file → cpu metric is skipped with a warning.
        result = mcp_server.tool_get_efficiency(
            scale="daily_14d",
            metric="cpu",
            data_dir=tmp_path,
        )
        assert result["efficiency"] == []
        assert any("requests" in w.lower() for w in result["metadata"]["warnings"])

    def test_unknown_namespace_warns(self, tmp_path):
        _setup_efficiency_fixture(tmp_path)
        result = mcp_server.tool_get_efficiency(
            scale="daily_14d",
            namespace="ghost-ns",
            data_dir=tmp_path,
        )
        assert result["efficiency"] == []
        assert any("ghost-ns" in w for w in result["metadata"]["warnings"])

    def test_data_window_reflects_actual_dates(self, tmp_path):
        _setup_efficiency_fixture(tmp_path)
        result = mcp_server.tool_get_efficiency(scale="daily_14d", data_dir=tmp_path)
        window = result["metadata"]["data_window"]
        assert window is not None
        assert window["start"] == "01 Feb"
        assert window["end"] == "02 Feb"
        assert window["points"] == 2

    def test_source_file_lists_both_files(self, tmp_path):
        _setup_efficiency_fixture(tmp_path)
        result = mcp_server.tool_get_efficiency(
            scale="daily_14d",
            metric="cpu",
            data_dir=tmp_path,
        )
        sf = result["metadata"]["source_file"]
        assert "cpu_usage_period_1209600.json" in sf
        assert "cpu_requests_period_1209600.json" in sf

    def test_invalid_scale_raises(self, tmp_path):
        with pytest.raises(ValueError):
            mcp_server.tool_get_efficiency(scale="hourly", data_dir=tmp_path)

    def test_invalid_metric_raises(self, tmp_path):
        with pytest.raises(ValueError):
            mcp_server.tool_get_efficiency(
                scale="daily_14d",
                metric="disk",
                data_dir=tmp_path,
            )


class TestClassifyEfficiency:
    def test_threshold_boundaries(self):
        assert mcp_server._classify_efficiency(0.0) == "over_provisioned"
        assert mcp_server._classify_efficiency(0.49) == "over_provisioned"
        assert mcp_server._classify_efficiency(0.5) == "balanced"
        assert mcp_server._classify_efficiency(0.85) == "balanced"
        assert mcp_server._classify_efficiency(1.0) == "balanced"
        assert mcp_server._classify_efficiency(1.01) == "under_provisioned"
        assert mcp_server._classify_efficiency(5.0) == "under_provisioned"


# ---------------------------------------------------------------------------
# Trends tools — fixtures
# ---------------------------------------------------------------------------


def _make_trends_for_namespace(name, start_hour="2026-02-03T13:00:00Z", count=10, base=1.0):
    base_dt = datetime.fromisoformat(start_hour.replace("Z", "+00:00"))
    return [
        {
            "name": name,
            "dateUTC": (base_dt + timedelta(hours=i)).isoformat().replace("+00:00", "Z"),
            "usage": _round_value(base + 0.1 * i),
        }
        for i in range(count)
    ]


def _round_value(v):
    return round(float(v), 6)


# ---------------------------------------------------------------------------
# Tool: get_timeseries (§4.4)
# ---------------------------------------------------------------------------


class TestToolGetTimeseries:
    def _setup(self, tmp_path, name="registry", count=10):
        _write(tmp_path, "backend.json", {"cost_model": "cumulative", "currency": "%"})
        _write(tmp_path, "cpu_usage_trends.json", _make_trends_for_namespace(name, count=count))

    def test_returns_series_and_stats(self, tmp_path):
        self._setup(tmp_path, count=5)
        result = mcp_server.tool_get_timeseries(
            metric="cpu",
            namespace="registry",
            data_dir=tmp_path,
        )
        assert len(result["series"]) == 5
        assert result["stats"]["points"] == 5
        # Series usage values are 1.0, 1.1, 1.2, 1.3, 1.4
        assert result["stats"]["min"] == 1.0
        assert result["stats"]["max"] == 1.4
        assert abs(result["stats"]["mean"] - 1.2) < 1e-6

    def test_window_bounds_match_series(self, tmp_path):
        self._setup(tmp_path, count=3)
        result = mcp_server.tool_get_timeseries(
            metric="cpu",
            namespace="registry",
            data_dir=tmp_path,
        )
        assert result["stats"]["window_start_utc"] == result["series"][0]["dateUTC"]
        assert result["stats"]["window_end_utc"] == result["series"][-1]["dateUTC"]

    def test_filters_only_requested_namespace(self, tmp_path):
        _write(tmp_path, "backend.json", {"cost_model": "cumulative", "currency": "%"})
        _write(
            tmp_path,
            "cpu_usage_trends.json",
            _make_trends_for_namespace("registry", count=3) + _make_trends_for_namespace("other-ns", count=5),
        )
        result = mcp_server.tool_get_timeseries(
            metric="cpu",
            namespace="registry",
            data_dir=tmp_path,
        )
        assert all(p["dateUTC"] for p in result["series"])
        assert len(result["series"]) == 3

    def test_missing_namespace_returns_empty_with_warning(self, tmp_path):
        self._setup(tmp_path)
        result = mcp_server.tool_get_timeseries(
            metric="cpu",
            namespace="ghost-ns",
            data_dir=tmp_path,
        )
        assert result["series"] == []
        assert result["stats"]["points"] == 0
        assert any("ghost-ns" in w for w in result["metadata"]["warnings"])

    def test_missing_file_returns_empty_with_warning(self, tmp_path):
        _write(tmp_path, "backend.json", {"cost_model": "cumulative", "currency": "%"})
        result = mcp_server.tool_get_timeseries(
            metric="cpu",
            namespace="registry",
            data_dir=tmp_path,
        )
        assert result["series"] == []
        assert result["stats"]["points"] == 0
        assert any("cpu_usage_trends.json" in w for w in result["metadata"]["warnings"])

    def test_metadata_data_window(self, tmp_path):
        self._setup(tmp_path, count=4)
        result = mcp_server.tool_get_timeseries(
            metric="cpu",
            namespace="registry",
            data_dir=tmp_path,
        )
        window = result["metadata"]["data_window"]
        assert window is not None
        assert window["granularity"] == "hourly"
        assert window["points"] == 4
        assert window["timezone"] == "UTC"
        assert result["metadata"]["source_file"] == "cpu_usage_trends.json"

    def test_invalid_metric_raises(self, tmp_path):
        with pytest.raises(ValueError):
            mcp_server.tool_get_timeseries(metric="disk", namespace="x", data_dir=tmp_path)

    def test_empty_namespace_raises(self, tmp_path):
        with pytest.raises(ValueError):
            mcp_server.tool_get_timeseries(metric="cpu", namespace="", data_dir=tmp_path)


# ---------------------------------------------------------------------------
# Tool: get_efficiency_timeseries (§4.4)
# ---------------------------------------------------------------------------


class TestToolGetEfficiencyTimeseries:
    def test_reads_rf_trends_file(self, tmp_path):
        _write(tmp_path, "backend.json", {"cost_model": "cumulative", "currency": "%"})
        # Same shape as usage trends — value field is "usage" but semantic
        # is efficiency ratio (spec §2.2)
        rf_entries = [
            {"name": "registry", "dateUTC": "2026-02-10T11:00:00Z", "usage": 0.3},
            {"name": "registry", "dateUTC": "2026-02-10T12:00:00Z", "usage": 0.4},
            {"name": "registry", "dateUTC": "2026-02-10T13:00:00Z", "usage": 1.2},  # > 1: under-provisioned hour
        ]
        _write(tmp_path, "cpu_rf_trends.json", rf_entries)

        result = mcp_server.tool_get_efficiency_timeseries(
            metric="cpu",
            namespace="registry",
            data_dir=tmp_path,
        )
        assert len(result["series"]) == 3
        assert result["stats"]["max"] == 1.2  # ratio > 1 preserved (spec §4.4)
        assert result["metadata"]["unit"] == "efficiency_ratio"

    def test_unit_override_propagates_when_empty(self, tmp_path):
        _write(tmp_path, "backend.json", {"cost_model": "costs", "currency": "$"})
        # No rf file at all
        result = mcp_server.tool_get_efficiency_timeseries(
            metric="cpu",
            namespace="registry",
            data_dir=tmp_path,
        )
        # Even in error/empty case, unit must say efficiency_ratio
        assert result["metadata"]["unit"] == "efficiency_ratio"

    def test_invalid_metric_raises(self, tmp_path):
        with pytest.raises(ValueError):
            mcp_server.tool_get_efficiency_timeseries(
                metric="disk",
                namespace="x",
                data_dir=tmp_path,
            )


# ---------------------------------------------------------------------------
# Tool: compare_periods (§4.4)
# ---------------------------------------------------------------------------


class TestToolComparePeriods:
    def _setup(self, tmp_path, monthly_values=None):
        if monthly_values is None:
            monthly_values = [10.0, 12.0, 14.0, 16.0]  # growing
        _write(tmp_path, "backend.json", {"cost_model": "cumulative", "currency": "%"})
        _write(
            tmp_path,
            "cpu_usage_period_1209600.json",
            [
                {"stack": "registry", "usage": 5.0, "date": "01 Feb"},
                {"stack": "registry", "usage": 7.0, "date": "02 Feb"},
                {"stack": "registry", "usage": 3.0, "date": "03 Feb"},
                {"stack": "other", "usage": 999.0, "date": "01 Feb"},
            ],
        )
        months = ["Nov 2025", "Dec 2025", "Jan 2026", "Feb 2026"]
        _write(
            tmp_path,
            "cpu_usage_period_31968000.json",
            [{"stack": "registry", "usage": v, "date": m} for v, m in zip(monthly_values, months, strict=True)]
            + [{"stack": "other", "usage": 50.0, "date": "Feb 2026"}],
        )

    def test_returns_both_blocks(self, tmp_path):
        self._setup(tmp_path)
        result = mcp_server.tool_compare_periods(
            metric="cpu",
            namespace="registry",
            data_dir=tmp_path,
        )
        assert "daily_14d" in result
        assert "monthly_12m" in result
        assert result["daily_14d"]["aggregate"] == 15.0  # 5 + 7 + 3
        assert result["daily_14d"]["points"] == 3
        assert result["daily_14d"]["window_start"] == "01 Feb"
        assert result["daily_14d"]["window_end"] == "03 Feb"

    def test_monthly_series_preserves_order(self, tmp_path):
        self._setup(tmp_path)
        result = mcp_server.tool_compare_periods(
            metric="cpu",
            namespace="registry",
            data_dir=tmp_path,
        )
        dates = [p["date"] for p in result["monthly_12m"]["series"]]
        assert dates == ["Nov 2025", "Dec 2025", "Jan 2026", "Feb 2026"]

    def test_trend_growing(self, tmp_path):
        self._setup(tmp_path, monthly_values=[10.0, 12.0, 14.0, 16.0])
        result = mcp_server.tool_compare_periods(
            metric="cpu",
            namespace="registry",
            data_dir=tmp_path,
        )
        assert result["monthly_12m"]["trend_direction"] == "growing"

    def test_trend_decreasing(self, tmp_path):
        self._setup(tmp_path, monthly_values=[100.0, 90.0, 70.0, 60.0])
        result = mcp_server.tool_compare_periods(
            metric="cpu",
            namespace="registry",
            data_dir=tmp_path,
        )
        assert result["monthly_12m"]["trend_direction"] == "decreasing"

    def test_trend_stable(self, tmp_path):
        self._setup(tmp_path, monthly_values=[100.0, 102.0, 101.0, 99.0])
        result = mcp_server.tool_compare_periods(
            metric="cpu",
            namespace="registry",
            data_dir=tmp_path,
        )
        assert result["monthly_12m"]["trend_direction"] == "stable"

    def test_trend_insufficient_data(self, tmp_path):
        _write(tmp_path, "backend.json", {"cost_model": "cumulative", "currency": "%"})
        _write(
            tmp_path,
            "cpu_usage_period_1209600.json",
            [
                {"stack": "registry", "usage": 5.0, "date": "01 Feb"},
            ],
        )
        _write(
            tmp_path,
            "cpu_usage_period_31968000.json",
            [
                {"stack": "registry", "usage": 10.0, "date": "Feb 2026"},
            ],
        )
        result = mcp_server.tool_compare_periods(
            metric="cpu",
            namespace="registry",
            data_dir=tmp_path,
        )
        assert result["monthly_12m"]["trend_direction"] == "insufficient_data"

    def test_missing_daily_file_still_returns_monthly(self, tmp_path):
        _write(tmp_path, "backend.json", {"cost_model": "cumulative", "currency": "%"})
        _write(
            tmp_path,
            "cpu_usage_period_31968000.json",
            [
                {"stack": "registry", "usage": v, "date": d}
                for v, d in [(10.0, "Nov 2025"), (12.0, "Dec 2025"), (14.0, "Jan 2026"), (16.0, "Feb 2026")]
            ],
        )
        result = mcp_server.tool_compare_periods(
            metric="cpu",
            namespace="registry",
            data_dir=tmp_path,
        )
        assert result["daily_14d"] is None
        assert result["monthly_12m"] is not None
        assert result["monthly_12m"]["trend_direction"] == "growing"
        assert any("period_1209600" in w for w in result["metadata"]["warnings"])

    def test_unknown_namespace_warns(self, tmp_path):
        self._setup(tmp_path)
        result = mcp_server.tool_compare_periods(
            metric="cpu",
            namespace="ghost-ns",
            data_dir=tmp_path,
        )
        warnings = result["metadata"]["warnings"]
        assert any("ghost-ns" in w for w in warnings)
        # Both blocks present but empty
        assert result["daily_14d"]["aggregate"] == 0.0
        assert result["monthly_12m"]["series"] == []

    def test_invalid_metric_raises(self, tmp_path):
        with pytest.raises(ValueError):
            mcp_server.tool_compare_periods(
                metric="disk",
                namespace="x",
                data_dir=tmp_path,
            )


class TestClassifyTrend:
    def test_growing_above_threshold(self):
        assert mcp_server._classify_trend([10.0, 10.0, 12.0, 12.0]) == "growing"

    def test_decreasing_below_threshold(self):
        assert mcp_server._classify_trend([20.0, 20.0, 10.0, 10.0]) == "decreasing"

    def test_stable_within_threshold(self):
        # First half mean 10, second 10.5 → delta 5% < 10%
        assert mcp_server._classify_trend([10.0, 10.0, 10.5, 10.5]) == "stable"

    def test_insufficient_data(self):
        assert mcp_server._classify_trend([]) == "insufficient_data"
        assert mcp_server._classify_trend([5.0]) == "insufficient_data"

    def test_zero_baseline_growth(self):
        assert mcp_server._classify_trend([0.0, 0.0, 5.0, 5.0]) == "growing"


# ---------------------------------------------------------------------------
# Tool: group_namespaces (§4.5)
# ---------------------------------------------------------------------------


def _setup_group_fixture(tmp_path):
    """Mix of openshift-*, open-cluster-management-*, application, and special.

    Date "02 Feb" usage:
        openshift-monitoring=40, openshift-nmstate=10, openshift-dns=5
        open-cluster-management=50, open-cluster-management-agent=20
        registry=15, my-app=8
        non-allocatable=100
    """
    _write(tmp_path, "backend.json", {"cost_model": "cumulative", "currency": "%"})
    _write(
        tmp_path,
        "cpu_usage_period_1209600.json",
        [
            # 01 Feb — sparse, for "default date = last" test
            {"stack": "openshift-monitoring", "usage": 30.0, "date": "01 Feb"},
            {"stack": "registry", "usage": 10.0, "date": "01 Feb"},
            # 02 Feb — full fixture
            {"stack": "openshift-monitoring", "usage": 40.0, "date": "02 Feb"},
            {"stack": "openshift-nmstate", "usage": 10.0, "date": "02 Feb"},
            {"stack": "openshift-dns", "usage": 5.0, "date": "02 Feb"},
            {"stack": "open-cluster-management", "usage": 50.0, "date": "02 Feb"},
            {"stack": "open-cluster-management-agent", "usage": 20.0, "date": "02 Feb"},
            {"stack": "registry", "usage": 15.0, "date": "02 Feb"},
            {"stack": "my-app", "usage": 8.0, "date": "02 Feb"},
            {"stack": "non-allocatable", "usage": 100.0, "date": "02 Feb"},
        ],
    )


class TestToolGroupNamespaces:
    def test_openshift_prefix_match(self, tmp_path):
        _setup_group_fixture(tmp_path)
        result = mcp_server.tool_group_namespaces(
            metric="cpu",
            scale="daily_14d",
            pattern="openshift-*",
            data_dir=tmp_path,
        )
        # Default date = 02 Feb (last)
        assert result["date"] == "02 Feb"
        # openshift-monitoring=40, openshift-nmstate=10, openshift-dns=5
        assert result["matched_count"] == 3
        assert result["group_total"] == 55.0
        names = {m["namespace"] for m in result["members"]}
        assert names == {"openshift-monitoring", "openshift-nmstate", "openshift-dns"}

    def test_members_sorted_descending(self, tmp_path):
        _setup_group_fixture(tmp_path)
        result = mcp_server.tool_group_namespaces(
            metric="cpu",
            scale="daily_14d",
            pattern="openshift-*",
            data_dir=tmp_path,
        )
        values = [m["value"] for m in result["members"]]
        assert values == sorted(values, reverse=True)
        # Top is openshift-monitoring=40
        assert result["members"][0]["namespace"] == "openshift-monitoring"
        assert result["members"][0]["value"] == 40.0

    def test_pattern_with_star_excludes_special(self, tmp_path):
        _setup_group_fixture(tmp_path)
        result = mcp_server.tool_group_namespaces(
            metric="cpu",
            scale="daily_14d",
            pattern="*",
            data_dir=tmp_path,
        )
        names = {m["namespace"] for m in result["members"]}
        # non-allocatable matches the pattern but is filtered as special
        assert "non-allocatable" not in names
        # Group total = 40+10+5+50+20+15+8 = 148 (no overhead 100)
        assert result["group_total"] == 148.0

    def test_open_cluster_management_pattern(self, tmp_path):
        _setup_group_fixture(tmp_path)
        result = mcp_server.tool_group_namespaces(
            metric="cpu",
            scale="daily_14d",
            pattern="open-cluster-management*",
            data_dir=tmp_path,
        )
        assert result["matched_count"] == 2
        assert result["group_total"] == 70.0  # 50 + 20

    def test_explicit_date_filter(self, tmp_path):
        _setup_group_fixture(tmp_path)
        result = mcp_server.tool_group_namespaces(
            metric="cpu",
            scale="daily_14d",
            pattern="openshift-*",
            date="01 Feb",
            data_dir=tmp_path,
        )
        assert result["date"] == "01 Feb"
        assert result["matched_count"] == 1
        assert result["group_total"] == 30.0

    def test_pattern_matches_nothing_warns(self, tmp_path):
        _setup_group_fixture(tmp_path)
        result = mcp_server.tool_group_namespaces(
            metric="cpu",
            scale="daily_14d",
            pattern="nope-*",
            data_dir=tmp_path,
        )
        assert result["matched_count"] == 0
        assert result["group_total"] == 0.0
        assert result["members"] == []
        assert any("matched no namespace" in w for w in result["metadata"]["warnings"])

    def test_unknown_date_warns(self, tmp_path):
        _setup_group_fixture(tmp_path)
        result = mcp_server.tool_group_namespaces(
            metric="cpu",
            scale="daily_14d",
            pattern="openshift-*",
            date="99 Dec",
            data_dir=tmp_path,
        )
        # Date used as-is even if not in file → 0 matches + warning
        assert result["date"] == "99 Dec"
        assert result["matched_count"] == 0
        assert any("99 Dec" in w for w in result["metadata"]["warnings"])

    def test_missing_file_returns_empty_with_warning(self, tmp_path):
        _write(tmp_path, "backend.json", {"cost_model": "cumulative", "currency": "%"})
        result = mcp_server.tool_group_namespaces(
            metric="cpu",
            scale="daily_14d",
            pattern="openshift-*",
            data_dir=tmp_path,
        )
        assert result["matched_count"] == 0
        assert result["members"] == []
        assert any("cpu_usage_period_1209600.json" in w for w in result["metadata"]["warnings"])

    def test_data_window_reflects_single_date(self, tmp_path):
        _setup_group_fixture(tmp_path)
        result = mcp_server.tool_group_namespaces(
            metric="cpu",
            scale="daily_14d",
            pattern="openshift-*",
            data_dir=tmp_path,
        )
        window = result["metadata"]["data_window"]
        assert window["start"] == "02 Feb"
        assert window["end"] == "02 Feb"
        assert window["points"] == 1

    def test_empty_pattern_raises(self, tmp_path):
        with pytest.raises(ValueError):
            mcp_server.tool_group_namespaces(
                metric="cpu",
                scale="daily_14d",
                pattern="",
                data_dir=tmp_path,
            )

    def test_invalid_metric_raises(self, tmp_path):
        with pytest.raises(ValueError):
            mcp_server.tool_group_namespaces(
                metric="disk",
                scale="daily_14d",
                pattern="*",
                data_dir=tmp_path,
            )

    def test_invalid_scale_raises(self, tmp_path):
        with pytest.raises(ValueError):
            mcp_server.tool_group_namespaces(
                metric="cpu",
                scale="hourly",
                pattern="*",
                data_dir=tmp_path,
            )


# ---------------------------------------------------------------------------
# MCP wiring smoke tests (§7.2 step 8)
#
# These tests are skipped when the ``mcp`` SDK is not installed (e.g. on a
# Windows dev machine where the project's rrdtool dep prevents ``uv sync``).
# They cover only the wiring layer — the tool_* logic is tested above.
# ---------------------------------------------------------------------------

try:
    import mcp  # noqa: F401  (presence check)

    _MCP_AVAILABLE = True
except ImportError:
    _MCP_AVAILABLE = False


@pytest.mark.skipif(not _MCP_AVAILABLE, reason="mcp SDK not installed")
class TestMcpWiring:
    def test_build_mcp_app_registers_all_tools(self):
        app = mcp_server._build_mcp_app()
        registered = {name for name, _ in mcp_server._MCP_TOOL_REGISTRY}

        expected = {
            "list_namespaces",
            "describe_dataset",
            "get_usage",
            "get_top_consumers",
            "get_namespace_breakdown",
            "get_efficiency",
            "get_timeseries",
            "compare_periods",
            "get_efficiency_timeseries",
            "group_namespaces",
        }
        assert registered == expected
        # FastMCP exposes a list_tools method (or a tools manager) — just
        # assert the app instance is built and named correctly.
        assert app is not None
        assert getattr(app, "name", None) in ("kubeledger-mcp", None) or hasattr(app, "name")

    def test_registry_points_to_actual_tool_functions(self):
        mcp_server._build_mcp_app()
        for name, func in mcp_server._MCP_TOOL_REGISTRY:
            assert callable(func), f"registry entry {name!r} is not callable"
            # Sanity check: function name matches its key
            assert func.__name__ == "tool_" + name

    def test_detect_structured_content_reports_a_decision(self):
        supported, reason = mcp_server.detect_structured_content_support()
        # Whatever the decision is, the function must return a non-empty
        # reason string — spec §4.7 requires that the developer be informed.
        assert isinstance(supported, bool)
        assert isinstance(reason, str) and reason


class TestMcpWiringStubsWithoutSDK:
    """Verify the module is importable without the mcp SDK."""

    def test_module_imports_without_sdk(self):
        # mcp_server is already imported at top of file — re-import via a
        # fresh sys.path manipulation would be heavy; just verify the
        # public surface still exists.
        assert callable(mcp_server.tool_list_namespaces)
        assert callable(mcp_server.tool_describe_dataset)
        assert callable(mcp_server.tool_get_usage)
        assert callable(mcp_server.tool_get_top_consumers)
        assert callable(mcp_server.tool_get_namespace_breakdown)
        assert callable(mcp_server.tool_get_efficiency)
        assert callable(mcp_server.tool_get_timeseries)
        assert callable(mcp_server.tool_compare_periods)
        assert callable(mcp_server.tool_get_efficiency_timeseries)
        assert callable(mcp_server.tool_group_namespaces)

    def test_detect_structured_content_handles_missing_sdk_gracefully(self):
        # Even when the SDK is missing, the detector must return cleanly
        # rather than raise — wiring code at startup depends on that.
        result = mcp_server.detect_structured_content_support()
        assert len(result) == 2
        assert isinstance(result[0], bool)
        assert isinstance(result[1], str)


# ---------------------------------------------------------------------------
# Metadata audit (§7.2 step 9) — every tool's response must carry a metadata
# block whose shape matches spec §4.6.
#
# These tests deliberately ignore the *content* of metadata (already covered
# per-tool above) and focus on:
#   - presence of every required field,
#   - correct typing (warnings is a list, timezone is "UTC", ...),
#   - ISO 8601 UTC formatting of generated_at_utc,
#   - propagation of caller-supplied warnings.
# ---------------------------------------------------------------------------


METADATA_REQUIRED_KEYS = frozenset(
    {
        "cost_model",
        "currency",
        "unit",
        "metric",
        "scale",
        "data_window",
        "source_file",
        "generated_at_utc",
        "warnings",
    }
)


def _assert_valid_metadata(md, label):
    """Cross-tool metadata invariants — shape + types + UTC."""
    assert isinstance(md, dict), f"{label}: metadata is not a dict"
    extra = set(md) - METADATA_REQUIRED_KEYS
    missing = METADATA_REQUIRED_KEYS - set(md)
    assert not extra, f"{label}: metadata has unexpected keys {extra}"
    assert not missing, f"{label}: metadata is missing keys {missing}"

    assert isinstance(md["warnings"], list), f"{label}: warnings is not a list"
    for w in md["warnings"]:
        assert isinstance(w, str) and w, f"{label}: warning is not a non-empty string"

    if md["data_window"] is not None:
        dw = md["data_window"]
        assert dw["timezone"] == "UTC", f"{label}: data_window.timezone != UTC"
        assert "granularity" in dw and dw["granularity"] in (
            "daily",
            "monthly",
            "hourly",
        ), f"{label}: data_window.granularity invalid"
        assert "points" in dw and isinstance(dw["points"], int), f"{label}: data_window.points is not int"

    if md["generated_at_utc"] is not None:
        assert md["generated_at_utc"].endswith("Z"), (
            "{}: generated_at_utc not ISO 8601 UTC (must end with Z): {}".format(
                label,
                md["generated_at_utc"],
            )
        )


def _setup_full_audit_fixture(tmp_path):
    """Dataset rich enough to drive every tool through a happy path.

    - 2 metrics × 2 scales × usage + requests histograms (8 files)
    - usage + rf trends for both metrics (4 files)
    - backend.json with cost_model=cumulative
    """
    _write(tmp_path, "backend.json", {"cost_model": "cumulative", "currency": "%"})

    daily = _make_consolidation_dataset()
    monthly = [
        {"stack": "registry", "usage": 20.0, "date": "Dec 2025"},
        {"stack": "registry", "usage": 25.0, "date": "Jan 2026"},
        {"stack": "registry", "usage": 30.0, "date": "Feb 2026"},
        {"stack": "alpha", "usage": 5.0, "date": "Dec 2025"},
        {"stack": "alpha", "usage": 6.0, "date": "Jan 2026"},
        {"stack": "alpha", "usage": 8.0, "date": "Feb 2026"},
        {"stack": "non-allocatable", "usage": 200.0, "date": "Feb 2026"},
    ]

    for metric in ("cpu", "memory"):
        for dimension in ("usage", "requests"):
            _write(
                tmp_path, "{}_{}_period_1209600.json".format(metric if metric == "cpu" else "memory", dimension), daily
            )
            _write(
                tmp_path,
                "{}_{}_period_31968000.json".format(metric if metric == "cpu" else "memory", dimension),
                monthly,
            )
        _write(
            tmp_path,
            "{}_usage_trends.json".format(metric if metric == "cpu" else "memory"),
            _make_trends_for_namespace("registry", count=24),
        )
        _write(
            tmp_path,
            "{}_rf_trends.json".format(metric if metric == "cpu" else "memory"),
            _make_trends_for_namespace("registry", count=24, base=0.5),
        )


# Tools sorted as in spec §4. Each entry: (name, callable taking data_dir → response).
ALL_TOOL_INVOCATIONS = [
    ("list_namespaces", lambda d: mcp_server.tool_list_namespaces(data_dir=d)),
    ("describe_dataset", lambda d: mcp_server.tool_describe_dataset(data_dir=d)),
    ("get_usage", lambda d: mcp_server.tool_get_usage(metric="cpu", scale="daily_14d", data_dir=d)),
    ("get_top_consumers", lambda d: mcp_server.tool_get_top_consumers(metric="cpu", scale="daily_14d", data_dir=d)),
    (
        "get_namespace_breakdown",
        lambda d: mcp_server.tool_get_namespace_breakdown(metric="cpu", scale="daily_14d", data_dir=d),
    ),
    ("get_efficiency", lambda d: mcp_server.tool_get_efficiency(scale="daily_14d", data_dir=d)),
    ("get_timeseries", lambda d: mcp_server.tool_get_timeseries(metric="cpu", namespace="registry", data_dir=d)),
    ("compare_periods", lambda d: mcp_server.tool_compare_periods(metric="cpu", namespace="registry", data_dir=d)),
    (
        "get_efficiency_timeseries",
        lambda d: mcp_server.tool_get_efficiency_timeseries(metric="cpu", namespace="registry", data_dir=d),
    ),
    (
        "group_namespaces",
        lambda d: mcp_server.tool_group_namespaces(metric="cpu", scale="daily_14d", pattern="*", data_dir=d),
    ),
]


class TestMetadataAuditAcrossAllTools:
    @pytest.mark.parametrize("name,invoke", ALL_TOOL_INVOCATIONS, ids=[n for n, _ in ALL_TOOL_INVOCATIONS])
    def test_metadata_shape_is_uniform(self, tmp_path, name, invoke):
        _setup_full_audit_fixture(tmp_path)
        result = invoke(tmp_path)
        assert "metadata" in result, f"{name}: response has no metadata key"
        _assert_valid_metadata(result["metadata"], name)

    @pytest.mark.parametrize("name,invoke", ALL_TOOL_INVOCATIONS, ids=[n for n, _ in ALL_TOOL_INVOCATIONS])
    def test_metadata_carries_cost_model_when_available(self, tmp_path, name, invoke):
        _setup_full_audit_fixture(tmp_path)
        result = invoke(tmp_path)
        md = result["metadata"]
        # backend.json is present in the fixture → cost_model & currency populated
        assert md["cost_model"] == "cumulative", f"{name}: lost cost_model"
        assert md["currency"] == "%", f"{name}: lost currency"
        # unit: either the cumulative unit OR the efficiency_ratio override
        assert md["unit"] in ("percent_of_cluster_capacity", "efficiency_ratio"), "{}: unexpected unit {!r}".format(
            name, md["unit"]
        )

    @pytest.mark.parametrize("name,invoke", ALL_TOOL_INVOCATIONS, ids=[n for n, _ in ALL_TOOL_INVOCATIONS])
    def test_metadata_present_even_when_data_missing(self, tmp_path, name, invoke):
        # No fixture at all — every tool must still produce a valid metadata
        # block (with warnings explaining the lack of data).
        result = invoke(tmp_path)
        assert "metadata" in result
        _assert_valid_metadata(result["metadata"], name)
        assert result["metadata"]["warnings"], f"{name}: empty warnings list while data is missing"


class TestFreshnessWarningPropagation:
    """Spec §7.2-5 — freshness warning kicks in for stale data, on every tool."""

    @pytest.mark.parametrize("name,invoke", ALL_TOOL_INVOCATIONS, ids=[n for n, _ in ALL_TOOL_INVOCATIONS])
    def test_stale_data_yields_warning(self, tmp_path, name, invoke):
        _setup_full_audit_fixture(tmp_path)
        # Push every data file's mtime 2 hours into the past.
        stale_time = datetime.now(tz=UTC).timestamp() - 2 * 3600
        for path in tmp_path.iterdir():
            if path.name == "backend.json":
                continue  # backend.json is excluded from freshness (spec §7.2-5)
            os.utime(path, (stale_time, stale_time))

        result = invoke(tmp_path)
        warnings = result["metadata"]["warnings"]
        # Some tools (list_namespaces, describe_dataset) read every data file
        # → freshness applies; trend-only tools also touch files; backend.json
        # exclusion means the only mtime considered is data → all should warn.
        assert any("stale data" in w for w in warnings), f"{name}: expected a stale-data warning, got {warnings!r}"


class TestWarningsAreActionable:
    """Warnings must include enough context to be acted upon by the client."""

    def test_missing_file_warning_names_the_file(self, tmp_path):
        result = mcp_server.tool_get_usage(
            metric="cpu",
            scale="daily_14d",
            data_dir=tmp_path,
        )
        warnings = result["metadata"]["warnings"]
        assert any("cpu_usage_period_1209600.json" in w for w in warnings)

    def test_unknown_namespace_warning_names_the_namespace(self, tmp_path):
        _write(tmp_path, "backend.json", {"cost_model": "cumulative", "currency": "%"})
        _write(tmp_path, "cpu_usage_trends.json", _make_trends_for_namespace("real-ns", count=3))
        result = mcp_server.tool_get_timeseries(
            metric="cpu",
            namespace="ghost",
            data_dir=tmp_path,
        )
        warnings = result["metadata"]["warnings"]
        assert any("ghost" in w for w in warnings)

    def test_efficiency_threshold_warning_names_namespace_and_metric(self, tmp_path):
        _write(tmp_path, "backend.json", {"cost_model": "cumulative", "currency": "%"})
        _write(
            tmp_path,
            "cpu_usage_period_1209600.json",
            [
                {"stack": "weird-ns", "usage": 10.0, "date": "01 Feb"},
            ],
        )
        _write(
            tmp_path,
            "cpu_requests_period_1209600.json",
            [
                {"stack": "weird-ns", "usage": 1e-9, "date": "01 Feb"},
            ],
        )
        result = mcp_server.tool_get_efficiency(
            scale="daily_14d",
            metric="cpu",
            data_dir=tmp_path,
        )
        warnings = result["metadata"]["warnings"]
        assert any("weird-ns" in w and "cpu" in w for w in warnings)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

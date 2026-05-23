#!/usr/bin/env python
"""KubeLedger MCP server.

Read-only MCP (Model Context Protocol) server exposing KubeLedger analytics
that backend.py already materializes as JSON files under static/data/. The
server never reads the RRD databases directly, never writes, and never embeds
an LLM — it is a descriptive data surface, and the narrative intelligence is
brought by the MCP client.

This file is built up incrementally, following the implementation plan of
docs/kubeledger-mcp-spec.md (section 7.2). The current revision covers
step 1: the data access layer.
"""

__author__ = "Rodrigue Chakode"
__copyright__ = "Copyright 2026 Rodrigue Chakode and contributors"
__license__ = "Business Source License 1.1"

import collections
import fnmatch
import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_DATA_DIR = "./static/data"
BACKEND_CONFIG_FILENAME = "backend.json"

# Histogram periods written by backend.py:dump_histogram_analytics.
# These are not granularities — they are retrieval windows. backend.py groups
# by month for the year window and by day for the 14-day window. See spec §2.1.
PERIOD_14_DAYS_SEC = 1209600
PERIOD_YEAR_SEC = 31968000

# Public scale identifiers exposed by the MCP tools.
SCALE_DAILY_14D = "daily_14d"
SCALE_MONTHLY_12M = "monthly_12m"
SCALE_TO_PERIOD = {
    SCALE_DAILY_14D: PERIOD_14_DAYS_SEC,
    SCALE_MONTHLY_12M: PERIOD_YEAR_SEC,
}
SCALE_GRANULARITY = {
    SCALE_DAILY_14D: "daily",
    SCALE_MONTHLY_12M: "monthly",
}
SCALE_DEFAULT_DEPTH = {
    SCALE_DAILY_14D: "14 days",
    SCALE_MONTHLY_12M: "~12 months",
}

# Translation from the cost_model written by backend.py to its semantic unit
# (used in the metadata block of every MCP response — spec §4.6).
COST_MODEL_UNIT = {
    "cumulative": "percent_of_cluster_capacity",
    "normalized": "relative_share_percent",
    "costs": "monetary_cost",
}

METRICS = ("cpu", "memory")
DIMENSIONS = ("usage", "requests")

# Prefixes used to classify namespaces as "system" rather than "application"
# (spec §4.1, §2.4). OpenShift is the primary context (operators in
# openshift-*); the standard upstream Kubernetes namespaces (kube-system,
# kube-public, kube-node-lease) share the same role.
SYSTEM_NAMESPACE_PREFIXES = ("openshift-", "kube-")

# Special entries that are NOT namespaces — see spec §2.4. Always listed
# in list_namespaces' special_entries block, regardless of whether they
# happen to appear in the data on this particular run.
SPECIAL_ENTRIES: dict[str, dict[str, str]] = {
    "non-allocatable": {
        "role": "cluster_overhead",
        "description": (
            "Cluster capacity reserved for system overhead, not allocatable "
            "to pods. Excluded from usage analyses by default, but exposed as "
            "a separate signal — e.g. cluster_overhead in get_namespace_breakdown."
        ),
    },
    ".billing-hourly-rate": {
        "role": "billing_config",
        "description": (
            "Hourly billing rate, present only when cost_model=costs. Not a usage data point — a configuration value."
        ),
    },
}

# Warn when the most recent data file is older than this many seconds.
# backend.py dumps every ~5 minutes; 30 minutes leaves ample headroom.
FRESHNESS_WARN_AFTER_SECONDS = 1800

# Efficiency / requests handling (spec §4.3).
#
# requests is derived in backend.py as ``usage / rf`` (request factor), so a
# very high rf yields an *infinitesimal* but non-zero requests value. The
# exact ``requests == 0`` test is therefore insufficient — any value below
# this threshold must be treated as zero, otherwise usage/requests produces
# numerical aberrations.
REQUESTS_ZERO_THRESHOLD = 1e-6

# Efficiency classification thresholds (descriptive, not prescriptive).
# - ratio < 0.5  → over_provisioned (usage much smaller than requests)
# - 0.5 ≤ ratio ≤ 1.0 → balanced
# - ratio > 1.0  → under_provisioned (usage exceeds requests)
EFFICIENCY_OVER_PROVISIONED_BELOW = 0.5
EFFICIENCY_UNDER_PROVISIONED_ABOVE = 1.0

# Relative change threshold for ``compare_periods`` trend classification —
# noise filter, applied to the difference between first-half and second-half
# means of the monthly series. 10 % of the baseline.
TREND_CHANGE_THRESHOLD = 0.10

# Maximum length of a glob pattern accepted by ``tool_group_namespaces``.
# fnmatch internally compiles to a regex; pathological patterns built from
# many wildcards (e.g. ``"*?" * 1000``) can cause catastrophic backtracking.
# 256 chars is far above any realistic namespace selector and below the
# point where regex compilation cost matters.
GROUP_PATTERN_MAX_LENGTH = 256


def _get_env(name: str, default: str | None = None) -> str | None:
    """Read an environment variable, KL_<name> taking precedence over KOA_<name>.

    Mirrors backend.py:get_backend_config_env so operators configure the MCP
    container with the same convention as the main backend.
    """
    kl_value = os.environ.get(f"KL_{name}")
    if kl_value is not None:
        return kl_value
    return os.environ.get(f"KOA_{name}", default)


def get_data_dir() -> Path:
    """Resolve the directory holding KubeLedger static data files."""
    value = _get_env("MCP_DATA_DIR", DEFAULT_DATA_DIR)
    return Path(value or DEFAULT_DATA_DIR)


# ---------------------------------------------------------------------------
# Robust JSON reader
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FileReadResult:
    """Outcome of a JSON file read attempt.

    The reader never raises: every failure mode is reported through this
    structure, so callers can decide whether the failure is recoverable
    (missing accessory file → warning) or fatal for the request (missing
    file required to answer → MCP error).
    """

    filename: str
    path: Path
    exists: bool
    payload: Any
    error: str | None
    mtime_utc: float | None

    @property
    def ok(self) -> bool:
        return self.error is None and self.payload is not None


def read_json_file(filename: str, data_dir: Path | None = None) -> FileReadResult:
    """Read and parse a JSON file from the data directory, never raising.

    Failure modes intercepted:

    - **Missing file** → ``exists=False``, ``payload=None``, ``error`` set.
    - **OS-level read failure** → ``exists=True``, ``payload=None``, ``error`` set.
    - **Malformed or truncated JSON** → ``exists=True``, ``payload=None``,
      ``error`` set. backend.py writes histogram and trend files by string
      concatenation (``"[" + ",".join(parts) + "]"``); a crash mid-write
      leaves a syntactically invalid file. ``json.JSONDecodeError`` must
      therefore be caught here and must never propagate up to the MCP
      transport, where it would terminate the client connection.

    On success the parsed JSON is returned in ``payload``. For the files
    produced by backend.py this is typically a ``list[dict]`` (histograms,
    trends) or a ``dict`` (``backend.json``).

    Path containment: today every call site uses a fixed name derived from
    :func:`histogram_filename`, :func:`trends_filename`, or
    :data:`BACKEND_CONFIG_FILENAME`, so traversal cannot happen. The guard
    below makes that contract explicit — any future caller passing
    untrusted input will fail closed rather than escape the data directory.
    """
    if "/" in filename or "\\" in filename or ".." in filename or filename.startswith("."):
        # Defensive guard. ``.billing-hourly-rate`` and similar dot-prefixed
        # names are valid namespace entries but never used as filenames here.
        return FileReadResult(
            filename=filename,
            path=Path(filename),
            exists=False,
            payload=None,
            error=f"invalid filename {filename!r}: path traversal characters not allowed",
            mtime_utc=None,
        )

    root = data_dir if data_dir is not None else get_data_dir()
    path = root / filename

    if not path.is_file():
        return FileReadResult(
            filename=filename,
            path=path,
            exists=False,
            payload=None,
            error=f"file not found: {path}",
            mtime_utc=None,
        )

    try:
        mtime = path.stat().st_mtime
    except OSError as exc:
        return FileReadResult(
            filename=filename,
            path=path,
            exists=True,
            payload=None,
            error=f"stat failed: {exc}",
            mtime_utc=None,
        )

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return FileReadResult(
            filename=filename,
            path=path,
            exists=True,
            payload=None,
            error=f"read failed: {exc}",
            mtime_utc=mtime,
        )

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        return FileReadResult(
            filename=filename,
            path=path,
            exists=True,
            payload=None,
            error=f"invalid JSON: {exc.msg} (line {exc.lineno}, column {exc.colno})",
            mtime_utc=mtime,
        )

    return FileReadResult(
        filename=filename,
        path=path,
        exists=True,
        payload=payload,
        error=None,
        mtime_utc=mtime,
    )


# ---------------------------------------------------------------------------
# Filename helpers — derived from backend.py:dump_histogram_analytics and
# dump_trend_analytics. The MCP holds no other assumption about file paths.
# ---------------------------------------------------------------------------


def _mem_label(gpu: bool) -> str:
    # backend.py uses "memory" for standard metrics and "mem" when the
    # gpu_ prefix is applied — see backend.py:1078 and 1181.
    return "mem" if gpu else "memory"


def histogram_filename(metric: str, dimension: str, period_sec: int, gpu: bool = False) -> str:
    """Return the filename produced by backend.py for a given histogram.

    Args:
        metric: ``"cpu"`` or ``"memory"``.
        dimension: ``"usage"`` or ``"requests"``.
        period_sec: ``PERIOD_14_DAYS_SEC`` or ``PERIOD_YEAR_SEC``.
        gpu: ``True`` to select the GPU variant (``gpu_*`` prefix).

    """
    if metric not in METRICS:
        raise ValueError(f"unknown metric: {metric!r} (expected one of {METRICS})")
    if dimension not in DIMENSIONS:
        raise ValueError(f"unknown dimension: {dimension!r} (expected one of {DIMENSIONS})")
    prefix = "gpu_" if gpu else ""
    res = "cpu" if metric == "cpu" else _mem_label(gpu)
    return f"{prefix}{res}_{dimension}_period_{period_sec}.json"


def trends_filename(metric: str, category: str, gpu: bool = False) -> str:
    """Return the filename produced by backend.py for a given trends series.

    Args:
        metric: ``"cpu"`` or ``"memory"``.
        category: ``"usage"`` or ``"rf"`` (request factor). ``rf`` only
            exists in the non-GPU variant — backend.py does not emit
            ``gpu_*_rf_trends.json``.
        gpu: ``True`` to select the GPU variant.

    """
    if metric not in METRICS:
        raise ValueError(f"unknown metric: {metric!r} (expected one of {METRICS})")
    if category not in ("usage", "rf"):
        raise ValueError(f"unknown category: {category!r} (expected 'usage' or 'rf')")
    prefix = "gpu_" if gpu else ""
    res = "cpu" if metric == "cpu" else _mem_label(gpu)
    return f"{prefix}{res}_{category}_trends.json"


# ---------------------------------------------------------------------------
# Cost model — loaded from static/data/backend.json (spec §2.3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CostModelInfo:
    """Cost model in effect, as written by backend.py at start-up.

    The labels here are the **translated** ones from ``backend.json``
    (``cumulative`` / ``normalized`` / ``costs``), not the internal config
    names (``CUMULATIVE_RATIO`` / ``RATIO`` / ``CHARGE_BACK``). The MCP must
    only consider the translated labels — see spec §2.3.
    """

    cost_model: str
    currency: str
    unit: str  # Semantic unit derived from cost_model (spec §4.6 mapping).


def load_cost_model(data_dir: Path | None = None) -> tuple[CostModelInfo | None, list[str]]:
    """Load the cost model from ``backend.json``.

    Returns:
        ``(info, warnings)``. ``info`` is ``None`` when the file is missing
        or unusable; ``warnings`` collects every recoverable issue so the
        caller can surface them in the MCP response.

    """
    warnings: list[str] = []
    result = read_json_file(BACKEND_CONFIG_FILENAME, data_dir=data_dir)
    if not result.ok:
        warnings.append(f"backend.json unreadable: {result.error}")
        return None, warnings

    payload = result.payload
    if not isinstance(payload, dict):
        warnings.append(f"backend.json: expected a JSON object, got {type(payload).__name__}")
        return None, warnings

    cost_model = payload.get("cost_model")
    currency = payload.get("currency")
    if not isinstance(cost_model, str) or not isinstance(currency, str):
        warnings.append("backend.json: missing or non-string cost_model/currency")
        return None, warnings

    unit = COST_MODEL_UNIT.get(cost_model)
    if unit is None:
        warnings.append(f"backend.json: unknown cost_model {cost_model!r}")
        unit = "unknown"

    return CostModelInfo(cost_model=cost_model, currency=currency, unit=unit), warnings


# ---------------------------------------------------------------------------
# Dataset discovery — describe what the data directory currently exposes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScaleInfo:
    granularity: str
    depth: str
    points_per_namespace: int


@dataclass(frozen=True)
class TrendsInfo:
    granularity: str
    depth: str
    points_per_namespace: int


@dataclass(frozen=True)
class EfficiencyInfo:
    aggregated: bool
    hourly_timeseries: bool


@dataclass(frozen=True)
class DatasetInfo:
    """Snapshot of the dataset currently exposed by ``static/data/``.

    Built from the **files that actually exist** at probe time. Depths and
    point counts are derived from the data — never hard-coded — so that the
    response stays honest if backend.py runs in a degraded mode (spec §7.2
    point 1, "honnêteté temporelle").
    """

    metrics_available: list[str]
    gpu_available: bool
    scales: dict[str, ScaleInfo]
    trends: TrendsInfo | None
    dimensions: list[str]
    efficiency: EfficiencyInfo
    cost_model: CostModelInfo | None
    billing_hourly_rate: float | None
    data_freshness_utc: str | None
    warnings: list[str] = field(default_factory=list)


def _unique_count(payload: Any, key: str) -> int:
    """Return the number of distinct values of ``key`` in a list payload."""
    if not isinstance(payload, list):
        return 0
    return len({entry[key] for entry in payload if isinstance(entry, dict) and key in entry})


def _iso_utc(epoch_seconds: float) -> str:
    """Format a POSIX timestamp as an ISO 8601 UTC string (no microseconds)."""
    return datetime.fromtimestamp(epoch_seconds, tz=UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _trends_depth(payload: Any) -> tuple[str | None, int]:
    """Derive (human-readable depth, points_per_namespace) from a trends payload.

    backend.py:dump_trend_data writes one entry per (namespace, hour) — so
    for any single namespace, ``points_per_namespace`` equals the number of
    distinct ``dateUTC`` timestamps. We use distinct timestamps over the whole
    file as an upper bound, which is what describe_dataset wants to expose.
    """
    if not isinstance(payload, list):
        return None, 0
    timestamps = sorted(
        {entry["dateUTC"] for entry in payload if isinstance(entry, dict) and isinstance(entry.get("dateUTC"), str)}
    )
    if not timestamps:
        return None, 0
    points = len(timestamps)
    if len(timestamps) < 2:
        return "1 hour", points
    try:
        first = datetime.fromisoformat(timestamps[0].replace("Z", "+00:00"))
        last = datetime.fromisoformat(timestamps[-1].replace("Z", "+00:00"))
    except ValueError:
        return None, points
    span_hours = (last - first).total_seconds() / 3600.0
    span_days = span_hours / 24.0
    if span_days >= 1.5:
        return f"{round(span_days):.0f} days", points
    return f"{round(span_hours):.0f} hours", points


def discover_dataset(data_dir: Path | None = None) -> DatasetInfo:
    """Probe ``static/data/`` and describe what is currently available.

    Never raises. Anything that prevents the discovery from completing
    cleanly is recorded as a warning in the returned :class:`DatasetInfo`.
    """
    root = data_dir if data_dir is not None else get_data_dir()
    warnings: list[str] = []

    cost_model, cm_warnings = load_cost_model(data_dir=root)
    warnings.extend(cm_warnings)

    # ------------------------------------------------------------------
    # Probe histograms — which (metric, dimension, scale, gpu) files exist.
    # ------------------------------------------------------------------

    probed: dict[str, FileReadResult] = {}

    def _probe(filename: str) -> FileReadResult:
        if filename not in probed:
            probed[filename] = read_json_file(filename, data_dir=root)
        return probed[filename]

    metrics_available: list[str] = []
    for metric in METRICS:
        for scale in (SCALE_DAILY_14D, SCALE_MONTHLY_12M):
            fname = histogram_filename(metric, "usage", SCALE_TO_PERIOD[scale])
            if _probe(fname).exists:
                if metric not in metrics_available:
                    metrics_available.append(metric)
                break

    gpu_available = False
    for metric in METRICS:
        for scale in (SCALE_DAILY_14D, SCALE_MONTHLY_12M):
            fname = histogram_filename(metric, "usage", SCALE_TO_PERIOD[scale], gpu=True)
            r = _probe(fname)
            if r.ok and isinstance(r.payload, list) and r.payload:
                gpu_available = True
                break
        if gpu_available:
            break

    dimensions: list[str] = []
    if metrics_available:
        if any(
            _probe(histogram_filename(m, "usage", SCALE_TO_PERIOD[s])).exists
            for m in metrics_available
            for s in (SCALE_DAILY_14D, SCALE_MONTHLY_12M)
        ):
            dimensions.append("usage")
        if any(
            _probe(histogram_filename(m, "requests", SCALE_TO_PERIOD[s])).exists
            for m in metrics_available
            for s in (SCALE_DAILY_14D, SCALE_MONTHLY_12M)
        ):
            dimensions.append("requests")

    # ------------------------------------------------------------------
    # Scales — depth and points derived from data when readable.
    # ------------------------------------------------------------------

    scales: dict[str, ScaleInfo] = {}
    for scale in (SCALE_DAILY_14D, SCALE_MONTHLY_12M):
        sample = None
        for metric in metrics_available or METRICS:
            r = _probe(histogram_filename(metric, "usage", SCALE_TO_PERIOD[scale]))
            if r.ok and isinstance(r.payload, list) and r.payload:
                sample = r
                break
        if sample is None:
            continue
        points = _unique_count(sample.payload, "date")
        scales[scale] = ScaleInfo(
            granularity=SCALE_GRANULARITY[scale],
            depth=SCALE_DEFAULT_DEPTH[scale],
            points_per_namespace=points,
        )

    # ------------------------------------------------------------------
    # Trends — depth derived from the first usage trends file we can read.
    # ------------------------------------------------------------------

    trends: TrendsInfo | None = None
    for metric in metrics_available or METRICS:
        r = _probe(trends_filename(metric, "usage"))
        if r.ok and isinstance(r.payload, list) and r.payload:
            depth, points = _trends_depth(r.payload)
            trends = TrendsInfo(
                granularity="hourly",
                depth=depth or "unknown",
                points_per_namespace=points,
            )
            break

    # ------------------------------------------------------------------
    # Efficiency capability — aggregated vs. hourly timeseries.
    # ------------------------------------------------------------------

    aggregated_efficiency = "requests" in dimensions
    hourly_rf = any(_probe(trends_filename(m, "rf")).exists for m in metrics_available or METRICS)
    efficiency = EfficiencyInfo(
        aggregated=aggregated_efficiency,
        hourly_timeseries=hourly_rf,
    )

    # ------------------------------------------------------------------
    # Data freshness — most recent mtime across the data files probed.
    # backend.json is excluded: it is written once at start-up and its
    # mtime does not reflect data freshness (spec §7.2 point 5).
    # ------------------------------------------------------------------

    mtimes = [
        r.mtime_utc for fname, r in probed.items() if r.mtime_utc is not None and fname != BACKEND_CONFIG_FILENAME
    ]
    data_freshness_utc = _iso_utc(max(mtimes)) if mtimes else None

    # ------------------------------------------------------------------
    # billing_hourly_rate — exposed only in cost_model=costs.
    # backend.py does not write this value into backend.json today, and it
    # filters the .billing-hourly-rate entry out of the histogram outputs
    # (see backend.py:1144). The value is therefore not reachable from the
    # static JSON surface in v1. We surface this gap as a warning rather
    # than guessing — the caller knows the field is unavailable.
    # ------------------------------------------------------------------

    billing_hourly_rate: float | None = None
    if cost_model is not None and cost_model.cost_model == "costs":
        warnings.append(
            "billing_hourly_rate is not exposed in the current static data set; "
            "backend.json carries only cost_model and currency."
        )

    return DatasetInfo(
        metrics_available=metrics_available,
        gpu_available=gpu_available,
        scales=scales,
        trends=trends,
        dimensions=dimensions,
        efficiency=efficiency,
        cost_model=cost_model,
        billing_hourly_rate=billing_hourly_rate,
        data_freshness_utc=data_freshness_utc,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Namespace classification & collection
# ---------------------------------------------------------------------------


def classify_namespace(name: str) -> str:
    """Return the type of a namespace-like entry seen in the data.

    Possible values:

    - ``"special"`` if ``name`` is a known special entry (``non-allocatable``,
      ``.billing-hourly-rate`` — see spec §2.4).
    - ``"system"`` if ``name`` starts with a known system prefix
      (``openshift-``, ``kube-``).
    - ``"application"`` otherwise.
    """
    if name in SPECIAL_ENTRIES:
        return "special"
    for prefix in SYSTEM_NAMESPACE_PREFIXES:
        if name.startswith(prefix):
            return "system"
    return "application"


def _iter_data_filenames(include_gpu: bool = True) -> list[str]:
    """Enumerate every data filename produced by backend.py:dump_analytics."""
    out: list[str] = []
    gpu_variants = (False, True) if include_gpu else (False,)
    for metric in METRICS:
        for period in (PERIOD_14_DAYS_SEC, PERIOD_YEAR_SEC):
            for dimension in DIMENSIONS:
                for gpu in gpu_variants:
                    out.append(histogram_filename(metric, dimension, period, gpu=gpu))
        for category in ("usage", "rf"):
            for gpu in gpu_variants:
                # backend.py emits no gpu_*_rf_trends.json — see spec §2.2.
                if gpu and category == "rf":
                    continue
                out.append(trends_filename(metric, category, gpu=gpu))
    return out


def _collect_namespace_names(
    data_dir: Path | None = None,
) -> tuple[set, list[str], float | None]:
    """Walk all data files, collect every entry name seen.

    Returns:
        ``(names, warnings, latest_mtime)`` where ``names`` is the union of
        every ``stack`` (histograms) and ``name`` (trends) field observed,
        ``warnings`` lists files that exist but failed to parse (recoverable),
        and ``latest_mtime`` is the most recent POSIX mtime across files
        actually read — used to compute ``generated_at_utc``.

    """
    root = data_dir if data_dir is not None else get_data_dir()
    names: set = set()
    warnings: list[str] = []
    latest_mtime: float | None = None

    for fname in _iter_data_filenames():
        result = read_json_file(fname, data_dir=root)
        if not result.exists:
            continue
        if result.mtime_utc is not None:
            latest_mtime = result.mtime_utc if latest_mtime is None else max(latest_mtime, result.mtime_utc)
        if not result.ok:
            warnings.append(f"{fname}: {result.error}")
            continue
        if not isinstance(result.payload, list):
            warnings.append(f"{fname}: expected a list at top level")
            continue
        for entry in result.payload:
            if not isinstance(entry, dict):
                continue
            stack = entry.get("stack")
            if isinstance(stack, str):
                names.add(stack)
            tname = entry.get("name")
            if isinstance(tname, str):
                names.add(tname)

    return names, warnings, latest_mtime


# ---------------------------------------------------------------------------
# Common response metadata block (spec §4.6)
# ---------------------------------------------------------------------------


def build_metadata(
    cost_model: CostModelInfo | None,
    warnings: list[str] | None = None,
    metric: str | None = None,
    scale: str | None = None,
    data_window: dict[str, Any] | None = None,
    source_file: str | None = None,
    generated_at_mtime: float | None = None,
    unit_override: str | None = None,
) -> dict[str, Any]:
    """Build the metadata block returned with every tool response.

    ``cost_model`` / ``currency`` / ``unit`` are global (derived from
    ``backend.json``). The remaining fields are tool-specific and omitted
    when not applicable. Always returns the same shape — unused fields are
    set to ``null`` so the schema is predictable for clients.

    A freshness warning is appended automatically when ``generated_at_mtime``
    is older than :data:`FRESHNESS_WARN_AFTER_SECONDS`.
    """
    out_warnings = list(warnings) if warnings else []

    if generated_at_mtime is not None:
        generated_at_utc = _iso_utc(generated_at_mtime)
        age_sec = datetime.now(tz=UTC).timestamp() - generated_at_mtime
        if age_sec > FRESHNESS_WARN_AFTER_SECONDS:
            out_warnings.append(f"stale data: most recent file is {age_sec / 60.0:.0f} minutes old")
    else:
        generated_at_utc = None

    return {
        "cost_model": cost_model.cost_model if cost_model else None,
        "currency": cost_model.currency if cost_model else None,
        "unit": unit_override or (cost_model.unit if cost_model else None),
        "metric": metric,
        "scale": scale,
        "data_window": data_window,
        "source_file": source_file,
        "generated_at_utc": generated_at_utc,
        "warnings": out_warnings,
    }


# ---------------------------------------------------------------------------
# Tools — Discovery group (spec §4.1)
# ---------------------------------------------------------------------------


def tool_list_namespaces(data_dir: Path | None = None) -> dict[str, Any]:
    """Implement the ``list_namespaces`` MCP tool.

    Returns the real namespaces seen across all data files (classified as
    ``application`` or ``system``) and the known special entries — listed
    explicitly so the client cannot confuse them with namespaces.
    """
    root = data_dir if data_dir is not None else get_data_dir()
    names, warnings, latest_mtime = _collect_namespace_names(data_dir=root)
    cost_model, cm_warnings = load_cost_model(data_dir=root)
    warnings.extend(cm_warnings)

    namespaces: list[dict[str, str]] = []
    counts = {"application": 0, "system": 0, "special": 0}
    for name in sorted(names):
        kind = classify_namespace(name)
        if kind == "special":
            # Special entries are surfaced in their own block, not mingled
            # with the namespace list. We intentionally do NOT silently drop
            # them — they are accounted for via SPECIAL_ENTRIES below.
            continue
        namespaces.append({"name": name, "type": kind})
        counts[kind] += 1

    special_entries: list[dict[str, str]] = []
    for entry_name, meta in SPECIAL_ENTRIES.items():
        special_entries.append(
            {
                "name": entry_name,
                "role": meta["role"],
                "description": meta["description"],
            }
        )
    counts["special"] = len(special_entries)

    return {
        "namespaces": namespaces,
        "special_entries": special_entries,
        "counts": counts,
        "metadata": build_metadata(
            cost_model=cost_model,
            warnings=warnings,
            generated_at_mtime=latest_mtime,
        ),
    }


def tool_describe_dataset(data_dir: Path | None = None) -> dict[str, Any]:
    """Implement the ``describe_dataset`` MCP tool.

    Announces what the dataset currently exposes — metrics, GPU availability,
    scales (with actual point counts derived from the data), trend depth,
    available dimensions, efficiency capability, cost model. Intended to be
    called first by any well-behaved client so it does not over-promise.
    """
    info = discover_dataset(data_dir=data_dir)

    scales_out: dict[str, dict[str, Any]] = {}
    for scale_key, scale_info in info.scales.items():
        scales_out[scale_key] = {
            "granularity": scale_info.granularity,
            "depth": scale_info.depth,
            "points_per_namespace": scale_info.points_per_namespace,
        }

    trends_out: dict[str, Any] | None = None
    if info.trends is not None:
        trends_out = {
            "granularity": info.trends.granularity,
            "depth": info.trends.depth,
            "points_per_namespace": info.trends.points_per_namespace,
        }

    # Convert the data_freshness ISO string back to a mtime for the metadata
    # helper — keeps freshness warning logic centralised.
    freshness_mtime: float | None = None
    if info.data_freshness_utc is not None:
        try:
            freshness_mtime = datetime.fromisoformat(info.data_freshness_utc.replace("Z", "+00:00")).timestamp()
        except ValueError:
            freshness_mtime = None

    return {
        "metrics_available": info.metrics_available,
        "gpu_available": info.gpu_available,
        "scales": scales_out,
        "trends": trends_out,
        "dimensions": info.dimensions,
        "efficiency": {
            "aggregated": info.efficiency.aggregated,
            "hourly_timeseries": info.efficiency.hourly_timeseries,
        },
        "cost_model": info.cost_model.cost_model if info.cost_model else None,
        "currency": info.cost_model.currency if info.cost_model else None,
        "billing_hourly_rate": info.billing_hourly_rate,
        "data_freshness_utc": info.data_freshness_utc,
        "metadata": build_metadata(
            cost_model=info.cost_model,
            warnings=info.warnings,
            generated_at_mtime=freshness_mtime,
        ),
    }


# ---------------------------------------------------------------------------
# Histogram loading & filtering helpers (shared by §4.2 tools)
# ---------------------------------------------------------------------------


def _is_special(name: str) -> bool:
    return name in SPECIAL_ENTRIES


def _is_system(name: str) -> bool:
    return any(name.startswith(p) for p in SYSTEM_NAMESPACE_PREFIXES)


def _validate_metric(metric: str) -> None:
    if metric not in METRICS:
        raise ValueError(f"unknown metric {metric!r}: expected one of {list(METRICS)}")


def _validate_scale(scale: str) -> None:
    if scale not in SCALE_TO_PERIOD:
        raise ValueError(f"unknown scale {scale!r}: expected one of {list(SCALE_TO_PERIOD)}")


def _load_histogram_payload(
    metric: str,
    scale: str,
    dimension: str = "usage",
    gpu: bool = False,
    data_dir: Path | None = None,
) -> tuple[list[dict[str, Any]] | None, str, float | None, str | None]:
    """Load a histogram file by (metric, scale, dimension, gpu).

    Returns:
        ``(entries, filename, mtime_utc, error)``. ``entries`` is ``None``
        when the file is missing, malformed, or has the wrong shape; the
        caller surfaces ``error`` in the response so the client can decide.

    """
    period = SCALE_TO_PERIOD[scale]
    fname = histogram_filename(metric, dimension, period, gpu=gpu)
    result = read_json_file(fname, data_dir=data_dir)
    if not result.ok:
        return None, fname, result.mtime_utc, result.error
    if not isinstance(result.payload, list):
        return None, fname, result.mtime_utc, f"{fname}: expected JSON array at top level"
    return result.payload, fname, result.mtime_utc, None


def _unique_dates_in_order(entries: list[dict[str, Any]]) -> list[str]:
    """Distinct ``date`` values in the order they first appear in the file.

    backend.py emits entries in chronological order over RRD consolidated
    data points, so the first occurrence order is chronological — taking
    the last element here means "most recent date in the data".
    """
    seen: list[str] = []
    seen_set: set = set()
    for e in entries:
        d = e.get("date")
        if isinstance(d, str) and d not in seen_set:
            seen.append(d)
            seen_set.add(d)
    return seen


def _coerce_value(entry: dict[str, Any]) -> float | None:
    """Extract the numeric usage value of an entry, or None if unusable."""
    v = entry.get("usage")
    if isinstance(v, (int, float)):
        return float(v)
    return None


def _build_data_window(
    scale: str,
    dates_in_order: list[str],
    selected_date: str | None = None,
) -> dict[str, Any]:
    """Assemble the ``data_window`` block for histogram-backed responses."""
    if selected_date is not None:
        return {
            "granularity": SCALE_GRANULARITY[scale],
            "start": selected_date,
            "end": selected_date,
            "points": 1,
            "timezone": "UTC",
        }
    return {
        "granularity": SCALE_GRANULARITY[scale],
        "start": dates_in_order[0] if dates_in_order else None,
        "end": dates_in_order[-1] if dates_in_order else None,
        "points": len(dates_in_order),
        "timezone": "UTC",
    }


def _round(value: float, decimals: int = 6) -> float:
    return round(float(value), decimals)


# ---------------------------------------------------------------------------
# Tools — Consolidations group (spec §4.2)
# ---------------------------------------------------------------------------


def tool_get_usage(
    metric: str,
    scale: str,
    namespace: str | None = None,
    exclude_special: bool = True,
    data_dir: Path | None = None,
) -> dict[str, Any]:
    """Implement the ``get_usage`` MCP tool — usage per namespace.

    Returns the time series ``{namespace, date, value}`` from the histogram
    file for the requested (metric, scale). System namespaces are NOT
    filtered (callers see them) — only special entries are excluded when
    ``exclude_special`` is True (the default, per spec §2.4).
    """
    _validate_metric(metric)
    _validate_scale(scale)

    cost_model, warnings = load_cost_model(data_dir=data_dir)
    entries, fname, mtime, err = _load_histogram_payload(
        metric=metric,
        scale=scale,
        dimension="usage",
        data_dir=data_dir,
    )
    if entries is None:
        warnings.append(f"{fname}: {err or 'histogram unavailable'}")
        return {
            "metric": metric,
            "scale": scale,
            "namespace": namespace,
            "series": [],
            "metadata": build_metadata(
                cost_model=cost_model,
                warnings=warnings,
                metric=metric,
                scale=scale,
                source_file=fname,
                generated_at_mtime=mtime,
            ),
        }

    series: list[dict[str, Any]] = []
    for entry in entries:
        stack = entry.get("stack")
        if not isinstance(stack, str):
            continue
        if exclude_special and _is_special(stack):
            continue
        if namespace is not None and stack != namespace:
            continue
        value = _coerce_value(entry)
        date_key = entry.get("date")
        if value is None or not isinstance(date_key, str):
            continue
        series.append(
            {
                "namespace": stack,
                "date": date_key,
                "value": _round(value),
            }
        )

    dates = _unique_dates_in_order(entries)
    return {
        "metric": metric,
        "scale": scale,
        "namespace": namespace,
        "series": series,
        "metadata": build_metadata(
            cost_model=cost_model,
            warnings=warnings,
            metric=metric,
            scale=scale,
            data_window=_build_data_window(scale, dates),
            source_file=fname,
            generated_at_mtime=mtime,
        ),
    }


def tool_get_top_consumers(
    metric: str,
    scale: str,
    limit: int = 10,
    exclude_system: bool = True,
    date: str | None = None,
    data_dir: Path | None = None,
) -> dict[str, Any]:
    """Implement the ``get_top_consumers`` MCP tool.

    Ranks namespaces by usage descending for the selected date (last
    available by default). Special entries are ALWAYS excluded — letting
    ``non-allocatable`` top the ranking would defeat the purpose (spec §2.4).
    ``exclude_system`` only controls whether system namespaces appear.
    """
    _validate_metric(metric)
    _validate_scale(scale)
    if limit < 1:
        raise ValueError(f"limit must be >= 1, got {limit}")

    cost_model, warnings = load_cost_model(data_dir=data_dir)
    entries, fname, mtime, err = _load_histogram_payload(
        metric=metric,
        scale=scale,
        dimension="usage",
        data_dir=data_dir,
    )
    if entries is None:
        warnings.append(f"{fname}: {err or 'histogram unavailable'}")
        return {
            "metric": metric,
            "scale": scale,
            "date": date,
            "ranking": [],
            "metadata": build_metadata(
                cost_model=cost_model,
                warnings=warnings,
                metric=metric,
                scale=scale,
                source_file=fname,
                generated_at_mtime=mtime,
            ),
        }

    dates = _unique_dates_in_order(entries)
    target_date = date if date is not None else (dates[-1] if dates else None)
    if target_date is None:
        warnings.append(f"no date available in {fname}")
        return {
            "metric": metric,
            "scale": scale,
            "date": None,
            "ranking": [],
            "metadata": build_metadata(
                cost_model=cost_model,
                warnings=warnings,
                metric=metric,
                scale=scale,
                source_file=fname,
                generated_at_mtime=mtime,
            ),
        }
    if date is not None and date not in dates:
        warnings.append(f"date {date!r} not present in {fname} (available: {dates})")

    candidates: list[tuple[str, float]] = []
    for entry in entries:
        if entry.get("date") != target_date:
            continue
        stack = entry.get("stack")
        if not isinstance(stack, str):
            continue
        if _is_special(stack):
            continue  # always excluded from rankings
        if exclude_system and _is_system(stack):
            continue
        value = _coerce_value(entry)
        if value is None:
            continue
        candidates.append((stack, value))

    candidates.sort(key=lambda item: item[1], reverse=True)
    total = sum(v for _, v in candidates)
    effective_limit = min(limit, len(candidates))

    ranking: list[dict[str, Any]] = []
    for idx, (ns, value) in enumerate(candidates[:effective_limit], start=1):
        share_pct = (100.0 * value / total) if total > 0 else 0.0
        ranking.append(
            {
                "rank": idx,
                "namespace": ns,
                "value": _round(value),
                "share_pct": round(share_pct, 2),
            }
        )

    return {
        "metric": metric,
        "scale": scale,
        "date": target_date,
        "ranking": ranking,
        "metadata": build_metadata(
            cost_model=cost_model,
            warnings=warnings,
            metric=metric,
            scale=scale,
            data_window=_build_data_window(scale, dates, selected_date=target_date),
            source_file=fname,
            generated_at_mtime=mtime,
        ),
    }


def tool_get_namespace_breakdown(
    metric: str,
    scale: str,
    date: str | None = None,
    exclude_system: bool = True,
    data_dir: Path | None = None,
) -> dict[str, Any]:
    """Implement the ``get_namespace_breakdown`` MCP tool.

    Returns the full ranked breakdown of namespaces (not limited), plus
    concentration indicators (top-3 / top-10 share) and a ``cluster_overhead``
    block that surfaces ``non-allocatable`` separately — never mingled with
    namespaces (spec §4.2). ``cluster_overhead.non_allocatable_share_pct``
    and ``allocatable_share_pct`` are computed against the total cluster
    (overhead + namespaces) and sum to 100 %.
    """
    _validate_metric(metric)
    _validate_scale(scale)

    cost_model, warnings = load_cost_model(data_dir=data_dir)
    entries, fname, mtime, err = _load_histogram_payload(
        metric=metric,
        scale=scale,
        dimension="usage",
        data_dir=data_dir,
    )
    if entries is None:
        warnings.append(f"{fname}: {err or 'histogram unavailable'}")
        return {
            "metric": metric,
            "scale": scale,
            "date": date,
            "breakdown": [],
            "concentration": {"top_3_share_pct": 0.0, "top_10_share_pct": 0.0, "total_namespaces": 0},
            "cluster_overhead": None,
            "metadata": build_metadata(
                cost_model=cost_model,
                warnings=warnings,
                metric=metric,
                scale=scale,
                source_file=fname,
                generated_at_mtime=mtime,
            ),
        }

    dates = _unique_dates_in_order(entries)
    target_date = date if date is not None else (dates[-1] if dates else None)
    if target_date is None:
        warnings.append(f"no date available in {fname}")
        return {
            "metric": metric,
            "scale": scale,
            "date": None,
            "breakdown": [],
            "concentration": {"top_3_share_pct": 0.0, "top_10_share_pct": 0.0, "total_namespaces": 0},
            "cluster_overhead": None,
            "metadata": build_metadata(
                cost_model=cost_model,
                warnings=warnings,
                metric=metric,
                scale=scale,
                source_file=fname,
                generated_at_mtime=mtime,
            ),
        }
    if date is not None and date not in dates:
        warnings.append(f"date {date!r} not present in {fname} (available: {dates})")

    non_allocatable_value: float | None = None
    namespace_values: list[tuple[str, float]] = []
    for entry in entries:
        if entry.get("date") != target_date:
            continue
        stack = entry.get("stack")
        if not isinstance(stack, str):
            continue
        value = _coerce_value(entry)
        if value is None:
            continue
        if stack == "non-allocatable":
            non_allocatable_value = value
            continue
        if _is_special(stack):
            continue  # never in breakdown
        if exclude_system and _is_system(stack):
            continue
        namespace_values.append((stack, value))

    namespace_values.sort(key=lambda item: item[1], reverse=True)
    namespace_total = sum(v for _, v in namespace_values)

    breakdown: list[dict[str, Any]] = []
    for ns, value in namespace_values:
        share_pct = (100.0 * value / namespace_total) if namespace_total > 0 else 0.0
        breakdown.append(
            {
                "namespace": ns,
                "value": _round(value),
                "share_pct": round(share_pct, 2),
            }
        )

    top_3_sum = sum(v for _, v in namespace_values[:3])
    top_10_sum = sum(v for _, v in namespace_values[:10])
    concentration = {
        "top_3_share_pct": round(100.0 * top_3_sum / namespace_total, 2) if namespace_total > 0 else 0.0,
        "top_10_share_pct": round(100.0 * top_10_sum / namespace_total, 2) if namespace_total > 0 else 0.0,
        "total_namespaces": len(breakdown),
    }

    cluster_overhead: dict[str, Any] | None = None
    if non_allocatable_value is not None:
        total_cluster = non_allocatable_value + namespace_total
        if total_cluster > 0:
            cluster_overhead = {
                "non_allocatable_value": _round(non_allocatable_value),
                "non_allocatable_share_pct": round(100.0 * non_allocatable_value / total_cluster, 2),
                "allocatable_share_pct": round(100.0 * namespace_total / total_cluster, 2),
            }
        else:
            warnings.append("non-allocatable present but total cluster value is zero; cluster_overhead omitted")
    else:
        warnings.append(f"non-allocatable absent from data for date {target_date!r}; cluster_overhead omitted")

    return {
        "metric": metric,
        "scale": scale,
        "date": target_date,
        "breakdown": breakdown,
        "concentration": concentration,
        "cluster_overhead": cluster_overhead,
        "metadata": build_metadata(
            cost_model=cost_model,
            warnings=warnings,
            metric=metric,
            scale=scale,
            data_window=_build_data_window(scale, dates, selected_date=target_date),
            source_file=fname,
            generated_at_mtime=mtime,
        ),
    }


# ---------------------------------------------------------------------------
# Tools — Efficiency group (spec §4.3)
# ---------------------------------------------------------------------------


def _classify_efficiency(ratio: float) -> str:
    """Descriptive classification — never prescriptive (spec §4.3)."""
    if ratio < EFFICIENCY_OVER_PROVISIONED_BELOW:
        return "over_provisioned"
    if ratio > EFFICIENCY_UNDER_PROVISIONED_ABOVE:
        return "under_provisioned"
    return "balanced"


def _aggregate_by_namespace(
    entries: list[dict[str, Any]],
) -> dict[str, float]:
    """Sum ``usage`` values per namespace across all dates, skipping specials."""
    totals: dict[str, float] = collections.defaultdict(float)
    for entry in entries:
        stack = entry.get("stack")
        if not isinstance(stack, str):
            continue
        if _is_special(stack):
            continue
        value = _coerce_value(entry)
        if value is None:
            continue
        totals[stack] += value
    return totals


def tool_get_efficiency(
    scale: str,
    namespace: str | None = None,
    metric: str | None = None,
    data_dir: Path | None = None,
) -> dict[str, Any]:
    """Implement the ``get_efficiency`` MCP tool.

    Crosses the ``*_usage_*`` and ``*_requests_*`` histograms to compute the
    ratio ``usage / requests`` per (namespace, metric) at scale level. The
    ratio is **descriptive**: a classification ``over_provisioned`` /
    ``balanced`` / ``under_provisioned`` is provided but no recommendation
    is made (spec §4.3).

    Tolerance to zero (§4.3): ``requests`` is derived from ``usage / rf``,
    so a high ``rf`` produces an infinitesimal — not strictly zero —
    aggregate. Any aggregate below :data:`REQUESTS_ZERO_THRESHOLD` is
    treated as zero; the ratio becomes ``null`` and a warning is added.
    """
    _validate_scale(scale)
    if metric is not None:
        _validate_metric(metric)

    metrics_filter = [metric] if metric is not None else list(METRICS)

    cost_model, warnings = load_cost_model(data_dir=data_dir)
    efficiency: list[dict[str, Any]] = []
    source_files: list[str] = []
    latest_mtime: float | None = None
    data_window: dict[str, Any] | None = None

    for current_metric in metrics_filter:
        usage_entries, usage_fname, usage_mtime, usage_err = _load_histogram_payload(
            metric=current_metric,
            scale=scale,
            dimension="usage",
            data_dir=data_dir,
        )
        if usage_entries is None:
            warnings.append("{}: {}".format(usage_fname, usage_err or "unavailable"))
            continue
        source_files.append(usage_fname)
        if usage_mtime is not None:
            latest_mtime = usage_mtime if latest_mtime is None else max(latest_mtime, usage_mtime)

        req_entries, req_fname, req_mtime, req_err = _load_histogram_payload(
            metric=current_metric,
            scale=scale,
            dimension="requests",
            data_dir=data_dir,
        )
        if req_entries is None:
            warnings.append("{}: {}".format(req_fname, req_err or "unavailable"))
            continue
        source_files.append(req_fname)
        if req_mtime is not None:
            latest_mtime = req_mtime if latest_mtime is None else max(latest_mtime, req_mtime)

        # Capture the data window once — both files share the same dates by
        # construction (backend.py:1141 — same iteration over usage dates).
        if data_window is None:
            dates = _unique_dates_in_order(usage_entries)
            data_window = _build_data_window(scale, dates)

        usage_totals = _aggregate_by_namespace(usage_entries)
        requests_totals = _aggregate_by_namespace(req_entries)

        all_namespaces = set(usage_totals) | set(requests_totals)
        if namespace is not None:
            if namespace not in all_namespaces:
                warnings.append(f"namespace {namespace!r} not present in {current_metric} histograms")
                continue
            all_namespaces = {namespace}

        for ns in sorted(all_namespaces):
            usage_value = usage_totals.get(ns, 0.0)
            requests_value = requests_totals.get(ns, 0.0)

            if requests_value < REQUESTS_ZERO_THRESHOLD:
                ratio: float | None = None
                classification: str | None = None
                warnings.append(
                    f"{ns}/{current_metric}: requests aggregate below {REQUESTS_ZERO_THRESHOLD:g} threshold; "
                    "efficiency_ratio set to null"
                )
            else:
                ratio = usage_value / requests_value
                classification = _classify_efficiency(ratio)

            efficiency.append(
                {
                    "namespace": ns,
                    "metric": current_metric,
                    "usage": _round(usage_value),
                    "requests": _round(requests_value),
                    "efficiency_ratio": _round(ratio) if ratio is not None else None,
                    "classification": classification,
                }
            )

    # Stable order: (namespace, metric) — same namespace's metrics stay adjacent.
    efficiency.sort(key=lambda e: (e["namespace"], e["metric"]))

    # source_file is a single string in the metadata block — join the
    # contributing files so the trail is preserved without changing shape.
    source_file = ", ".join(source_files) if source_files else None

    return {
        "scale": scale,
        "namespace": namespace,
        "metric": metric,
        "efficiency": efficiency,
        "metadata": build_metadata(
            cost_model=cost_model,
            warnings=warnings,
            scale=scale,
            metric=metric,
            data_window=data_window,
            source_file=source_file,
            generated_at_mtime=latest_mtime,
            # The headline number in this response is a dimensionless ratio.
            # Override the cost_model-derived unit so clients don't mistake
            # the ratio for the cost_model's unit.
            unit_override="efficiency_ratio",
        ),
    }


# ---------------------------------------------------------------------------
# Tools — Trends group (spec §4.4)
# ---------------------------------------------------------------------------


def _validate_namespace(namespace: str) -> None:
    if not isinstance(namespace, str) or not namespace:
        raise ValueError("namespace must be a non-empty string")


def _load_trends_payload(
    metric: str,
    category: str = "usage",
    gpu: bool = False,
    data_dir: Path | None = None,
) -> tuple[list[dict[str, Any]] | None, str, float | None, str | None]:
    """Load a trends file by (metric, category, gpu).

    backend.py emits ``*_<category>_trends.json`` with hourly entries
    ``{name, dateUTC, usage}`` over a 7-day window. The value field is named
    ``usage`` even in ``*_rf_trends.json`` (it carries the efficiency ratio
    there — the filename, not the field, encodes the semantics; spec §2.2).
    """
    fname = trends_filename(metric, category, gpu=gpu)
    result = read_json_file(fname, data_dir=data_dir)
    if not result.ok:
        return None, fname, result.mtime_utc, result.error
    if not isinstance(result.payload, list):
        return None, fname, result.mtime_utc, f"{fname}: expected JSON array at top level"
    return result.payload, fname, result.mtime_utc, None


def _filter_trends_namespace(
    entries: list[dict[str, Any]],
    namespace: str,
) -> tuple[list[dict[str, Any]], list[float]]:
    """Extract ``(series, raw_values)`` for one namespace from trends entries.

    Preserves the file order — backend.py emits points chronologically per
    namespace, so the resulting series is monotonic in time without an
    explicit sort.
    """
    series: list[dict[str, Any]] = []
    values: list[float] = []
    for entry in entries:
        if entry.get("name") != namespace:
            continue
        dt = entry.get("dateUTC")
        v = entry.get("usage")
        if not isinstance(dt, str) or not isinstance(v, (int, float)):
            continue
        value = float(v)
        series.append({"dateUTC": dt, "usage": _round(value)})
        values.append(value)
    return series, values


def _compute_trend_stats(
    series: list[dict[str, Any]],
    values: list[float],
) -> dict[str, Any]:
    """Min / max / mean / window bounds for a timeseries — null when empty."""
    if not values or not series:
        return {
            "min": None,
            "max": None,
            "mean": None,
            "points": 0,
            "window_start_utc": None,
            "window_end_utc": None,
        }
    return {
        "min": _round(min(values)),
        "max": _round(max(values)),
        "mean": _round(sum(values) / len(values)),
        "points": len(values),
        "window_start_utc": series[0]["dateUTC"],
        "window_end_utc": series[-1]["dateUTC"],
    }


def _trends_data_window(stats: dict[str, Any]) -> dict[str, Any] | None:
    if stats["points"] <= 0:
        return None
    return {
        "granularity": "hourly",
        "start": stats["window_start_utc"],
        "end": stats["window_end_utc"],
        "points": stats["points"],
        "timezone": "UTC",
    }


def _classify_trend(values: list[float]) -> str:
    """Return an indicative direction for a monthly series — descriptive only.

    Splits the series into halves and compares means with a 10 % relative
    threshold against the first half's magnitude. Returns one of
    ``"growing"``, ``"stable"``, ``"decreasing"``, or ``"insufficient_data"``
    when there are fewer than two points.
    """
    if len(values) < 2:
        return "insufficient_data"
    mid = len(values) // 2
    first_half = values[:mid]
    second_half = values[mid:]
    if not first_half or not second_half:
        return "insufficient_data"
    first_mean = sum(first_half) / len(first_half)
    second_mean = sum(second_half) / len(second_half)
    if first_mean == 0:
        # Usage values in this domain are non-negative (sum of RRD CDPs
        # rounded to 6 decimals — see backend.py:dump_histogram_analytics),
        # so second_mean is either zero (stable) or positive (growing).
        return "growing" if second_mean > 0 else "stable"
    delta_ratio = (second_mean - first_mean) / abs(first_mean)
    if delta_ratio > TREND_CHANGE_THRESHOLD:
        return "growing"
    if delta_ratio < -TREND_CHANGE_THRESHOLD:
        return "decreasing"
    return "stable"


def tool_get_timeseries(
    metric: str,
    namespace: str,
    data_dir: Path | None = None,
) -> dict[str, Any]:
    """Implement the ``get_timeseries`` MCP tool.

    Returns the hourly usage series over the trends window (~7 days in v1)
    for a single namespace, plus simple pre-computed statistics. Depth is
    derived from the data — not hard-coded — so the response stays accurate
    if backend.py later widens or narrows the trends retention.
    """
    _validate_metric(metric)
    _validate_namespace(namespace)

    cost_model, warnings = load_cost_model(data_dir=data_dir)
    entries, fname, mtime, err = _load_trends_payload(
        metric=metric,
        category="usage",
        data_dir=data_dir,
    )
    if entries is None:
        warnings.append("{}: {}".format(fname, err or "unavailable"))
        empty_stats = _compute_trend_stats([], [])
        return {
            "namespace": namespace,
            "metric": metric,
            "series": [],
            "stats": empty_stats,
            "metadata": build_metadata(
                cost_model=cost_model,
                warnings=warnings,
                metric=metric,
                source_file=fname,
                generated_at_mtime=mtime,
            ),
        }

    series, values = _filter_trends_namespace(entries, namespace=namespace)
    if not series:
        warnings.append(f"namespace {namespace!r} not present in {fname} (no data points)")
    stats = _compute_trend_stats(series, values)

    return {
        "namespace": namespace,
        "metric": metric,
        "series": series,
        "stats": stats,
        "metadata": build_metadata(
            cost_model=cost_model,
            warnings=warnings,
            metric=metric,
            data_window=_trends_data_window(stats),
            source_file=fname,
            generated_at_mtime=mtime,
        ),
    }


def tool_get_efficiency_timeseries(
    metric: str,
    namespace: str,
    data_dir: Path | None = None,
) -> dict[str, Any]:
    """Implement the ``get_efficiency_timeseries`` MCP tool.

    Same shape as :func:`tool_get_timeseries`, sourced from
    ``*_rf_trends.json``. The numeric value at each hour is the efficiency
    factor (``rf = usage / requests``) — not bounded by 1; values > 1 mean
    usage exceeds requests (under-provisioning), per spec §4.4. The unit
    in the metadata is set to ``efficiency_ratio`` so the client cannot
    confuse it with the cost_model's unit.
    """
    _validate_metric(metric)
    _validate_namespace(namespace)

    cost_model, warnings = load_cost_model(data_dir=data_dir)
    entries, fname, mtime, err = _load_trends_payload(
        metric=metric,
        category="rf",
        data_dir=data_dir,
    )
    if entries is None:
        warnings.append("{}: {}".format(fname, err or "unavailable"))
        empty_stats = _compute_trend_stats([], [])
        return {
            "namespace": namespace,
            "metric": metric,
            "series": [],
            "stats": empty_stats,
            "metadata": build_metadata(
                cost_model=cost_model,
                warnings=warnings,
                metric=metric,
                source_file=fname,
                generated_at_mtime=mtime,
                unit_override="efficiency_ratio",
            ),
        }

    series, values = _filter_trends_namespace(entries, namespace=namespace)
    if not series:
        warnings.append(f"namespace {namespace!r} not present in {fname} (no data points)")
    stats = _compute_trend_stats(series, values)

    return {
        "namespace": namespace,
        "metric": metric,
        "series": series,
        "stats": stats,
        "metadata": build_metadata(
            cost_model=cost_model,
            warnings=warnings,
            metric=metric,
            data_window=_trends_data_window(stats),
            source_file=fname,
            generated_at_mtime=mtime,
            unit_override="efficiency_ratio",
        ),
    }


def _namespace_monthly_trajectory(
    entries: list[dict[str, Any]],
    namespace: str,
) -> tuple[list[dict[str, Any]], list[float]]:
    """Per-namespace monthly series from a year-window histogram payload."""
    series: list[dict[str, Any]] = []
    values: list[float] = []
    for entry in entries:
        if entry.get("stack") != namespace:
            continue
        d = entry.get("date")
        v = _coerce_value(entry)
        if not isinstance(d, str) or v is None:
            continue
        series.append({"date": d, "value": _round(v)})
        values.append(v)
    return series, values


def _namespace_period_aggregate(
    entries: list[dict[str, Any]],
    namespace: str,
) -> tuple[float, list[str]]:
    """Sum a namespace's values across a histogram + return distinct dates."""
    total = 0.0
    dates: list[str] = []
    seen: set = set()
    for entry in entries:
        if entry.get("stack") != namespace:
            continue
        v = _coerce_value(entry)
        if v is None:
            continue
        total += v
        d = entry.get("date")
        if isinstance(d, str) and d not in seen:
            dates.append(d)
            seen.add(d)
    return total, dates


def tool_compare_periods(
    metric: str,
    namespace: str,
    data_dir: Path | None = None,
) -> dict[str, Any]:
    """Implement the ``compare_periods`` MCP tool.

    Reads the 14-day daily histogram and the year-window monthly histogram
    for a single namespace and presents both side-by-side: aggregate over
    14 days, plus the monthly trajectory with a descriptive direction
    (growing / stable / decreasing / insufficient_data). Strictly
    descriptive — no prediction is attempted (spec §4.4).
    """
    _validate_metric(metric)
    _validate_namespace(namespace)

    cost_model, warnings = load_cost_model(data_dir=data_dir)

    daily_entries, daily_fname, daily_mtime, daily_err = _load_histogram_payload(
        metric=metric,
        scale=SCALE_DAILY_14D,
        dimension="usage",
        data_dir=data_dir,
    )
    monthly_entries, monthly_fname, monthly_mtime, monthly_err = _load_histogram_payload(
        metric=metric,
        scale=SCALE_MONTHLY_12M,
        dimension="usage",
        data_dir=data_dir,
    )

    daily_block: dict[str, Any] | None = None
    if daily_entries is None:
        warnings.append("{}: {}".format(daily_fname, daily_err or "unavailable"))
    else:
        total, dates = _namespace_period_aggregate(daily_entries, namespace)
        if not dates:
            warnings.append(f"namespace {namespace!r} not present in {daily_fname}")
        daily_block = {
            "aggregate": _round(total),
            "window_start": dates[0] if dates else None,
            "window_end": dates[-1] if dates else None,
            "points": len(dates),
        }

    monthly_block: dict[str, Any] | None = None
    if monthly_entries is None:
        warnings.append("{}: {}".format(monthly_fname, monthly_err or "unavailable"))
    else:
        series, values = _namespace_monthly_trajectory(monthly_entries, namespace)
        if not series:
            warnings.append(f"namespace {namespace!r} not present in {monthly_fname}")
        monthly_block = {
            "series": series,
            "trend_direction": _classify_trend(values),
        }

    latest_mtime: float | None = None
    for mt in (daily_mtime, monthly_mtime):
        if mt is not None:
            latest_mtime = mt if latest_mtime is None else max(latest_mtime, mt)
    source_files = [f for f in (daily_fname, monthly_fname) if f]
    source_file = ", ".join(source_files) if source_files else None

    return {
        "namespace": namespace,
        "metric": metric,
        "daily_14d": daily_block,
        "monthly_12m": monthly_block,
        "metadata": build_metadata(
            cost_model=cost_model,
            warnings=warnings,
            metric=metric,
            source_file=source_file,
            generated_at_mtime=latest_mtime,
        ),
    }


# ---------------------------------------------------------------------------
# Tools — Utility group (spec §4.5)
# ---------------------------------------------------------------------------


def tool_group_namespaces(
    metric: str,
    scale: str,
    pattern: str,
    date: str | None = None,
    data_dir: Path | None = None,
) -> dict[str, Any]:
    """Implement the ``group_namespaces`` MCP tool.

    Aggregates namespaces whose names match a glob ``pattern`` (Unix-style,
    backed by :mod:`fnmatch` — same convention as backend.py uses for its
    include/exclude lists). Returns the group's total, the matched count,
    and a per-member breakdown sorted by value descending.

    Special entries (``non-allocatable``, ``.billing-hourly-rate``) are
    always excluded — letting them match a wildcard like ``*`` would inflate
    the group total with non-usage values (spec §2.4).
    """
    _validate_metric(metric)
    _validate_scale(scale)
    if not isinstance(pattern, str) or not pattern:
        raise ValueError("pattern must be a non-empty string")
    if len(pattern) > GROUP_PATTERN_MAX_LENGTH:
        raise ValueError(
            f"pattern length {len(pattern)} exceeds maximum of {GROUP_PATTERN_MAX_LENGTH}",
        )

    cost_model, warnings = load_cost_model(data_dir=data_dir)
    entries, fname, mtime, err = _load_histogram_payload(
        metric=metric,
        scale=scale,
        dimension="usage",
        data_dir=data_dir,
    )
    if entries is None:
        warnings.append("{}: {}".format(fname, err or "unavailable"))
        return {
            "metric": metric,
            "scale": scale,
            "pattern": pattern,
            "date": date,
            "group_total": 0.0,
            "matched_count": 0,
            "members": [],
            "metadata": build_metadata(
                cost_model=cost_model,
                warnings=warnings,
                metric=metric,
                scale=scale,
                source_file=fname,
                generated_at_mtime=mtime,
            ),
        }

    dates = _unique_dates_in_order(entries)
    target_date = date if date is not None else (dates[-1] if dates else None)
    if target_date is None:
        warnings.append(f"no date available in {fname}")
        return {
            "metric": metric,
            "scale": scale,
            "pattern": pattern,
            "date": None,
            "group_total": 0.0,
            "matched_count": 0,
            "members": [],
            "metadata": build_metadata(
                cost_model=cost_model,
                warnings=warnings,
                metric=metric,
                scale=scale,
                source_file=fname,
                generated_at_mtime=mtime,
            ),
        }
    if date is not None and date not in dates:
        warnings.append(f"date {date!r} not present in {fname} (available: {dates})")

    members_map: dict[str, float] = {}
    for entry in entries:
        if entry.get("date") != target_date:
            continue
        stack = entry.get("stack")
        if not isinstance(stack, str):
            continue
        if _is_special(stack):
            continue
        if not fnmatch.fnmatch(stack, pattern):
            continue
        value = _coerce_value(entry)
        if value is None:
            continue
        # If multiple entries somehow share (stack, date), sum them.
        members_map[stack] = members_map.get(stack, 0.0) + value

    members = [
        {"namespace": ns, "value": _round(v)}
        for ns, v in sorted(members_map.items(), key=lambda item: item[1], reverse=True)
    ]
    group_total = sum(v for v in members_map.values())

    if not members:
        warnings.append(f"pattern {pattern!r} matched no namespace at date {target_date!r}")

    return {
        "metric": metric,
        "scale": scale,
        "pattern": pattern,
        "date": target_date,
        "group_total": _round(group_total),
        "matched_count": len(members),
        "members": members,
        "metadata": build_metadata(
            cost_model=cost_model,
            warnings=warnings,
            metric=metric,
            scale=scale,
            data_window=_build_data_window(scale, dates, selected_date=target_date),
            source_file=fname,
            generated_at_mtime=mtime,
        ),
    }


# ---------------------------------------------------------------------------
# MCP wiring (spec §7.2 step 8) — protocol layer, transport, entry point
# ---------------------------------------------------------------------------
#
# The tool_* functions above are plain Python: they take native Python args,
# return native Python dicts, and have no MCP dependency. This last section
# is the thin adapter that:
#
# - registers each tool with the MCP SDK via FastMCP (it auto-generates the
#   ``inputSchema`` from type annotations);
# - exposes the tools over the Streamable HTTP transport (spec §3.4 — the
#   single endpoint ``/mcp`` is what the K8s Service publishes; SSE and
#   stdio are intentionally not supported, see ``main()`` docstring);
# - detects whether the SDK supports ``structuredContent`` /
#   ``outputSchema`` and logs which mechanism is in effect (spec §4.7).
#
# MCP SDK and its transitive deps (starlette, uvicorn, anyio) are listed in
# pyproject.toml. They are only required when the MCP container starts;
# importing them lazily here means the rest of the module remains usable in
# isolation (e.g. when unit-testing the tool_* functions on a machine
# without the SDK installed).
#
# Note on response shape — FastMCP convention. When a tool returns a generic
# ``Dict[str, Any]`` (rather than a Pydantic model), the SDK wraps the value
# under a ``"result"`` key in ``structuredContent``. So the JSON shape the
# client receives is:
#
#     {
#       "content": [{"type": "text", "text": "<json-encoded tool dict>"}],
#       "structuredContent": {"result": { ...tool dict... }}
#     }
#
# The unwrapped spec-shape (§4.1–§4.5) is therefore reachable two ways:
# parsing ``content[0].text`` (text fallback, always present) or navigating
# ``structuredContent.result.*``. Both carry identical data — and both
# include the ``metadata`` block. This is a deliberate FastMCP safety
# convention, not a spec violation; clients are expected to handle it.


_MCP_TOOL_REGISTRY: list[tuple[str, Any]] = [
    # (mcp_tool_name, underlying tool_* function). Used for diagnostics and
    # for the lazy-built FastMCP app below.
]


def _build_mcp_app() -> Any:
    """Build and configure the FastMCP application — done lazily.

    Kept lazy so importing ``mcp_server`` does NOT require the ``mcp`` SDK
    to be installed; only :func:`main` triggers it. This is also what
    keeps the unit tests (which never call main()) light.
    """
    from mcp.server.fastmcp import FastMCP

    host = _get_env("MCP_LISTEN_HOST", "0.0.0.0") or "0.0.0.0"
    try:
        port = int(_get_env("MCP_LISTEN_PORT", "5484") or "5484")
    except (TypeError, ValueError):
        port = 5484

    app = FastMCP(
        name="kubeledger-mcp",
        instructions=(
            "Read-only descriptive surface over KubeLedger analytics. "
            "Exposes namespace usage, top consumers, breakdown with cluster "
            "overhead, efficiency (aggregated and hourly), trends, and "
            "regrouping by glob pattern. Every response carries a metadata "
            "block with cost_model, unit, data window and warnings — call "
            "describe_dataset first to learn what is available."
        ),
        host=host,
        port=port,
    )

    # --- Tools registration ---------------------------------------------
    #
    # Each registration is a thin wrapper around the corresponding pure
    # Python ``tool_*`` function. The annotations on the wrapper determine
    # the JSON Schema that FastMCP advertises to clients, so:
    #   - Use ``Literal[...]`` for the values that the spec constrains
    #     (metric, scale) — clients get a strict enum at the protocol level.
    #   - Use ``Optional[str]`` for free-form optional strings (namespace,
    #     date, pattern when allowed null) so they appear as nullable.
    #   - Return ``Dict[str, Any]`` — FastMCP serialises and (when the SDK
    #     supports it) puts the result under ``structuredContent``.

    @app.tool()
    def list_namespaces() -> dict[str, Any]:
        """List available namespaces (application / system) and the known special entries."""
        return tool_list_namespaces()

    @app.tool()
    def describe_dataset() -> dict[str, Any]:
        """Announce dataset capabilities: metrics, GPU availability, scales, efficiency, cost_model."""
        return tool_describe_dataset()

    @app.tool()
    def get_usage(
        metric: Literal["cpu", "memory"],
        scale: Literal["daily_14d", "monthly_12m"],
        namespace: str | None = None,
        exclude_special: bool = True,
    ) -> dict[str, Any]:
        """Usage per namespace for a given metric and scale (time series of points)."""
        return tool_get_usage(
            metric=metric,
            scale=scale,
            namespace=namespace,
            exclude_special=exclude_special,
        )

    @app.tool()
    def get_top_consumers(
        metric: Literal["cpu", "memory"],
        scale: Literal["daily_14d", "monthly_12m"],
        limit: int = 10,
        exclude_system: bool = True,
        date: str | None = None,
    ) -> dict[str, Any]:
        """Rank namespaces by usage for a date (defaults to the most recent in the data)."""
        return tool_get_top_consumers(
            metric=metric,
            scale=scale,
            limit=limit,
            exclude_system=exclude_system,
            date=date,
        )

    @app.tool()
    def get_namespace_breakdown(
        metric: Literal["cpu", "memory"],
        scale: Literal["daily_14d", "monthly_12m"],
        date: str | None = None,
        exclude_system: bool = True,
    ) -> dict[str, Any]:
        """Proportional breakdown + concentration + cluster_overhead (non-allocatable separated)."""
        return tool_get_namespace_breakdown(
            metric=metric,
            scale=scale,
            date=date,
            exclude_system=exclude_system,
        )

    @app.tool()
    def get_efficiency(
        scale: Literal["daily_14d", "monthly_12m"],
        namespace: str | None = None,
        metric: Literal["cpu", "memory"] | None = None,
    ) -> dict[str, Any]:
        """Return usage/requests ratio per (namespace, metric) — descriptive classification."""
        return tool_get_efficiency(scale=scale, namespace=namespace, metric=metric)

    @app.tool()
    def get_timeseries(
        metric: Literal["cpu", "memory"],
        namespace: str,
    ) -> dict[str, Any]:
        """Hourly usage series over the trends window for a single namespace, with min/max/mean."""
        return tool_get_timeseries(metric=metric, namespace=namespace)

    @app.tool()
    def compare_periods(
        metric: Literal["cpu", "memory"],
        namespace: str,
    ) -> dict[str, Any]:
        """Compare 14-day aggregate against monthly trajectory; descriptive trend direction."""
        return tool_compare_periods(metric=metric, namespace=namespace)

    @app.tool()
    def get_efficiency_timeseries(
        metric: Literal["cpu", "memory"],
        namespace: str,
    ) -> dict[str, Any]:
        """Hourly efficiency factor (rf = usage/requests) series — not bounded at 1."""
        return tool_get_efficiency_timeseries(metric=metric, namespace=namespace)

    @app.tool()
    def group_namespaces(
        metric: Literal["cpu", "memory"],
        scale: Literal["daily_14d", "monthly_12m"],
        pattern: str,
        date: str | None = None,
    ) -> dict[str, Any]:
        """Aggregate namespaces matching a glob pattern (fnmatch) — sum + per-member detail."""
        return tool_group_namespaces(
            metric=metric,
            scale=scale,
            pattern=pattern,
            date=date,
        )

    # Register the metadata for diagnostics. Names match the wrapper
    # functions defined above so the registry stays in sync with the SDK.
    _MCP_TOOL_REGISTRY.clear()
    for name in (
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
    ):
        _MCP_TOOL_REGISTRY.append((name, globals().get("tool_" + name)))

    return app


def detect_structured_content_support() -> tuple[bool, str]:
    """Detect whether the installed MCP SDK supports ``structuredContent``.

    Spec §4.7 mandates that the wiring step verifies SDK capabilities and
    signals the retained mode to the developer. We probe two markers:

    1. the presence of a ``structuredContent`` field on the SDK's
       ``CallToolResult`` model (added with protocol revision 2025-03),
    2. the presence of ``outputSchema`` on the ``Tool`` model.

    FastMCP automatically routes return values through ``structuredContent``
    when the underlying SDK exposes it, falling back to a serialized text
    block otherwise — so detection here is informational. Returns
    ``(supported, human_readable_reason)``.
    """
    try:
        import mcp as _mcp_pkg
        from mcp.types import CallToolResult, Tool  # type: ignore[attr-defined]
    except ImportError as exc:
        return False, f"mcp SDK not importable: {exc}"

    sdk_version = getattr(_mcp_pkg, "__version__", "unknown")

    def _has_field(model: Any, name: str) -> bool:
        fields = getattr(model, "model_fields", None) or getattr(model, "__fields__", None) or {}
        return name in fields

    has_structured = _has_field(CallToolResult, "structuredContent")
    has_output_schema = _has_field(Tool, "outputSchema")

    if has_structured and has_output_schema:
        return True, f"mcp SDK {sdk_version}: structuredContent + outputSchema both present"
    if has_structured:
        return True, f"mcp SDK {sdk_version}: structuredContent present, outputSchema missing"
    return False, f"mcp SDK {sdk_version}: structuredContent not supported — falling back to text content"


def main() -> None:
    """Run the KubeLedger MCP server over Streamable HTTP.

    Single transport by design (spec §3.3 / §3.4): KubeLedger's MCP server
    is meant to live inside a Kubernetes pod alongside backend.py, behind
    a Service. Streamable HTTP is the modern transport that all current
    MCP clients (Claude Desktop, Claude Code, MCP Inspector) speak.

    SSE was the historical transport — now deprecated and removed here to
    avoid a second code path that no current client needs. stdio was only
    relevant for the ``.mcpb`` bundle pattern (data shipped locally), which
    does not fit KubeLedger's "data generated in-cluster" model. Both have
    been dropped in v1 to keep the surface minimal and the deployment
    story coherent.
    """
    import logging

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logger = logging.getLogger("kubeledger-mcp")

    data_dir = get_data_dir()
    host = _get_env("MCP_LISTEN_HOST", "0.0.0.0") or "0.0.0.0"
    try:
        port = int(_get_env("MCP_LISTEN_PORT", "5484") or "5484")
    except ValueError:
        port = 5484

    logger.info(
        "kubeledger-mcp starting on %s:%d (data_dir=%s, exists=%s)",
        host,
        port,
        data_dir,
        data_dir.is_dir(),
    )
    if host in ("0.0.0.0", "::", "*"):
        # MCP v1 has no authentication at the tool layer (spec §3.5).
        # Wide-open binding is fine when the pod is protected by the chart's
        # default-deny NetworkPolicy, but risky on a bare host — surface it
        # so operators don't deploy outside that envelope by accident.
        logger.warning(
            "binding on %s — server is reachable from any reachable interface. "
            "MCP v1 has no auth; rely on NetworkPolicy / firewall.",
            host,
        )

    supported, reason = detect_structured_content_support()
    if supported:
        logger.info("structuredContent: ENABLED — %s", reason)
    else:
        logger.info("structuredContent: DISABLED — %s", reason)

    app = _build_mcp_app()
    logger.info("registered %d MCP tools: %s", len(_MCP_TOOL_REGISTRY), ", ".join(n for n, _ in _MCP_TOOL_REGISTRY))
    logger.info("transport: streamable-http (endpoint: /mcp)")

    # Hand off to FastMCP's native ASGI transport. It internally builds a
    # Starlette app and runs it with uvicorn — no custom ASGI plumbing
    # needed (spec §3.3 / §7.3).
    app.run(transport="streamable-http")


if __name__ == "__main__":
    main()

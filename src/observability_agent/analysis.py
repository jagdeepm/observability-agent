"""Deterministic analysis of telemetry payloads — arithmetic in code, prose in the LLM.

Each function turns a raw source response into facts an SRE would compute by hand:
distribution statistics and anomaly counts for metrics, deduplicated groups with exact
counts for logs, a span tree with the slowest span surfaced for traces.

The rendered text replaces the truncated payload in the summarization prompt; the
signals mapping travels on the Evidence to the Coordinator's prompts.

Nothing here raises: an unrecognized payload degrades to the previous truncated-string
behaviour with no signals, so a source's API change costs evidence quality, not the
investigation.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from math import isfinite
from statistics import mean, median, quantiles, stdev
from typing import Any

from pydantic import BaseModel, Field

from observability_agent.domain.models import Signal

logger = logging.getLogger(__name__)

# ponytail: calibration knobs. Real telemetry is noisy in environment-specific ways —
# tune these per deployment rather than editing the detection logic.
Z_SCORE_THRESHOLD = 3.0
PCT_CHANGE_THRESHOLD = 50.0
# Values below this fraction of a series' maximum are too small for a percentage
# change between them to mean anything.
NEGLIGIBLE_FRACTION = 0.01

# Rendering caps — the point of this module is bounded prompts, so it must not
# reintroduce unbounded output.
MAX_SERIES = 10
MAX_LOG_GROUPS = 20
MAX_SPANS = 50
FALLBACK_TRUNCATE = 4000

_ERROR_LEVELS = {"error", "err", "critical", "crit", "fatal", "emerg", "alert"}
_ERROR_PATTERN = re.compile(
    r"\b(error|fatal|critical|panic|exception|traceback|oomkilled|segfault)\b",
    re.IGNORECASE,
)
# Normalize volatile parts of a log line so repeats collapse into one group.
_HEX_OR_UUID = re.compile(r"\b[0-9a-f]{8,}\b", re.IGNORECASE)
_NUMBER = re.compile(r"\d+")
_WHITESPACE = re.compile(r"\s+")


class Analysis(BaseModel):
    """Rendered facts for the prompt, plus the same facts as structured signals."""

    text: str
    signals: dict[str, Signal] = Field(default_factory=dict)


def _fallback(raw: Any) -> Analysis:
    """Previous behaviour: a truncated string dump, no signals."""
    text = str(raw)
    if len(text) > FALLBACK_TRUNCATE:
        text = text[:FALLBACK_TRUNCATE] + "...(truncated)"
    return Analysis(text=text)


def _fmt(value: float) -> str:
    return f"{value:g}"


# ---------------------------------------------------------------------------
# Metrics — Prometheus
# ---------------------------------------------------------------------------


def _extract_series(raw: dict[str, Any]) -> list[tuple[str, list[float]]]:
    """Pull (label_text, values) out of a Prometheus vector or matrix response."""
    result = raw.get("data", {}).get("result", [])
    series: list[tuple[str, list[float]]] = []

    for item in result:
        labels = item.get("metric", {})
        label_text = "{" + ", ".join(f'{k}="{v}"' for k, v in sorted(labels.items())) + "}"

        points = item.get("values") or ([item["value"]] if "value" in item else [])
        values: list[float] = []
        for point in points:
            try:
                value = float(point[1])
            except (TypeError, ValueError, IndexError):
                continue
            # Prometheus emits "NaN" for gaps; NaN propagates through mean/stdev
            # and would poison every downstream signal.
            if isfinite(value):
                values.append(value)

        if values:
            series.append((label_text, values))

    return series


def _series_stats(values: list[float]) -> dict[str, float]:
    stats: dict[str, float] = {
        "mean": mean(values),
        "median": median(values),
        "min": min(values),
        "max": max(values),
    }
    if len(values) >= 2:
        deviation = stdev(values)
        stats["stdev"] = deviation
        # "inclusive" keeps percentiles inside the observed range. The default
        # "exclusive" method extrapolates past the maximum on short series —
        # a p95 above any value actually recorded is not a fact.
        stats["p95"] = quantiles(values, n=100, method="inclusive")[94]
        if stats["mean"] != 0:
            stats["cv"] = deviation / stats["mean"]
    return stats


def _find_spikes(values: list[float], stats: dict[str, float]) -> tuple[set[int], float]:
    """Indices flagged by z-score or rate-of-change, and the maximum |z| seen.

    ponytail: the z-rule cannot fire on short series — with a sample stdev the
    largest possible |z| is (n-1)/sqrt(n), which stays under 3.0 until n > 10.
    The rate-of-change rule is what catches spikes in short windows. Lower
    Z_SCORE_THRESHOLD if you routinely query windows of ten points or fewer.
    """
    flagged: set[int] = set()
    max_z = 0.0

    deviation = stats.get("stdev", 0.0)
    if deviation > 0:
        for i, value in enumerate(values):
            z = abs(value - stats["mean"]) / deviation
            max_z = max(max_z, z)
            if z > Z_SCORE_THRESHOLD:
                flagged.add(i)

    # A percentage jump between two values that are both negligible against the
    # series as a whole is noise, not a spike.
    floor = abs(stats["max"]) * NEGLIGIBLE_FRACTION
    for i in range(1, len(values)):
        previous = values[i - 1]
        if previous == 0 or (abs(previous) < floor and abs(values[i]) < floor):
            continue
        if abs(values[i] - previous) / abs(previous) * 100 > PCT_CHANGE_THRESHOLD:
            flagged.add(i)

    return flagged, max_z


def analyze_metrics(raw: Any) -> Analysis:
    """Distribution statistics and spike detection over the full series."""
    try:
        if not isinstance(raw, dict):
            return _fallback(raw)

        series = _extract_series(raw)
        if not series:
            return Analysis(text="No metric data returned for this query.")

        lines = [f"Metric analysis ({len(series)} series):"]
        # Signals describe the most interesting series: an anomalous one always
        # beats a quiet one, however large the quiet one's values are. Reporting
        # the loudest series would let a flat high-value series mask a spiking
        # neighbour, and the anomaly flag is what the Coordinator branches on.
        best: dict[str, Signal] = {}
        best_rank = (False, float("-inf"))
        total_spikes = 0

        for label_text, values in series[:MAX_SERIES]:
            stats = _series_stats(values)
            flagged, max_z = _find_spikes(values, stats)
            total_spikes += len(flagged)

            rendered = " ".join(f"{k}={_fmt(v)}" for k, v in stats.items())
            lines.append(f"  {label_text} ({len(values)} points): {rendered}")
            if flagged:
                lines.append(
                    f"    {len(flagged)} spike(s), max |z|={_fmt(max_z)} "
                    f"(threshold {_fmt(Z_SCORE_THRESHOLD)})"
                )
            else:
                lines.append(f"    no spikes (max |z|={_fmt(max_z)})")

            rank = (bool(flagged), stats["max"])
            if rank > best_rank:
                best_rank = rank
                best = {
                    "anomaly": bool(flagged),
                    "spike_count": len(flagged),
                    "max_z": round(max_z, 3),
                    **{k: round(v, 6) for k, v in stats.items()},
                }

        if len(series) > MAX_SERIES:
            lines.append(f"  ...and {len(series) - MAX_SERIES} more series")

        best["series_count"] = len(series)
        best["total_spike_count"] = total_spikes
        return Analysis(text="\n".join(lines), signals=best)

    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("analyze_metrics fell back: %s", exc)
        return _fallback(raw)


# ---------------------------------------------------------------------------
# Logs — Loki
# ---------------------------------------------------------------------------


def _normalize(line: str) -> str:
    """Collapse volatile parts so repeated lines group together."""
    normalized = _HEX_OR_UUID.sub("#", line)
    normalized = _NUMBER.sub("#", normalized)
    return _WHITESPACE.sub(" ", normalized).strip().lower()


def _is_error(line: str, level: str) -> bool:
    return level in _ERROR_LEVELS or bool(_ERROR_PATTERN.search(line))


def _iso(nanoseconds: str) -> str:
    try:
        return datetime.fromtimestamp(int(nanoseconds) / 1e9, UTC).isoformat(timespec="seconds")
    except (TypeError, ValueError):
        return str(nanoseconds)


def analyze_logs(raw: Any) -> Analysis:
    """Exact counts, with repeated lines collapsed into counted groups."""
    try:
        if not isinstance(raw, dict):
            return _fallback(raw)

        data = raw.get("data", {})
        # A metric LogQL query (rate(), count_over_time()) returns a matrix of
        # samples, not log lines. Counting those as lines would report confident
        # nonsense, which is worse than falling back.
        if data.get("resultType") not in ("streams", None):
            return _fallback(raw)

        streams = data.get("result", [])
        if not isinstance(streams, list):
            return _fallback(raw)

        groups: dict[str, dict[str, Any]] = {}
        total = 0
        errors = 0

        for stream in streams:
            labels = stream.get("stream", {})
            level = str(
                labels.get("level") or labels.get("detected_level") or labels.get("severity") or ""
            ).lower()

            for entry in stream.get("values", []):
                try:
                    timestamp, line = entry[0], entry[1]
                except (TypeError, IndexError):
                    continue

                total += 1
                is_error = _is_error(line, level)
                errors += is_error

                key = _normalize(line)
                group = groups.setdefault(
                    key,
                    {"line": line, "count": 0, "first": timestamp, "last": timestamp,
                     "error": is_error, "service": labels.get("service", "")},
                )
                group["count"] += 1
                group["first"] = min(group["first"], timestamp)
                group["last"] = max(group["last"], timestamp)

        if total == 0:
            return Analysis(text="No log lines returned for this query.")

        ranked = sorted(groups.values(), key=lambda g: g["count"], reverse=True)
        lines = [
            f"Log analysis: {total} lines, {errors} error-level, "
            f"{len(groups)} distinct messages."
        ]
        for group in ranked[:MAX_LOG_GROUPS]:
            marker = "ERROR" if group["error"] else "info"
            lines.append(
                f"  [{marker}] ×{group['count']} "
                f"({_iso(group['first'])} → {_iso(group['last'])}): {group['line']}"
            )
        if len(ranked) > MAX_LOG_GROUPS:
            lines.append(f"  ...and {len(ranked) - MAX_LOG_GROUPS} more distinct messages")

        return Analysis(
            text="\n".join(lines),
            signals={
                "error_count": errors,
                "total_lines": total,
                "distinct_messages": len(groups),
            },
        )

    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("analyze_logs fell back: %s", exc)
        return _fallback(raw)


# ---------------------------------------------------------------------------
# Traces — Tempo
# ---------------------------------------------------------------------------


def _attr(span: dict[str, Any], key: str, default: str = "") -> str:
    """Read a span attribute from either shape Tempo returns.

    Search results carry OTLP-style attribute lists —
    [{"key": "service.name", "value": {"stringValue": "checkout"}}] — while
    some endpoints and mocks use a plain mapping. Assuming the mapping made
    every real trace fall back to a raw dump.
    """
    attributes = span.get("attributes")

    if isinstance(attributes, dict):
        value = attributes.get(key, default)
        return str(value) if value != "" else default

    if isinstance(attributes, list):
        for item in attributes:
            if not isinstance(item, dict) or item.get("key") != key:
                continue
            value = item.get("value")
            if isinstance(value, dict):
                for field in ("stringValue", "intValue", "doubleValue", "boolValue"):
                    if field in value:
                        return str(value[field])
                return default
            return str(value) if value is not None else default

    return default


def _span_id(span: dict[str, Any]) -> str | None:
    """Tempo spells span IDs differently across endpoints."""
    return span.get("spanID") or span.get("spanId")


def _parent_id(span: dict[str, Any]) -> str | None:
    return span.get("parentSpanID") or span.get("parentSpanId")


def _span_name(span: dict[str, Any]) -> str:
    return str(span.get("name") or _attr(span, "name") or "(unnamed)")


def _span_duration_ms(span: dict[str, Any]) -> float:
    for key, divisor in (("durationNanos", 1e6), ("durationNs", 1e6), ("durationMs", 1.0)):
        if key in span:
            try:
                return float(span[key]) / divisor
            except (TypeError, ValueError):
                continue
    return 0.0


def _collect_spans(trace: dict[str, Any]) -> list[dict[str, Any]]:
    span_sets = trace.get("spanSets") or ([trace["spanSet"]] if "spanSet" in trace else [])
    spans: list[dict[str, Any]] = []
    for span_set in span_sets:
        spans.extend(span_set.get("spans", []))
    return spans


def _children_map(
    spans: list[dict[str, Any]],
) -> tuple[dict[str | None, list[dict[str, Any]]], bool]:
    by_id = {_span_id(s): s for s in spans if _span_id(s)}
    has_parents = any(_parent_id(s) in by_id for s in spans)

    children: dict[str | None, list[dict[str, Any]]] = {}
    for span in spans:
        parent = _parent_id(span)
        children.setdefault(parent if parent in by_id else None, []).append(span)
    return children, has_parents


def _self_time_ms(span: dict[str, Any], children: dict[str | None, list[dict[str, Any]]]) -> float:
    """Time spent in this span itself, excluding its children.

    A parent's total duration contains its children's, so ranking by total always
    returns the root span and says nothing. Self time is what points at the
    bottleneck.
    """
    child_total = sum(_span_duration_ms(c) for c in children.get(_span_id(span), []))
    return max(_span_duration_ms(span) - child_total, 0.0)


def _render_tree(
    spans: list[dict[str, Any]],
    root_service: str,
    children: dict[str | None, list[dict[str, Any]]],
    has_parents: bool,
) -> list[str]:
    """Indent by parent/child depth when links exist, else rank by duration."""

    def describe(span: dict[str, Any], depth: int = 0) -> str:
        service = _attr(span, "service.name", root_service)
        total = _span_duration_ms(span)
        name = _span_name(span)
        if not has_parents:
            return f"{'  ' * depth}{name} — {_fmt(total)}ms ({service})"
        self_ms = _self_time_ms(span, children)
        return (
            f"{'  ' * depth}{name} — {_fmt(self_ms)}ms self / "
            f"{_fmt(total)}ms total ({service})"
        )

    if not has_parents:
        ranked = sorted(spans, key=_span_duration_ms, reverse=True)
        return [describe(s) for s in ranked[:MAX_SPANS]]

    lines: list[str] = []

    def walk(span: dict[str, Any], depth: int) -> None:
        if len(lines) >= MAX_SPANS:
            return
        lines.append(describe(span, depth))
        for child in sorted(
            children.get(_span_id(span), []), key=_span_duration_ms, reverse=True
        ):
            walk(child, depth + 1)

    for root in children.get(None, []):
        walk(root, 0)
    return lines


def analyze_traces(raw: Any) -> Analysis:
    """Span tree with the slowest span found by comparing durations."""
    try:
        if not isinstance(raw, dict):
            return _fallback(raw)

        traces = raw.get("traces")
        if not isinstance(traces, list):
            return _fallback(raw)
        if not traces:
            return Analysis(text="No traces returned for this query.")

        lines: list[str] = []
        span_count = 0
        slowest: dict[str, Any] | None = None
        slowest_service = ""
        slowest_ms = 0.0

        for trace in traces:
            root_service = trace.get("rootServiceName", "unknown")
            spans = _collect_spans(trace)
            span_count += len(spans)
            children, has_parents = _children_map(spans)

            lines.append(
                f"Trace {trace.get('traceID', '?')} — root service {root_service}, "
                f"{len(spans)} spans:"
            )
            lines.extend(
                f"  {line}" for line in _render_tree(spans, root_service, children, has_parents)
            )

            for span in spans:
                self_ms = _self_time_ms(span, children)
                if slowest is None or self_ms > slowest_ms:
                    slowest = span
                    slowest_ms = self_ms
                    slowest_service = _attr(span, "service.name", root_service)

        if slowest is None:
            return Analysis(text="No spans returned for this query.")

        header = (
            f"Trace analysis: {len(traces)} traces, {span_count} spans. "
            f"Slowest span by self time: '{_span_name(slowest)}' "
            f"in {slowest_service} at {_fmt(slowest_ms)}ms."
        )

        return Analysis(
            text="\n".join([header, *lines]),
            signals={
                "slowest_span": _span_name(slowest),
                "slowest_service": slowest_service,
                "slowest_self_ms": round(slowest_ms, 3),
                "span_count": span_count,
                "trace_count": len(traces),
            },
        )

    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("analyze_traces fell back: %s", exc)
        return _fallback(raw)

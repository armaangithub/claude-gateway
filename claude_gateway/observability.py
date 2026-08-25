"""Observability: a always-on lightweight metrics registry plus optional
OpenTelemetry tracing/metrics.

The in-process registry guarantees the dashboard and ``/v1/metrics`` always
have numbers even when no OTEL collector is configured. OpenTelemetry is wired
when ``OTEL_ENABLED=1`` and degrades to a no-op if the packages are missing.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from .logging_config import get_logger

log = get_logger("gateway.obs")


@dataclass
class _Histogram:
    count: int = 0
    total: float = 0.0
    min: float = float("inf")
    max: float = 0.0
    buckets: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    def observe(self, value: float) -> None:
        self.count += 1
        self.total += value
        self.min = min(self.min, value)
        self.max = max(self.max, value)
        # Coarse latency buckets in ms.
        for edge in (50, 200, 500, 1000, 5000, 15000, 60000):
            if value <= edge:
                self.buckets[f"<= {edge}ms"] += 1
                break
        else:
            self.buckets["> 60000ms"] += 1

    @property
    def avg(self) -> float:
        return self.total / self.count if self.count else 0.0


class Metrics:
    """Thread-safe in-process metric registry."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, float] = defaultdict(float)
        self._gauges: dict[str, float] = defaultdict(float)
        self._hist: dict[str, _Histogram] = defaultdict(_Histogram)
        self.start_time = time.time()

    def inc(self, name: str, value: float = 1.0, **labels: str) -> None:
        key = _key(name, labels)
        with self._lock:
            self._counters[key] += value
        _otel_add(name, value, labels)

    def gauge(self, name: str, value: float, **labels: str) -> None:
        with self._lock:
            self._gauges[_key(name, labels)] = value

    def observe(self, name: str, value: float, **labels: str) -> None:
        key = _key(name, labels)
        with self._lock:
            self._hist[key].observe(value)
        _otel_record(name, value, labels)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "uptime_s": round(time.time() - self.start_time, 1),
                "counters": dict(self._counters),
                "gauges": dict(self._gauges),
                "histograms": {
                    k: {
                        "count": h.count,
                        "avg_ms": round(h.avg, 1),
                        "min_ms": round(h.min, 1) if h.count else 0,
                        "max_ms": round(h.max, 1),
                        "buckets": dict(h.buckets),
                    }
                    for k, h in self._hist.items()
                },
            }

    def prometheus(self) -> str:
        """Render a minimal Prometheus exposition format."""
        lines: list[str] = []
        snap = self.snapshot()
        for k, v in snap["counters"].items():
            lines.append(f"{_prom_name(k)} {v}")
        for k, v in snap["gauges"].items():
            lines.append(f"{_prom_name(k)} {v}")
        for k, h in snap["histograms"].items():
            base = _prom_name(k)
            lines.append(f"{base}_count {h['count']}")
            lines.append(f"{base}_avg_ms {h['avg_ms']}")
            lines.append(f"{base}_max_ms {h['max_ms']}")
        return "\n".join(lines) + "\n"


def _key(name: str, labels: dict[str, str]) -> str:
    if not labels:
        return name
    label_str = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
    return f"{name}{{{label_str}}}"


def _prom_name(key: str) -> str:
    return "gateway_" + key.replace(".", "_").replace("-", "_")


# ---------------------------------------------------------------------------
# Optional OpenTelemetry
# ---------------------------------------------------------------------------
_otel_tracer = None
_otel_counters: dict[str, Any] = {}
_otel_histograms: dict[str, Any] = {}
_otel_meter = None


def init_otel(enabled: bool, endpoint: str | None, service_name: str) -> None:
    global _otel_tracer, _otel_meter
    if not enabled:
        return
    try:
        from opentelemetry import metrics, trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

        resource = Resource.create({"service.name": service_name})
        tp = TracerProvider(resource=resource)
        mp_readers = []
        if endpoint:
            try:
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                    OTLPSpanExporter,
                )
                from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
                    OTLPMetricExporter,
                )

                tp.add_span_processor(
                    BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces"))
                )
                mp_readers.append(
                    PeriodicExportingMetricReader(
                        OTLPMetricExporter(endpoint=f"{endpoint}/v1/metrics")
                    )
                )
            except Exception as e:  # pragma: no cover - exporter optional
                log.warning("OTLP exporter unavailable: %s", e)
        trace.set_tracer_provider(tp)
        metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=mp_readers))
        _otel_tracer = trace.get_tracer(service_name)
        _otel_meter = metrics.get_meter(service_name)
        log.info("OpenTelemetry initialized (endpoint=%s)", endpoint or "console-only")
    except Exception as e:  # pragma: no cover
        log.warning("OpenTelemetry init failed, continuing without it: %s", e)


def _otel_add(name: str, value: float, labels: dict[str, str]) -> None:
    if _otel_meter is None:
        return
    try:
        c = _otel_counters.get(name)
        if c is None:
            c = _otel_meter.create_counter(_prom_name(name))
            _otel_counters[name] = c
        c.add(value, attributes=labels or None)
    except Exception:  # pragma: no cover
        pass


def _otel_record(name: str, value: float, labels: dict[str, str]) -> None:
    if _otel_meter is None:
        return
    try:
        h = _otel_histograms.get(name)
        if h is None:
            h = _otel_meter.create_histogram(_prom_name(name))
            _otel_histograms[name] = h
        h.record(value, attributes=labels or None)
    except Exception:  # pragma: no cover
        pass


def span(name: str):
    """Return a tracing span context manager (no-op if OTEL disabled)."""
    if _otel_tracer is None:
        from contextlib import nullcontext

        return nullcontext()
    return _otel_tracer.start_as_current_span(name)


# Process-wide registry.
metrics_registry = Metrics()

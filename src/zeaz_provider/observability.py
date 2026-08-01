from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest


class Observability:
    def __init__(self, *, otlp_enabled: bool = False):
        self.registry = CollectorRegistry()
        self.requests = Counter(
            "zeaz_http_requests_total",
            "Completed gateway HTTP requests.",
            ("method", "path", "status"),
            registry=self.registry,
        )
        self.duration = Histogram(
            "zeaz_http_request_duration_seconds",
            "Gateway HTTP request duration.",
            ("method", "path"),
            registry=self.registry,
        )
        self.in_flight = Gauge(
            "zeaz_http_requests_in_flight",
            "Gateway HTTP requests currently in flight.",
            registry=self.registry,
        )
        self._meter_provider = None
        self._otel_requests = None
        self._otel_duration = None
        if otlp_enabled:
            self._configure_otlp()

    def _configure_otlp(self) -> None:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

        reader = PeriodicExportingMetricReader(OTLPMetricExporter())
        self._meter_provider = MeterProvider(metric_readers=[reader])
        meter = self._meter_provider.get_meter("zeaz_provider")
        self._otel_requests = meter.create_counter(
            "zeaz.http.requests",
            description="Completed gateway HTTP requests.",
        )
        self._otel_duration = meter.create_histogram(
            "zeaz.http.request.duration",
            unit="s",
            description="Gateway HTTP request duration.",
        )

    def start_request(self) -> None:
        self.in_flight.inc()

    def finish_request(
        self,
        *,
        method: str,
        path: str,
        status_code: int,
        duration_seconds: float,
    ) -> None:
        duration = max(0.0, duration_seconds)
        status = str(status_code)
        self.in_flight.dec()
        self.requests.labels(method=method, path=path, status=status).inc()
        self.duration.labels(method=method, path=path).observe(duration)
        attributes = {
            "http.request.method": method,
            "http.route": path,
            "http.response.status_code": status_code,
        }
        if self._otel_requests is not None:
            self._otel_requests.add(1, attributes)
        if self._otel_duration is not None:
            self._otel_duration.record(duration, attributes)

    def prometheus(self) -> bytes:
        return generate_latest(self.registry)

    def shutdown(self) -> None:
        if self._meter_provider is not None:
            self._meter_provider.shutdown()

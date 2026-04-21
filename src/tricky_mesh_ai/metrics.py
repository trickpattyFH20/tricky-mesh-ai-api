import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


log = logging.getLogger(__name__)


_COUNTER_NAMES = (
    "messages_received_total",
    "messages_dropped_authz_total",
    "messages_dropped_unknown_sender_total",
    "messages_dropped_inbound_too_large_total",
    "messages_dropped_ratelimit_total",
    "messages_replied_total",
    "reply_acks_success_total",
    "reply_acks_failed_total",
    "llm_errors_total",
    "send_errors_total",
    "meshcore_reconnects_total",
)


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, int] = {n: 0 for n in _COUNTER_NAMES}
        self._llm_latency_sum = 0.0
        self._llm_latency_count = 0
        self._started_at = time.time()

    def inc(self, name: str, n: int = 1) -> None:
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + n

    def observe_llm_latency(self, seconds: float) -> None:
        with self._lock:
            self._llm_latency_sum += seconds
            self._llm_latency_count += 1

    def prometheus(self) -> str:
        with self._lock:
            counters = dict(self._counters)
            lat_sum = self._llm_latency_sum
            lat_count = self._llm_latency_count
            uptime = time.time() - self._started_at

        lines: list[str] = []
        for k, v in counters.items():
            lines.append(f"# TYPE tricky_mesh_ai_{k} counter")
            lines.append(f"tricky_mesh_ai_{k} {v}")
        lines.append("# TYPE tricky_mesh_ai_llm_latency_seconds_sum counter")
        lines.append(f"tricky_mesh_ai_llm_latency_seconds_sum {lat_sum}")
        lines.append("# TYPE tricky_mesh_ai_llm_latency_seconds_count counter")
        lines.append(f"tricky_mesh_ai_llm_latency_seconds_count {lat_count}")
        lines.append("# TYPE tricky_mesh_ai_uptime_seconds gauge")
        lines.append(f"tricky_mesh_ai_uptime_seconds {uptime}")
        return "\n".join(lines) + "\n"


def start_metrics_server(metrics: Metrics, host: str, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):  # silence default access log
            pass

        def do_GET(self):  # noqa: N802 (http.server interface)
            if self.path not in ("/metrics", "/"):
                self.send_response(404)
                self.end_headers()
                return
            body = metrics.prometheus().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer((host, port), Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True, name="metrics-http")
    t.start()
    log.info("metrics server listening on http://%s:%d/metrics", host, port)
    return server

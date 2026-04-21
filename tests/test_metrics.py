import urllib.request

from tricky_mesh_ai.metrics import Metrics, start_metrics_server


def test_counters_and_latency():
    m = Metrics()
    m.inc("messages_received_total", 3)
    m.inc("llm_errors_total")
    m.observe_llm_latency(0.5)
    m.observe_llm_latency(1.5)
    out = m.prometheus()
    assert "tricky_mesh_ai_messages_received_total 3" in out
    assert "tricky_mesh_ai_llm_errors_total 1" in out
    assert "tricky_mesh_ai_llm_latency_seconds_sum 2.0" in out
    assert "tricky_mesh_ai_llm_latency_seconds_count 2" in out
    assert "tricky_mesh_ai_uptime_seconds" in out


def test_http_endpoint_serves_metrics():
    m = Metrics()
    m.inc("messages_replied_total", 7)
    srv = start_metrics_server(m, "127.0.0.1", 0)
    try:
        host, port = srv.server_address[:2]
        with urllib.request.urlopen(f"http://{host}:{port}/metrics", timeout=2) as r:
            body = r.read().decode()
            assert r.status == 200
            assert "tricky_mesh_ai_messages_replied_total 7" in body
        with urllib.request.urlopen(f"http://{host}:{port}/", timeout=2) as r:
            assert r.status == 200
    finally:
        srv.shutdown()
        srv.server_close()

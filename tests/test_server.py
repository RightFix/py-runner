"""HTTP server: sessions, execution, errors, caps, CORS."""

import json
import threading
import urllib.error
import urllib.request

import pytest

from py_runner.server import Handler, SessionRegistry, serve


@pytest.fixture()
def base_url():
    Handler.registry = SessionRegistry(max_sessions=100)
    httpd = serve("127.0.0.1", 0, 100)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{port}"
    httpd.shutdown()
    httpd.server_close()


def call(method, url, data=None, raw=None):
    body = raw if raw is not None else (json.dumps(data).encode() if data is not None else None)
    req = urllib.request.Request(url, method=method, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, dict(r.headers), json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), json.loads(e.read() or b"null")


def test_health_reports_version(base_url):
    status, _, body = call("GET", base_url + "/health")
    assert status == 200
    assert body["status"] == "ok" and body["version"]


def test_session_lifecycle_and_state(base_url):
    _, _, created = call("POST", base_url + "/sessions", {})
    sid = created["session_id"]
    _, _, r1 = call("POST", base_url + "/execute", {"session_id": sid, "code": "x = 40 + 2"})
    assert r1["execution_count"] == 1 and r1["error"] is None
    _, _, r2 = call("POST", base_url + "/execute", {"session_id": sid, "code": "x + 1"})
    assert r2["display"][0]["data"] == "43"
    _, _, stats = call("GET", base_url + f"/sessions/{sid}/stats")
    assert stats["execution_count"] == 2
    _, _, listed = call("GET", base_url + "/sessions")
    assert any(s["session_id"] == sid for s in listed["sessions"])
    status, _, deleted = call("DELETE", base_url + f"/sessions/{sid}")
    assert status == 200 and deleted == {"deleted": True}
    assert call("GET", base_url + f"/sessions/{sid}/stats")[0] == 404


def test_sessions_are_isolated(base_url):
    a = call("POST", base_url + "/sessions", {})[2]["session_id"]
    b = call("POST", base_url + "/sessions", {})[2]["session_id"]
    call("POST", base_url + "/execute", {"session_id": a, "code": "v = 1"})
    _, _, r = call("POST", base_url + "/execute", {"session_id": b, "code": "v"})
    assert r["error"] is not None  # NameError: b never saw v


def test_unknown_session_404(base_url):
    assert call("POST", base_url + "/execute", {"session_id": "nope", "code": "1"})[0] == 404
    assert call("POST", base_url + "/interrupt", {"session_id": "nope"})[0] == 404
    assert call("DELETE", base_url + "/sessions/nope")[0] == 404


def test_bad_requests_400(base_url):
    assert call("POST", base_url + "/execute", {}, raw=b"{bad")[0] == 400
    assert call("POST", base_url + "/execute", {"session_id": "x"})[0] == 400
    assert call("POST", base_url + "/execute", {"code": "1"})[0] == 400


def test_interrupt_and_sandbox_defaults(base_url):
    sid = call("POST", base_url + "/sessions", {})[2]["session_id"]
    status, _, body = call("POST", base_url + "/interrupt", {"session_id": sid})
    assert status == 200 and body == {"interrupted": True}
    _, _, r = call("POST", base_url + "/execute", {"session_id": sid, "code": "!echo hi"})
    assert "disabled" in (r["error"] or "")


def test_session_cap_429():
    Handler.registry = SessionRegistry(max_sessions=2)
    httpd = serve("127.0.0.1", 0, 2)
    port = httpd.server_address[1]
    base = f"http://127.0.0.1:{port}"
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        assert call("POST", base + "/sessions", {})[0] == 201
        assert call("POST", base + "/sessions", {})[0] == 201
        status, _, body = call("POST", base + "/sessions", {})
        assert status == 429 and "limit" in body["error"]
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_cors_preflight_and_headers(base_url):
    req = urllib.request.Request(
        base_url + "/execute", method="OPTIONS",
        headers={"Origin": "https://localhost",
                 "Access-Control-Request-Method": "POST",
                 "Access-Control-Request-Headers": "Content-Type"},
    )
    with urllib.request.urlopen(req) as r:
        assert r.status == 204
        assert r.headers.get("Access-Control-Allow-Origin") == "*"
    _, headers, _ = call("POST", base_url + "/sessions", {})
    assert headers.get("Access-Control-Allow-Origin") == "*"

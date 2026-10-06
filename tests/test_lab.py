"""Lab GUI backend: static assets, notebook files, search, WS-LSP bridge."""

import base64
import json
import os
import socket
import struct
import threading
import urllib.error
import urllib.request

import pytest

from py_runner.lab import LabHandler, serve_lab


@pytest.fixture()
def lab(tmp_path):
    root = str(tmp_path)
    os.makedirs(os.path.join(root, "sub"), exist_ok=True)
    with open(os.path.join(root, "a.ipynb"), "w") as f:
        json.dump({"cells": [], "metadata": {}, "nbformat": 4, "nbformat_minor": 5}, f)
    with open(os.path.join(root, "sub", "b.ipynb"), "w") as f:
        json.dump({"cells": [], "metadata": {}, "nbformat": 4, "nbformat_minor": 5}, f)
    LabHandler.root = root
    httpd = serve_lab("127.0.0.1", 0, root, 100)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{port}", root
    httpd.shutdown()
    httpd.server_close()


def call(method, url, data=None, raw=None):
    body = raw if raw is not None else (json.dumps(data).encode() if data is not None else None)
    headers = {}
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, method=method, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def test_static_assets(lab):
    base, _ = lab
    for path, ctype in (("/", "text/html"), ("/js/app.js", "application/javascript"),
                        ("/js/vendor/cm.js", "application/javascript"),
                        ("/css/styles.css", "text/css")):
        status, headers, body = call("GET", base + path)
        assert status == 200, path
        assert ctype in headers.get("Content-Type", ""), path
        assert len(body) > 100, path
    assert call("GET", base + "/js/../server.py")[0] == 404


def test_files_browse_and_crud(lab):
    base, _ = lab
    _, _, root = call("GET", base + "/api/files")
    root = json.loads(root)
    assert root["files"] == ["a.ipynb"]
    assert [d["path"] for d in root["dirs"]] == ["sub"]

    _, _, sub = call("GET", base + "/api/files?path=sub")
    assert json.loads(sub)["files"] == ["sub/b.ipynb"]

    assert call("GET", base + "/api/files?path=../")[0] == 400
    assert call("GET", base + "/api/files?path=/etc")[0] == 400

    nb = {"cells": [{"cell_type": "code", "source": ["1"], "metadata": {},
                     "outputs": [], "execution_count": None}],
          "metadata": {}, "nbformat": 4, "nbformat_minor": 5}
    assert call("PUT", base + "/api/file?path=sub/n.ipynb", nb)[0] == 200
    status, _, body = call("GET", base + "/api/file?path=sub/n.ipynb")
    assert status == 200 and json.loads(body)["cells"][0]["source"] == ["1"]

    assert call("PUT", base + "/api/file?path=x.txt", nb)[0] == 400
    assert call("PUT", base + "/api/file?path=x.ipynb", {"bad": 1})[0] == 400
    assert call("GET", base + "/api/file?path=nope.ipynb")[0] == 404
    assert call("GET", base + "/api/file?path=/etc/passwd")[0] == 404


def test_search(lab):
    base, _ = lab
    _, _, body = call("GET", base + "/api/search?q=.ipynb")
    results = json.loads(body)["results"]
    assert "a.ipynb" in results and "sub/b.ipynb" in results
    _, _, body = call("GET", base + "/api/search?q=sub%2Fb")
    assert json.loads(body)["results"] == ["sub/b.ipynb"]
    assert call("GET", base + "/api/search")[0] == 400


def test_query_string_api_routes(lab):
    base, _ = lab
    assert call("GET", base + "/api/health?x=1")[0] == 200
    status, _, body = call("POST", base + "/api/sessions?x=1", {})
    assert status == 201 and json.loads(body)["session_id"]


# ── websocket bridge (raw client, stdlib only) ──────────────────────────

def _ws_connect(base, path="/lsp"):
    hostport = base.split("://", 1)[1]
    host, port = hostport.rsplit(":", 1)
    s = socket.create_connection((host, int(port)), timeout=10)
    key = base64.b64encode(os.urandom(16)).decode()
    s.sendall(f"GET {path} HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\n"
              f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
              f"Sec-WebSocket-Version: 13\r\n\r\n".encode())
    resp = b""
    while b"\r\n\r\n" not in resp:
        resp += s.recv(4096)
    assert resp.split(b"\r\n")[0] == b"HTTP/1.1 101 Switching Protocols"
    return s


def _ws_send_lsp(s, obj):
    body = json.dumps(obj).encode()
    payload = b"Content-Length: %d\r\n\r\n" % len(body) + body
    mask = os.urandom(4)
    n = len(payload)
    if n < 126:
        hdr = bytes([0x81, 0x80 | n])
    elif n < 65536:
        hdr = bytes([0x81, 0x80 | 126]) + struct.pack(">H", n)
    else:
        hdr = bytes([0x81, 0x80 | 127]) + struct.pack(">Q", n)
    s.sendall(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))


def _ws_recv_json(s, timeout=20):
    s.settimeout(timeout)
    h = s.recv(2)
    ln = h[1] & 0x7F
    if ln == 126:
        ln = struct.unpack(">H", s.recv(2))[0]
    elif ln == 127:
        ln = struct.unpack(">Q", s.recv(8))[0]
    data = b""
    while len(data) < ln:
        data += s.recv(ln - len(data))
    return json.loads(data.split(b"\r\n\r\n", 1)[-1])


def test_lsp_bridge_initialize_and_completion(lab):
    base, _ = lab
    if not __import__("py_runner.lab", fromlist=["LSP_ARGV"]).LSP_ARGV:
        pytest.skip("no language server installed")
    s = _ws_connect(base)
    try:
        _ws_send_lsp(s, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                         "params": {"processId": None, "rootUri": None, "capabilities": {}}})
        r = _ws_recv_json(s)
        assert r.get("id") == 1 and "capabilities" in (r.get("result") or {})
        _ws_send_lsp(s, {"jsonrpc": "2.0", "method": "initialized", "params": {}})
        _ws_send_lsp(s, {"jsonrpc": "2.0", "method": "textDocument/didOpen",
                         "params": {"textDocument": {"uri": "untitled:t.py", "languageId": "python",
                                                     "version": 1, "text": "import os\nos."}}})
        _ws_send_lsp(s, {"jsonrpc": "2.0", "id": 2, "method": "textDocument/completion",
                         "params": {"textDocument": {"uri": "untitled:t.py"},
                                    "position": {"line": 1, "character": 3}}})
        import time
        start = time.time()
        while time.time() - start < 20:
            m = _ws_recv_json(s)
            if m.get("id") == 2:
                items = (m.get("result") or {}).get("items", m.get("result") or [])
                labels = [i.get("label") for i in items if isinstance(i, dict)]
                assert any((l or "").startswith("path") for l in labels), labels[:5]
                return
        raise AssertionError("no completion response")
    finally:
        s.close()

"""Notebook GUI backend: static frontend + kernel API in one process.

Run with ``py-runner lab [--port 8000] [--dir .]``. All state-changing
kernel routes live under ``/api/*`` and reuse server.Handler; notebook
files are served from ``--dir`` with a traversal guard. Standard library only.
"""

import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import threading
from base64 import b64encode
from hashlib import sha1
from importlib import resources
from urllib.parse import parse_qs, urlparse

from .server import (
    MAX_BODY_BYTES,
    SessionRegistry,
    _serve_dual_stack,
)
from .server import (
    Handler as KernelHandler,
)


def find_lsp():
    """Locate a Python language server. Returns argv or None (never raises).

    `python-lsp-server` ships as a hard dependency, but installs can be
    partial (`--no-deps`) or broken — the GUI degrades to plain cells.
    """
    exe = shutil.which("pylsp")
    if exe:
        return [exe]
    try:
        subprocess.run(
            [sys.executable, "-m", "pylsp", "--version"],
            capture_output=True, timeout=10,
        )
        return [sys.executable, "-m", "pylsp"]
    except Exception:
        return None


LSP_ARGV = find_lsp()


# ── LSP hub: one pylsp process shared by all browser tabs ────────────────
# stdio LSP frames are multiplexed here: request ids are rewritten to a
# global namespace so concurrent notebooks share one ~100MB server instead
# of one each. stdlib only (raw WebSocket framing over the hijacked socket).

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _ws_accept(key: str) -> str:
    return b64encode(sha1((key + _WS_GUID).encode()).digest()).decode()


def _ws_send(sock, payload: bytes, opcode: int = 0x1) -> None:
    header = bytes([0x80 | opcode])
    n = len(payload)
    if n < 126:
        header += bytes([n])
    elif n < 65536:
        header += bytes([126]) + struct.pack(">H", n)
    else:
        header += bytes([127]) + struct.pack(">Q", n)
    sock.sendall(header + payload)


def _read_exact(sock, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("socket closed")
        buf += chunk
    return buf


def _ws_recv(sock):
    """Returns (opcode, payload) or raises ConnectionError."""
    head = _read_exact(sock, 2)
    fin = head[0] & 0x80
    opcode = head[0] & 0x0F
    masked = head[1] & 0x80
    length = head[1] & 0x7F
    if length == 126:
        length = struct.unpack(">H", _read_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack(">Q", _read_exact(sock, 8))[0]
    mask = _read_exact(sock, 4) if masked else None
    payload = _read_exact(sock, length) if length else b""
    if mask:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    if not fin:
        # Continuation: accumulate (rare from browsers, but correct).
        parts = [payload]
        while True:
            op2, more = _ws_recv(sock)
            parts.append(more)
            if op2 & 0xF0 or op2 == 0x0:
                break
        payload = b"".join(parts)
    return opcode, payload


def _read_lsp_message(stream) -> bytes:
    """Read one Content-Length-framed LSP message, header+body, raw."""
    raw_headers = b""

    def readline():
        line = stream.readline()
        if not line:
            raise ConnectionError("lsp closed")
        return line

    length = None
    while True:
        line = readline()
        raw_headers += line
        if line in (b"\r\n", b"\n", b""):
            break
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":", 1)[1].strip())
    if length is None:
        raise ValueError("LSP message without Content-Length")
    body = b""
    while len(body) < length:
        chunk = stream.read(length - len(body))
        if not chunk:
            raise ConnectionError("lsp closed")
        body += chunk
    return raw_headers + body


class LspHub:
    """Single pylsp shared by all WS clients, ids namespaced globally."""

    def __init__(self):
        self._lock = threading.Lock()
        self._proc = None
        self._next_id = 1
        self._routes = {}   # global_id -> (socket, orig_id)
        self._sockets = set()

    def _ensure_proc(self) -> bool:
        if self._proc is not None and self._proc.poll() is None:
            return True
        if not LSP_ARGV:
            return False
        try:
            self._proc = subprocess.Popen(
                LSP_ARGV,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
        except Exception:
            self._proc = None
            return False
        threading.Thread(target=self._pump, daemon=True).start()
        return True

    def _pump(self):
        """pylsp stdout -> route by id, broadcast notifications."""
        proc = self._proc
        try:
            while True:
                raw = _read_lsp_message(proc.stdout)
                try:
                    msg = json.loads(raw.split(b"\r\n\r\n", 1)[-1].split(b"\n\n", 1)[-1])
                except Exception:
                    continue
                target = None
                if isinstance(msg, dict) and "id" in msg:
                    with self._lock:
                        target = self._routes.pop(msg["id"], None)
                    if target is not None:
                        sock, orig = target
                        msg["id"] = orig
                        raw = self._reframe(raw, msg)
                        try:
                            _ws_send(sock, raw)
                        except Exception:
                            pass
                        continue
                # Notification (e.g. textDocument/publishDiagnostics): all.
                with self._lock:
                    socks = list(self._sockets)
                for sock in socks:
                    try:
                        _ws_send(sock, raw)
                    except Exception:
                        pass
        except Exception:
            pass

    @staticmethod
    def _reframe(raw: bytes, msg: dict) -> bytes:
        body = json.dumps(msg).encode()
        return (f"Content-Length: {len(body)}\r\n\r\n").encode() + body

    def forward(self, sock, raw: bytes) -> None:
        """Client bytes -> pylsp, rewriting request ids into global space."""
        if not self._ensure_proc():
            raise RuntimeError("no language server (pip install python-lsp-server)")
        try:
            head, _, body = raw.partition(b"\r\n\r\n")
            if not body:
                head, _, body = raw.partition(b"\n\n")
            msg = json.loads(body)
        except Exception:
            return
        if isinstance(msg, dict) and "id" in msg:
            with self._lock:
                gid = self._next_id
                self._next_id += 1
                self._routes[gid] = (sock, msg["id"])
            msg["id"] = gid
            raw = self._reframe(raw, msg)
        with self._lock:
            proc = self._proc
        try:
            proc.stdin.write(raw)
            proc.stdin.flush()
        except Exception:
            pass

    def attach(self, sock) -> None:
        with self._lock:
            self._sockets.add(sock)

    def detach(self, sock) -> None:
        with self._lock:
            self._sockets.discard(sock)
            self._routes = {k: v for k, v in self._routes.items() if v[0] is not sock}


_HUB = LspHub()

STATIC_DIR = resources.files("py_runner") / "lab_static"
MIME = {".html": "text/html", ".css": "text/css", ".js": "application/javascript"}


class LabHandler(KernelHandler):
    """Kernel API under /api/* + static GUI + notebook files."""

    root = os.getcwd()

    # ── static ───────────────────────────────────────────────────────
    def _send_static(self, name: str):
        try:
            data = (STATIC_DIR / name).read_bytes()
        except Exception:
            return self._send(404, {"error": "not found"})
        ext = os.path.splitext(name)[1]
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    # ── notebook files (jailed to root) ──────────────────────────────
    def _resolve(self, rel: str):
        if not rel or rel.startswith("/") or "\\" in rel:
            return None
        abs_path = os.path.realpath(os.path.join(self.root, rel))
        if abs_path != self.root and not abs_path.startswith(self.root + os.sep):
            return None
        return abs_path

    def _handle_files(self):
        """Immediate children of ?path=: subdirs + .ipynb files (Jupyter-like)."""
        query = parse_qs(urlparse(self.path).query)
        sub = (query.get("path") or [""])[0]
        base = self._resolve(sub) or self.root
        if not os.path.isdir(base):
            return self._send(400, {"error": "not a directory"})
        try:
            names = sorted(os.listdir(base))
        except Exception as e:
            return self._send(500, {"error": f"list failed: {e}"})
        rel = os.path.relpath(base, self.root)
        rel = "" if rel == "." else rel
        dirs, files = [], []
        for n in names:
            if n.startswith("."):
                continue
            full = os.path.join(base, n)
            p = f"{rel}/{n}" if rel else n
            if os.path.isdir(full):
                dirs.append({"name": n, "path": p})
            elif n.endswith(".ipynb"):
                files.append(p)
        return self._send(200, {"path": rel, "dirs": sorted(dirs, key=lambda d: d["name"]),
                               "files": sorted(files), "root": self.root})

    def _handle_search(self, limit: int = 200, max_dirs: int = 5000):
        """Recursive .ipynb search under root: GET /api/search?q=text."""
        query = parse_qs(urlparse(self.path).query)
        q = (query.get("q") or [""])[0].strip().lower()
        if not q:
            return self._send(400, {"error": "need ?q=text"})
        results = []
        seen_dirs = 0
        for dirpath, dirnames, filenames in os.walk(self.root, followlinks=False):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            seen_dirs += 1
            if seen_dirs > max_dirs:
                break
            for fn in sorted(filenames):
                if not fn.endswith(".ipynb") or fn.startswith("."):
                    continue
                rel = os.path.relpath(os.path.join(dirpath, fn), self.root)
                if q in rel.lower():
                    results.append(rel)
                    if len(results) >= limit:
                        return self._send(200, {"results": results, "truncated": True})
        return self._send(200, {"results": results, "truncated": False})

    def _handle_file_get(self):
        query = parse_qs(urlparse(self.path).query)
        path = self._resolve(query.get("path", [""])[0])
        if not path or not os.path.isfile(path):
            return self._send(404, {"error": "notebook not found"})
        try:
            with open(path, "r", encoding="utf-8") as f:
                nb = json.load(f)
        except Exception as e:
            return self._send(400, {"error": f"invalid notebook: {e}"})
        if not isinstance(nb, dict) or not isinstance(nb.get("cells"), list):
            return self._send(400, {"error": "invalid notebook: missing cells"})
        return self._send(200, nb)

    def _handle_file_put(self):
        query = parse_qs(urlparse(self.path).query)
        path = self._resolve(query.get("path", [""])[0])
        if not path or not path.endswith(".ipynb"):
            return self._send(400, {"error": "need ?path=<name>.ipynb inside root"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._send(400, {"error": "invalid Content-Length"})
        if length > MAX_BODY_BYTES:
            return self._send(400, {"error": "body too large"})
        try:
            nb = json.loads(self.rfile.read(length).decode() or "{}")
        except Exception:
            return self._send(400, {"error": "invalid JSON body"})
        if not isinstance(nb, dict) or not isinstance(nb.get("cells"), list):
            return self._send(400, {"error": "invalid notebook: missing cells"})
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(nb, f, indent=1)
        except Exception as e:
            return self._send(500, {"error": f"save failed: {e}"})
        return self._send(200, {"saved": True})

    # ── LSP websocket bridge ─────────────────────────────────────────
    def _handle_lsp(self):
        """Hijack the socket: browser WS <-> shared pylsp stdio."""
        key = self.headers.get("Sec-WebSocket-Key")
        if not key:
            return self._send(400, {"error": "websocket upgrade required"})
        try:
            sock = self.request
            sock.sendall(
                ("HTTP/1.1 101 Switching Protocols\r\n"
                 "Upgrade: websocket\r\n"
                 "Connection: Upgrade\r\n"
                 f"Sec-WebSocket-Accept: {_ws_accept(key.strip())}\r\n\r\n").encode()
            )
        except Exception:
            return
        self.close_connection = False
        _HUB.attach(sock)
        try:
            while True:
                try:
                    opcode, payload = _ws_recv(sock)
                except Exception:
                    break
                if opcode == 0x8:  # close
                    try:
                        _ws_send(sock, b"", opcode=0x8)
                    except Exception:
                        pass
                    break
                if opcode == 0x9:  # ping
                    try:
                        _ws_send(sock, payload, opcode=0xA)
                    except Exception:
                        break
                    continue
                if opcode not in (0x1, 0x2):
                    continue
                try:
                    _HUB.forward(sock, payload)
                except Exception as e:
                    try:
                        _ws_send(sock, json.dumps(
                            {"error": str(e)}).encode())
                    except Exception:
                        break
        finally:
            _HUB.detach(sock)
            self.close_connection = True
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                sock.close()
            except Exception:
                pass

    # ── routing: /api/* delegates to the kernel handler ──────────────
    def _as_api(self):
        """Strip /api prefix then dispatch to KernelHandler routes."""
        path = urlparse(self.path).path
        if path == "/api" or path.startswith("/api/"):
            rest = path[len("/api"):] or "/"
        else:
            return None
        saved = self.path
        self.path = rest
        try:
            if self.command == "GET":
                return KernelHandler.do_GET(self)
            if self.command == "POST":
                return KernelHandler.do_POST(self)
            if self.command == "DELETE":
                return KernelHandler.do_DELETE(self)
            if self.command == "OPTIONS":
                return KernelHandler.do_OPTIONS(self)
        finally:
            self.path = saved
        return None

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            return self._send_static("index.html")
        if self.path.startswith("/js/") or self.path.startswith("/css/"):
            name = self.path[1:]
            if ".." in name or "\\" in name:
                return self._send(404, {"error": "not found"})
            return self._send_static(name)
        if self.path == "/api/lsp-status":
            if LSP_ARGV:
                from urllib.request import pathname2url as _p2u

                return self._send(200, {
                    "available": True,
                    "command": LSP_ARGV,
                    "rootUri": "file://" + _p2u(self.root),
                })
            return self._send(200, {
                "available": False,
                "hint": "pip install python-lsp-server (ships with py-runner-kernel)",
            })
        if self.path == "/lsp":
            return self._handle_lsp()
        if self.path == "/api/files" or self.path.startswith("/api/files?"):
            return self._handle_files()
        if self.path == "/api/search" or self.path.startswith("/api/search?"):
            return self._handle_search()
        if self.path == "/api/file" or self.path.startswith("/api/file?"):
            return self._handle_file_get()
        if self.path == "/api" or self.path.startswith("/api/"):
            return self._as_api()
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/api" or self.path.startswith("/api/"):
            return self._as_api()
        return self._send(404, {"error": "not found"})

    def do_DELETE(self):
        if self.path == "/api" or self.path.startswith("/api/"):
            return self._as_api()
        return self._send(404, {"error": "not found"})

    def do_PUT(self):
        if self.path == "/api/file" or self.path.startswith("/api/file?"):
            return self._handle_file_put()
        if self.path == "/api" or self.path.startswith("/api/"):
            # PUT has no kernel route; answer preflight-style CORS only.
            return self._send(404, {"error": "not found"})
        return self._send(404, {"error": "not found"})

    def do_OPTIONS(self):
        return KernelHandler.do_OPTIONS(self)


def serve_lab(host: str = "localhost", port: int = 8000,
              root: str = ".", max_sessions: int = 100):
    import threading

    LabHandler.root = os.path.realpath(root)
    LabHandler.registry = SessionRegistry(max_sessions=max_sessions)
    httpd, extra = _serve_dual_stack(host, port, LabHandler)
    for srv in extra:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    return httpd


def main(argv=None) -> int:
    """Run `py-runner lab [--port 8000] [--dir .]`."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="py-runner lab",
        description="Notebook GUI (JupyterLab-like) + kernel server.",
    )
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--dir", default=".",
                        help="Notebook root directory (jailed)")
    parser.add_argument("--max-sessions", type=int, default=100)
    args = parser.parse_args(argv)

    httpd = serve_lab(args.host, args.port, args.dir, args.max_sessions)
    from .server import VERSION

    lsp_state = "ready" if LSP_ARGV else "missing — pip install python-lsp-server"
    print(f"py-runner lab {VERSION} (Python {sys.version.split()[0]})")
    print(f"Serving notebooks from {os.path.realpath(args.dir)} (jailed)")
    print(f" * LSP {lsp_state}")
    print(f" * Running on http://{args.host}:{args.port}/ (CTRL+C to quit)", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

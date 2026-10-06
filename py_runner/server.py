"""HTTP execution server. Standard library only.

Endpoints (all JSON):
    GET    /health               -> {"status": "ok", "sessions": N}
    GET    /sessions             -> {"sessions": [{session_id, execution_count, is_busy}]}
    POST   /sessions             -> {"session_id": ...}
    GET    /sessions/{id}/stats  -> Kernel.stats()
    POST   /execute              -> Kernel.execute result dict
    POST   /interrupt            -> {"interrupted": true}
    DELETE /sessions/{id}        -> {"deleted": true}

Sessions live until explicitly deleted or the server restarts — there is
deliberately no idle timeout. Bound the blast radius with --max-sessions.
"""

import json
import socket
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .kernel import Kernel


def _package_version() -> str:
    try:
        from importlib.metadata import version

        return version("py-runner-kernel")
    except Exception:
        return "dev"


VERSION = _package_version()

# Refuse absurd bodies before parsing (10 MB of code is never legit).
MAX_BODY_BYTES = 10 * 1024 * 1024


class SessionRegistry:
    """Thread-safe map of session_id -> in-process kernel.

    NOTE: subprocess isolation (ProcessKernel in process.py, worker.py)
    is parked: its input handshake proved flaky, so sessions run
    in-process again until that is resolved. No expiry by design."""

    def __init__(self, max_sessions: int = 100):
        self._lock = threading.Lock()
        self._sessions: dict = {}
        self.max_sessions = max(1, max_sessions)

    def create(self, **kernel_kwargs) -> str:
        with self._lock:
            if len(self._sessions) >= self.max_sessions:
                raise SessionLimitReached(self.max_sessions)
            sid = uuid.uuid4().hex
            self._sessions[sid] = Kernel(**kernel_kwargs)
            return sid

    def get(self, sid) -> Kernel | None:
        with self._lock:
            return self._sessions.get(sid)

    def delete(self, sid) -> bool:
        with self._lock:
            kernel = self._sessions.pop(sid, None)
        if kernel is None:
            return False
        try:
            kernel.interrupt()
        except Exception:
            pass
        return True

    def list(self) -> list:
        with self._lock:
            return [
                {
                    "session_id": sid,
                    "execution_count": k._execution_count,
                    "is_busy": k.is_busy,
                }
                for sid, k in self._sessions.items()
            ]

    def __len__(self):
        with self._lock:
            return len(self._sessions)


class SessionLimitReached(Exception):
    def __init__(self, limit: int):
        super().__init__(f"session limit reached ({limit})")
        self.limit = limit


class Handler(BaseHTTPRequestHandler):
    registry: SessionRegistry = SessionRegistry()
    server_version = "py-runner-serve/" + VERSION
    protocol_version = "HTTP/1.1"

    # ── helpers ──────────────────────────────────────────────────────
    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        # CORS: WebView frontends (e.g. Acode plugins) fetch cross-origin
        # with JSON bodies, which triggers preflight. Localhost dev server:
        # permissive origin is an accepted tradeoff, same posture as v1.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        """Answer CORS preflights for any path."""
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None, "invalid Content-Length"
        if length > MAX_BODY_BYTES:
            return None, "body too large"
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}, None
        try:
            data = json.loads(raw.decode())
        except Exception:
            return None, "invalid JSON body"
        if not isinstance(data, dict):
            return None, "JSON body must be an object"
        return data, None

    def handle_one_request(self):
        self._req_start = time.perf_counter()
        return super().handle_one_request()

    def log_message(self, fmt, *args):
        # Django runserver style: [29/Sep/2026 12:00:01] "GET /x HTTP/1.1" 200 58
        # BaseHTTPRequestHandler calls us as log_message('"%s" %s %s', requestline, code, size).
        try:
            requestline, code, size = args[0], str(args[1]), str(args[2])
        except Exception:
            return
        elapsed = time.perf_counter() - getattr(self, "_req_start", time.perf_counter())
        slow = f" ({elapsed:.2f}s)" if elapsed > 1.0 else ""
        stamp = time.strftime("%d/%b/%Y %H:%M:%S", time.localtime())
        line = f'[{stamp}] "{requestline}" {code} {size}{slow}'
        try:
            if sys.stderr.isatty():
                if code.startswith("2"):
                    line = f"\x1b[32m{line}\x1b[0m"
                elif code.startswith("4"):
                    line = f"\x1b[33m{line}\x1b[0m"
                elif code.startswith("5"):
                    line = f"\x1b[31m{line}\x1b[0m"
            sys.stderr.write(line + "\n")
            sys.stderr.flush()
        except Exception:
            pass

    # ── routing ──────────────────────────────────────────────────────
    def do_GET(self):
        if self.path == "/health":
            return self._send(
                200,
                {"status": "ok", "version": VERSION, "sessions": len(self.registry)},
            )
        if self.path == "/sessions":
            return self._send(200, {"sessions": self.registry.list()})
        if self.path.startswith("/sessions/") and self.path.endswith("/stats"):
            sid = self.path[len("/sessions/") : -len("/stats")]
            kernel = self.registry.get(sid)
            if kernel is None:
                return self._send(404, {"error": "unknown session"})
            return self._send(200, kernel.stats())
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/sessions":
            return self._handle_create()
        if self.path == "/execute":
            return self._handle_execute()
        if self.path == "/interrupt":
            return self._handle_interrupt()
        return self._send(404, {"error": "not found"})

    def do_DELETE(self):
        if self.path.startswith("/sessions/"):
            sid = self.path[len("/sessions/") :]
            if not sid or "/" in sid:
                return self._send(404, {"error": "not found"})
            if not self.registry.delete(sid):
                return self._send(404, {"error": "unknown session"})
            return self._send(200, {"deleted": True})
        return self._send(404, {"error": "not found"})

    # ── endpoints ────────────────────────────────────────────────────
    def _handle_create(self):
        data, err = self._read_json()
        if err:
            return self._send(400, {"error": err})
        if not isinstance(data, dict):
            return self._send(400, {"error": "JSON body must be an object"})
        try:
            sid = self.registry.create(
                allow_shell=bool(data.get("allow_shell", False)),
                allow_pip=bool(data.get("allow_pip", False)),
                max_history=int(data.get("max_history", 100)),
            )
        except SessionLimitReached as e:
            return self._send(429, {"error": str(e)})
        except (TypeError, ValueError):
            return self._send(400, {"error": "invalid session options"})
        return self._send(201, {"session_id": sid})

    def _handle_execute(self):
        data, err = self._read_json()
        if err:
            return self._send(400, {"error": err})
        sid = data.get("session_id")
        code = data.get("code")
        if not sid or not isinstance(code, str):
            return self._send(400, {"error": "need {session_id: str, code: str}"})
        kernel = self.registry.get(sid)
        if kernel is None:
            return self._send(404, {"error": "unknown session"})
        try:
            result = kernel.execute(
                code,
                timeout=data.get("timeout"),
                max_output_length=int(data.get("max_output_length", 10000)),
                max_display_length=int(data.get("max_display_length", 50000)),
            )
        except (TypeError, ValueError):
            return self._send(400, {"error": "invalid execute options"})
        return self._send(200, result)

    def _handle_interrupt(self):
        data, err = self._read_json()
        if err:
            return self._send(400, {"error": err})
        sid = data.get("session_id")
        kernel = self.registry.get(sid) if sid else None
        if kernel is None:
            return self._send(404, {"error": "unknown session"})
        kernel.interrupt()
        return self._send(200, {"interrupted": True})


def _serve_dual_stack(host: str, port: int, handler):
    """Serve loopback over IPv4+IPv6.

    Browsers often resolve `localhost` to ::1 first while other clients
    use 127.0.0.1. One socket cannot cover both here (binding ::1 forces
    V6ONLY and post-bind changes are rejected), so bind one loopback
    socket per family sharing the same handler/registry.
    Returns (primary, secondaries).
    """

    class _Loopback6(ThreadingHTTPServer):
        address_family = socket.AF_INET6

    if host in ("localhost", "127.0.0.1", "::1"):
        try:
            six = _Loopback6(("::1", port), handler)
            six.daemon_threads = True
            # Ephemeral (:0) ports differ per socket — pin v4 to v6's port.
            bound = six.server_address[1] if port == 0 else port
            four = ThreadingHTTPServer(("127.0.0.1", bound), handler)
            four.daemon_threads = True
            return six, [four]
        except Exception:
            try:
                six.server_close()
            except Exception:
                pass
            pass
        host = "127.0.0.1"
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    return httpd, []


def serve(host: str = "localhost", port: int = 8000, max_sessions: int = 100):
    Handler.registry = SessionRegistry(max_sessions=max_sessions)
    httpd, _extra = _serve_dual_stack(host, port, Handler)
    for extra in _extra:
        threading.Thread(target=extra.serve_forever, daemon=True).start()
    return httpd


def main(argv=None) -> int:
    """CLI entry point (`py-runner-serve`)."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="py-runner-serve",
        description="Serve py-runner kernels over HTTP+JSON.",
    )
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--max-sessions", type=int, default=100)
    args = parser.parse_args(argv)

    httpd = serve(args.host, args.port, args.max_sessions)
    print(f"py-runner-serve {VERSION} (Python {sys.version.split()[0]})")
    print(f" * Sessions capped at {args.max_sessions}, no idle timeout")
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

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
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .kernel import Kernel

# Refuse absurd bodies before parsing (10 MB of code is never legit).
MAX_BODY_BYTES = 10 * 1024 * 1024


class SessionRegistry:
    """Thread-safe map of session_id -> Kernel. No expiry by design."""

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
    server_version = "py-runner-serve/0.1.0"
    protocol_version = "HTTP/1.1"

    # ── helpers ──────────────────────────────────────────────────────
    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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

    def log_message(self, fmt, *args):  # quieter than BaseHTTPRequestHandler
        pass

    # ── routing ──────────────────────────────────────────────────────
    def do_GET(self):
        if self.path == "/health":
            return self._send(200, {"status": "ok", "sessions": len(self.registry)})
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


def serve(host: str = "127.0.0.1", port: int = 8000, max_sessions: int = 100):
    Handler.registry = SessionRegistry(max_sessions=max_sessions)
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    return httpd


def main(argv=None) -> int:
    """CLI entry point (`py-runner-serve`)."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="py-runner-serve",
        description="Serve py-runner kernels over HTTP+JSON.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--max-sessions", type=int, default=100)
    args = parser.parse_args(argv)

    httpd = serve(args.host, args.port, args.max_sessions)
    print(f"py-runner-serve on http://{args.host}:{args.port} "
          f"(max_sessions={args.max_sessions})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

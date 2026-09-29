"""Notebook GUI backend: static frontend + kernel API in one process.

Run with ``py-runner lab [--port 8000] [--dir .]``. All state-changing
kernel routes live under ``/api/*`` and reuse server.Handler; notebook
files are served from ``--dir`` with a traversal guard. Standard library only.
"""

import json
import os
import shutil
import subprocess
import sys
from importlib import resources
from urllib.parse import parse_qs, urlparse

from .server import (
    Handler as KernelHandler,
    SessionRegistry,
    MAX_BODY_BYTES,
    _serve_dual_stack,
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
                return self._send(200, {"available": True, "command": LSP_ARGV})
            return self._send(200, {
                "available": False,
                "hint": "pip install python-lsp-server (ships with py-runner-kernel)",
            })
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
    print(f"py-runner lab on http://{args.host}:{args.port} "
          f"(dir={os.path.realpath(args.dir)})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

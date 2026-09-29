"""Process-backed kernel: same API as Kernel, isolated OS process. Stdlib only.

Each ProcessKernel owns one ``python -u -m py_runner.worker`` child speaking
the stdio JSON-RPC from worker.py. Consequences vs in-process Kernel:

* Crash containment: segfault / os._exit / OOM-killer takes only the child.
  The parent marks the session crashed and respawns transparently.
* Real timeouts: SIGINT first, SIGKILL escalation — blocking C calls die too.
* Real interrupt: signal to the child process group, not a trace flag.

The simplified result dict is unchanged, so servers and GUIs work with either.
"""

import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from typing import Callable, Optional


class ProcessKernel:
    """A Kernel-equivalent whose state lives in a child process."""

    def __init__(
        self,
        on_input: Optional[Callable[[str], str]] = None,
        max_history: int = 100,
        allow_shell: bool = True,
        allow_pip: bool = True,
    ):
        self._on_input = on_input
        self.max_history = max_history
        self._allow_shell = allow_shell
        self._allow_pip = allow_pip
        self._execution_count = 0
        self._proc: Optional[subprocess.Popen] = None
        self._reader: Optional[threading.Thread] = None
        self._inbox: "queue.Queue[Optional[dict]]" = queue.Queue()
        self._lock = threading.Lock()
        self._interrupts = 0
        self._crashed = False
        self._restarts = 0
        self._busy = False
        self._spawn()

    # ── process management ───────────────────────────────────────────
    def _spawn(self) -> None:
        cmd = [
            sys.executable, "-u", "-m", "py_runner.worker",
            "--max-history", str(max(1, self.max_history)),
        ]
        cmd += ["--allow-shell" if self._allow_shell else "--no-shell"]
        cmd += ["--allow-pip" if self._allow_pip else "--no-pip"]
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        self._inbox = queue.Queue()
        self._reader = threading.Thread(target=self._drain, daemon=True)
        self._reader.start()
        self._drain_stderr()
        self._crashed = False
        self._execution_count = 0

    def _drain(self) -> None:
        """Reader thread: pipe lines -> inbox; None sentinel on EOF."""
        try:
            assert self._proc is not None and self._proc.stdout is not None
            for line in self._proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    self._inbox.put(json.loads(line))
                except Exception:
                    continue
        except Exception:
            pass
        finally:
            self._inbox.put(None)

    def _drain_stderr(self) -> None:
        """Keep the child's stderr pipe from filling (diagnostics discarded)."""

        def _eat() -> None:
            try:
                assert self._proc is not None and self._proc.stderr is not None
                for _ in self._proc.stderr:
                    pass
            except Exception:
                pass

        threading.Thread(target=_eat, daemon=True).start()

    def _send(self, obj: dict) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        self._proc.stdin.write(json.dumps(obj) + "\n")
        self._proc.stdin.flush()

    def _alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _signal_group(self, sig) -> bool:
        """Signal the whole child process group; fall back to the pid."""
        if self._proc is None:
            return False
        try:
            if hasattr(os, "killpg"):
                os.killpg(os.getpgid(self._proc.pid), sig)
            else:
                self._proc.send_signal(sig)
            return True
        except Exception:
            try:
                self._proc.send_signal(sig)
                return True
            except Exception:
                return False

    def _kill(self) -> None:
        if self._proc is None:
            return
        try:
            self._proc.kill()
        except Exception:
            pass
        try:
            self._proc.wait(timeout=5)
        except Exception:
            pass

    def _note_crash(self) -> None:
        self._crashed = True
        self._kill()

    @property
    def crashed(self) -> bool:
        return self._crashed or not self._alive()

    @property
    def is_busy(self) -> bool:
        return self._busy

    def _ensure_live(self) -> None:
        if not self._alive():
            if not self._crashed:
                self._note_crash()
            self._restarts += 1
            self._spawn()

    # ── public API (mirrors Kernel) ──────────────────────────────────
    def execute(
        self,
        code: str,
        max_output_length: int = 10000,
        max_display_length: int = 50000,
        on_stream: Optional[Callable[[str], None]] = None,
        on_input: Optional[Callable[[str], str]] = None,
        timeout: Optional[float] = None,
        heartbeat_interval: Optional[float] = None,
        on_heartbeat: Optional[Callable[[float], None]] = None,
    ) -> dict:
        if not self._lock.acquire(blocking=False):
            self._execution_count += 1
            return {
                "output": None,
                "display": [],
                "error": "KernelBusy: another cell is still running. "
                "Call interrupt() or wait for it to finish.",
                "execution_count": self._execution_count,
                "execution_time": 0.0,
            }
        try:
            return self._execute_locked(
                code, max_output_length, max_display_length,
                on_stream, on_input, timeout,
                heartbeat_interval, on_heartbeat,
            )
        finally:
            self._lock.release()

    def _execute_locked(
        self, code, max_output_length, max_display_length,
        on_stream, on_input, timeout, heartbeat_interval, on_heartbeat,
    ) -> dict:
        input_cb = on_input if on_input is not None else self._on_input
        self._ensure_live()
        self._execution_count += 1
        self._busy = True
        self._interrupts = 0
        start = time.perf_counter()
        deadline = start + timeout if timeout is not None else None
        next_beat = start + heartbeat_interval if heartbeat_interval else None
        error = None
        result = None
        try:
            self._send({
                "op": "execute", "code": code,
                "max_output_length": max_output_length,
                "max_display_length": max_display_length,
            })
            while True:
                remaining = None
                if deadline is not None:
                    remaining = max(0.0, deadline - time.perf_counter())
                if next_beat is not None:
                    wait_beat = max(0.0, next_beat - time.perf_counter())
                    remaining = wait_beat if remaining is None else min(remaining, wait_beat)
                try:
                    msg = self._inbox.get(timeout=remaining)
                except queue.Empty:
                    msg = "__timeout__"
                now = time.perf_counter()
                if msg is None:
                    # Child died mid-cell: respawn, report, move on.
                    code_ = self._proc.returncode if self._proc else "?"
                    self._note_crash()
                    self._restarts += 1
                    self._spawn()
                    error = (f"SessionCrashed: child exited with code {code_}; "
                             "state was lost and the session restarted.")
                    break
                if msg == "__timeout__":
                    error = self._on_timeout(timeout)
                    break
                kind = msg.get("event") if isinstance(msg, dict) else None
                if kind == "result":
                    result = msg.get("result") or {}
                    break
                if kind == "stream":
                    if on_stream:
                        try:
                            on_stream(msg.get("data", ""))
                        except Exception:
                            pass
                elif kind == "input_request":
                    prompt = msg.get("prompt", "")
                    if input_cb is None:
                        self._send({"op": "input_reply", "error":
                            "input() requires an on_input callback "
                            "(ProcessKernel(on_input=...) or execute(..., on_input=...))"})
                    else:
                        try:
                            self._send({"op": "input_reply",
                                        "value": input_cb(str(prompt))})
                        except Exception as e:
                            self._send({"op": "input_reply", "error": str(e)})
                if (next_beat is not None and now >= next_beat
                        and on_heartbeat is not None):
                    try:
                        on_heartbeat(round(now - start, 3))
                    except Exception:
                        pass
                    next_beat = now + (heartbeat_interval or 0)
        except Exception as e:  # pipe broke mid-cell
            self._note_crash()
            error = f"SessionCrashed: lost contact with child ({e}); session restarted."
            self._restarts += 1
            self._spawn()
        finally:
            self._busy = False

        elapsed = round(time.perf_counter() - start, 6)
        if error is not None:
            return {
                "output": None,
                "display": [],
                "error": error,
                "execution_count": self._execution_count,
                "execution_time": elapsed,
            }
        result = result or {}
        result.setdefault("execution_count", self._execution_count)
        result.setdefault("execution_time", elapsed)
        return result

    def _on_timeout(self, timeout) -> str:
        """SIGINT first (clean abort), SIGKILL escalation (real stop)."""
        self._signal_group(signal.SIGINT)
        grace_until = time.perf_counter() + 1.0
        while time.perf_counter() < grace_until:
            try:
                msg = self._inbox.get(timeout=0.05)
            except queue.Empty:
                if not self._alive():
                    break
                continue
            if msg is None:
                break
            if isinstance(msg, dict) and msg.get("event") == "result":
                child_err = (msg.get("result") or {}).get("error")
                return child_err or (
                    f"TimeoutError: cell exceeded {timeout}s (interrupted).")
        if self._alive():
            self._note_crash()
            self._restarts += 1
            self._spawn()
            return (f"TimeoutError: cell exceeded {timeout}s and ignored SIGINT; "
                    "child killed and session restarted (state lost).")
        return f"TimeoutError: cell exceeded {timeout}s (interrupted)."

    def execute_json(
        self,
        code: str,
        on_stream: Optional[Callable[[str], None]] = None,
        timeout: Optional[float] = None,
    ) -> str:
        import json as _json

        result = self.execute(code, on_stream=on_stream, timeout=timeout)
        preview = dict(result)
        preview["display"] = [
            {**d, "data": f"<image/png {len(d['data'])} chars base64>"}
            if d["type"] == "image/png"
            else d
            for d in result.get("display", [])
        ]
        return _json.dumps(preview, indent=2)

    def interrupt(self) -> None:
        """SIGINT the child group; second call escalates to SIGKILL."""
        self._interrupts += 1
        if self._interrupts == 1:
            self._signal_group(signal.SIGINT)
        else:
            self._note_crash()
            self._restarts += 1
            self._spawn()

    def reset(self, keep_imports: bool = False) -> None:
        self._ensure_live()
        try:
            self._send({"op": "reset", "keep_imports": keep_imports})
            self._wait_for("ok", 30)
        except Exception:
            self._note_crash()
            self._restarts += 1
            self._spawn()

    def _wait_for(self, event: str, timeout: float) -> dict:
        deadline = time.perf_counter() + timeout
        while True:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                raise TimeoutError(f"no {event} from child")
            try:
                msg = self._inbox.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                continue
            if msg is None:
                raise ConnectionError("child died")
            if isinstance(msg, dict) and msg.get("event") == event:
                return msg

    @property
    def namespace(self) -> dict:
        try:
            self._ensure_live()
            self._send({"op": "namespace"})
            return self._wait_for("namespace", 30).get("namespace", {})
        except Exception:
            return {}

    def stats(self) -> dict:
        try:
            self._ensure_live()
            self._send({"op": "stats"})
            child = self._wait_for("stats", 5).get("stats", {})
        except Exception:
            child = {}
        info = {
            "execution_count": self._execution_count,
            "crashed": self.crashed,
            "restarts": self._restarts,
            "is_busy": self._busy,
            "pid": self._proc.pid if self._proc else None,
        }
        info.update(child)
        return info

    def close(self) -> None:
        try:
            if self._alive():
                self._send({"op": "shutdown"})
                self._proc.wait(timeout=3)
        except Exception:
            pass
        finally:
            self._kill()

    def __del__(self):  # best effort only
        try:
            self._kill()
        except Exception:
            pass

"""Persistent execution kernel. Standard library only."""

import ast
import io
import json
import threading
import time
import traceback
from typing import Callable, Optional

from .display import DisplayCollector
from .magics import MagicHandler
from .streams import StreamingBuffer, ThreadRoutedProxy
from .utils import try_import


class Kernel:
    """
    A persistent Python execution kernel.

    Input: Python code string.
    Output: simplified dict (JSON-serializable):
        - output (str | None)
        - display (list of {type, data})
        - error (str | None)
        - execution_count (int)
        - execution_time (float)
    """

    def __init__(
        self,
        on_input: Optional[Callable[[str], str]] = None,
        max_history: int = 100,
        allow_shell: bool = True,
        allow_pip: bool = True,
    ):
        """Create an isolated session kernel.

        Each Kernel has its own namespace — use one per user/session.
        Args:
            on_input: Default callback for input(prompt).
            max_history: Cap on Out[N] entries; oldest evicted first.
            allow_shell: Permit `!cmd` shell escapes (set False to sandbox).
            allow_pip: Permit `%pip` installs (set False to sandbox).
        """
        self._namespace = {}
        self._execution_count = 0
        self._out_history = {}  # Out[N] like Jupyter, bounded by max_history
        self.max_history = max(1, max_history)
        self.allow_shell = allow_shell
        self.allow_pip = allow_pip
        self._magic = MagicHandler(self)
        self._worker: Optional[threading.Thread] = None
        self._busy = threading.Lock()
        self._interrupt = threading.Event()
        self._on_input = on_input
        self._setup_namespace()

    def _setup_namespace(self):
        self._namespace["__builtins__"] = __builtins__

        # Inject display() function
        collector_ref = [None]

        def display(*objs, **kwargs):
            c = collector_ref[0]
            if c is None:
                return
            pd = try_import("pandas")
            PIL_Image = try_import("PIL.Image")
            for obj in objs:
                if pd and isinstance(obj, pd.DataFrame):
                    c.add("text/html", obj.to_html(max_rows=20, max_cols=20))
                elif pd and isinstance(obj, pd.Series):
                    c.add("text/html", obj.to_frame().to_html(max_rows=20))
                elif PIL_Image and isinstance(obj, PIL_Image.Image):
                    c.capture_pil_image(obj)
                else:
                    c.add("text/plain", repr(obj))

        def HTML(data):
            c = collector_ref[0]
            if c:
                c.add("text/html", data)

        def Markdown(data):
            c = collector_ref[0]
            if c:
                c.add("text/markdown", data)

        self._namespace["display"] = display
        self._namespace["HTML"] = HTML
        self._namespace["Markdown"] = Markdown
        self._collector_ref = collector_ref

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
        """
        Execute a code string in the persistent namespace.

        Args:
            code: Python code to run.
            max_output_length: Truncate stdout beyond this length.
            max_display_length: Truncate each display item beyond this length.
            on_stream: Optional callback called with each chunk of stdout
                       as it's written.
            on_input: Optional callback for input(prompt) -> str. If the
                      code calls input() and no callback is set, a clear
                      EOFError is returned instead of hanging.
            timeout: Max seconds to wait for the cell. On expiry the
                     interrupt flag is set (aborts pure-Python code at the
                     next line) and a TimeoutError is returned. Blocking C
                     extensions may keep running in the background daemon.
            heartbeat_interval: If set with on_heartbeat, call
                     on_heartbeat(elapsed) every N seconds while a long
                     cell runs with no output (e.g. silent model.fit).
            on_heartbeat: Callback for silent long runs.

        Returns a simplified dict (JSON-serializable).
        """
        # Handle magic commands first
        magic_result = self._magic.handle(code)
        if magic_result and magic_result.get("magic"):
            self._execution_count += 1
            return {
                "output": magic_result.get("output"),
                "display": magic_result.get("display", []),
                "error": magic_result.get("error"),
                "execution_count": self._execution_count,
                "execution_time": 0.0,
            }

        # One cell at a time (notebooks are single-threaded per kernel).
        if not self._busy.acquire(blocking=False):
            return {
                "output": None,
                "display": [],
                "error": "KernelBusy: another cell is still running. "
                "Call interrupt() or wait for it to finish.",
                "execution_count": self._execution_count + 1,
                "execution_time": 0.0,
            }

        self._execution_count += 1
        collector = DisplayCollector(max_length=max_display_length)
        self._collector_ref[0] = collector

        # Setup matplotlib Agg backend (optional, guarded)
        mpl = try_import("matplotlib")
        if mpl:
            mpl.use("Agg")

        # Patch tqdm to write to stdout properly (optional, guarded)
        self._patch_tqdm()

        # input() support: shadow builtin so code never blocks on stdin.
        input_cb = on_input if on_input is not None else self._on_input
        prev_input = self._namespace.get("input", None)
        had_input = "input" in self._namespace

        def _kernel_input(prompt: str = "") -> str:
            if prompt:
                # Mirror CPython: prompt goes to stdout.
                print(prompt, end="", flush=True)
            if input_cb is None:
                raise EOFError(
                    "input() requires an on_input callback "
                    "(Kernel(on_input=...) or execute(..., on_input=...))"
                )
            return input_cb(str(prompt))

        self._namespace["input"] = _kernel_input

        stdout_buf = StreamingBuffer(on_chunk=on_stream)
        stderr_buf = io.StringIO()
        suppress_display = code.strip().endswith(";")

        self._interrupt.clear()
        start = time.perf_counter()
        state: dict = {"error": None, "result": None}

        def _target() -> None:
            import logging
            import sys
            import warnings

            owner = threading.get_ident()

            # Cooperative interrupt: abort pure-Python code at the next
            # line once interrupt()/timeout sets the flag. Scoped to the
            # worker thread so other threads are never traced.
            def _tracer(frame, event, arg):
                if threading.get_ident() != owner:
                    return None
                if self._interrupt.is_set():
                    raise KeyboardInterrupt("Cell execution interrupted.")
                return _tracer

            # Route logging + warnings into the captured buffers so
            # training logs show up in output/streaming (stdlib only).
            # Only records from the worker thread are captured.
            class _BufLogHandler(logging.Handler):
                def emit(self, record):
                    if record.thread != owner:
                        return
                    try:
                        stdout_buf.write(self.format(record) + "\n")
                    except Exception:
                        pass

            log_handler = _BufLogHandler()
            log_handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
            root_log = logging.getLogger()
            root_log.addHandler(log_handler)
            showwarning_prev = warnings.showwarning

            def _showwarning(message, category, filename, lineno, *args, **kwargs):
                if threading.get_ident() != owner:
                    return showwarning_prev(message, category, filename, lineno, *args, **kwargs)
                stderr_buf.write(f"{category.__name__}: {message}\n")

            warnings.showwarning = _showwarning
            # Thread-routed streams: only the worker's writes are captured;
            # other threads keep writing to the real stdout/stderr.
            out_proxy = ThreadRoutedProxy(sys.stdout)
            err_proxy = ThreadRoutedProxy(sys.stderr)
            prev_out, prev_err = sys.stdout, sys.stderr
            sys.stdout, sys.stderr = out_proxy, err_proxy
            out_proxy.route(stdout_buf)
            err_proxy.route(stderr_buf)
            try:
                sys.settrace(_tracer)
                try:
                    self._run_cell(code)
                    state["result"] = self._run_cell_result
                except KeyboardInterrupt:
                    state["error"] = "KeyboardInterrupt: Cell execution interrupted."
                except Exception:
                    state["error"] = traceback.format_exc()
            finally:
                sys.settrace(None)
                sys.stdout, sys.stderr = prev_out, prev_err
                warnings.showwarning = showwarning_prev
                try:
                    root_log.removeHandler(log_handler)
                except Exception:
                    pass

        worker = threading.Thread(target=_target, daemon=True)
        self._worker = worker
        worker.start()

        # Wait with optional timeout + heartbeat for silent long runs.
        timed_out = False
        try:
            if timeout is None and heartbeat_interval is None:
                worker.join()
            else:
                deadline = start + timeout if timeout is not None else None
                next_beat = (
                    start + heartbeat_interval if heartbeat_interval else None
                )
                while worker.is_alive():
                    slice_end = time.perf_counter() + 0.05
                    remaining = slice_end - time.perf_counter()
                    if deadline is not None:
                        remaining = min(remaining, max(0.0, deadline - time.perf_counter()))
                    if next_beat is not None:
                        remaining = min(remaining, max(0.0, next_beat - time.perf_counter()))
                    worker.join(timeout=max(0.0, remaining))
                    now = time.perf_counter()
                    if deadline is not None and now >= deadline and worker.is_alive():
                        timed_out = True
                        self._interrupt.set()
                        worker.join(timeout=1.0)  # grace period for tracer
                        break
                    if (
                        next_beat is not None
                        and now >= next_beat
                        and worker.is_alive()
                        and on_heartbeat is not None
                    ):
                        try:
                            on_heartbeat(round(now - start, 3))
                        except Exception:
                            pass
                        next_beat = now + heartbeat_interval
        finally:
            self._worker = None
            self._busy.release()

        error = state["error"]
        last_result = state["result"]

        # Restore previous input binding.
        if had_input:
            self._namespace["input"] = prev_input
        else:
            self._namespace.pop("input", None)

        if timed_out and worker.is_alive():
            error = (
                (error + "\n" if error else "")
                + f"TimeoutError: cell exceeded {timeout}s and is still "
                "running in the background (blocking C call?). "
                "Its output will be discarded; call reset() for a clean state."
            )
            last_result = None
        elif timed_out:
            error = (error + "\n" if error else "") + (
                f"TimeoutError: cell exceeded {timeout}s (interrupted)."
            )
            last_result = None

        # Store in Out[N] like Jupyter (skip when suppressed/error/None).
        if last_result is not None and not error and not suppress_display:
            self._remember(last_result)
        elif suppress_display:
            last_result = None

        elapsed = round(time.perf_counter() - start, 6)

        # Capture plots
        collector.capture_matplotlib()

        # Auto-display last expression
        if last_result is not None and not error:
            collector.capture_last_expression(last_result)

        # Finalize stdout
        stdout = stdout_buf.getvalue()
        if len(stdout) > max_output_length:
            stdout = (
                stdout[:max_output_length]
                + f"\n... [truncated at {max_output_length} chars]"
            )
        stdout = stdout.strip() or None

        stderr = stderr_buf.getvalue().strip()
        if stderr and not error:
            error = stderr
        elif stderr and error:
            error = stderr + "\n" + error

        self._collector_ref[0] = None

        return {
            "output": stdout,
            "display": collector.items,
            "error": error,
            "execution_count": self._execution_count,
            "execution_time": elapsed,
        }

    def execute_json(
        self,
        code: str,
        on_stream: Optional[Callable[[str], None]] = None,
        timeout: Optional[float] = None,
    ) -> str:
        result = self.execute(code, on_stream=on_stream, timeout=timeout)
        preview = dict(result)
        preview["display"] = [
            {**d, "data": f"<image/png {len(d['data'])} chars base64>"}
            if d["type"] == "image/png"
            else d
            for d in result["display"]
        ]
        return json.dumps(preview, indent=2)

    def interrupt(self):
        """Signal the running cell to stop at the next Python line.

        Cooperative: pure-Python loops abort promptly; blocking C
        calls (sleep, some fit/predict) may ignore it until they return.
        """
        self._interrupt.set()

    @property
    def is_busy(self) -> bool:
        return self._worker is not None and self._worker.is_alive()

    def reset(self, keep_imports: bool = False):
        """Clear all variables and reset the kernel.

        Args:
            keep_imports: If True, preserve names bound to modules
                          (e.g. `import numpy as np` survives the reset).
        """
        modules = {}
        if keep_imports:
            import types

            modules = {
                k: v
                for k, v in self._namespace.items()
                if isinstance(v, types.ModuleType)
            }
        self._namespace = {}
        self._execution_count = 0
        self._out_history = {}
        self._setup_namespace()
        self._namespace.update(modules)

    def clear_history(self) -> None:
        """Drop Out[N] entries and `_`/`__`/`___` refs to free memory."""
        self._out_history = {}
        for name in ("_", "__", "___"):
            self._namespace.pop(name, None)

    def stats(self) -> dict:
        """Best-effort memory/usage snapshot (stdlib only)."""
        import sys

        total = 0
        for k, v in self._namespace.items():
            try:
                total += sys.getsizeof(k) + sys.getsizeof(v)
            except Exception:
                pass
        return {
            "execution_count": self._execution_count,
            "history_len": len(self._out_history),
            "max_history": self.max_history,
            "namespace_vars": len(self._namespace),
            "namespace_bytes_approx": total,
            "is_busy": self.is_busy,
        }

    def _remember(self, value) -> None:
        """Store a result in Out[N], evicting oldest beyond max_history."""
        self._out_history[self._execution_count] = value
        while len(self._out_history) > self.max_history:
            self._out_history.pop(min(self._out_history))
        keys = sorted(self._out_history.keys())
        self._namespace["_"] = self._out_history[keys[-1]]
        if len(keys) >= 2:
            self._namespace["__"] = self._out_history[keys[-2]]
        else:
            self._namespace.pop("__", None)
        if len(keys) >= 3:
            self._namespace["___"] = self._out_history[keys[-3]]
        else:
            self._namespace.pop("___", None)

    @property
    def namespace(self) -> dict:
        return {
            k: repr(v)
            for k, v in self._namespace.items()
            if not k.startswith("__")
            and k not in ("display", "HTML", "Markdown", "input")
        }

    def _patch_tqdm(self):
        """Patch tqdm to use regular print instead of carriage returns."""
        tqdm = try_import("tqdm")
        if tqdm is None:
            return
        try:
            tqdm.tqdm.__init__.__defaults__
            # Set tqdm to write to stdout with newlines instead of \r
            import tqdm as tqdm_module

            tqdm_module.tqdm = tqdm_module.tqdm
        except Exception:
            pass

    # ── Cell runner (ast-based) ───────────────────────────────────────────
    _run_cell_result = None

    def _run_cell(self, code: str) -> None:
        """Run a cell; sets _run_cell_result to last Expr value or None.

        - Parses with ast: if the last top-level node is an expression,
          exec the preceding body then eval the expression (Jupyter-like).
        - Trailing ';' is handled by the caller (suppresses display).
        - Empty/whitespace-only code yields None.
        """
        self._run_cell_result = None
        if not code.strip():
            return
        tree = ast.parse(code, mode="exec")
        if not tree.body:
            return
        if isinstance(tree.body[-1], ast.Expr):
            body = tree.body[:-1]
            last = tree.body[-1]
            if body:
                exec(
                    compile(ast.Module(body, []), "<cell>", "exec"),
                    self._namespace,
                )
            self._run_cell_result = eval(
                compile(ast.Expression(last.value), "<cell>", "eval"),
                self._namespace,
            )
        else:
            exec(compile(tree, "<cell>", "exec"), self._namespace)

"""IPython-style magic commands. Standard library only."""

import time
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .kernel import Kernel


class MagicHandler:
    """Handles IPython-style magic commands."""

    def __init__(self, kernel: "Kernel"):
        self._kernel = kernel

    def handle(self, code: str) -> Optional[dict]:
        """
        Returns a result dict if the code is a magic command, else None.
        """
        stripped = code.strip()

        # %timeit / %%timeit  (must check before %time)
        if stripped.startswith("%%timeit") or stripped.startswith("%timeit"):
            return self._handle_timeit(stripped)

        # %%time / %time
        if stripped.startswith("%%time") or stripped.startswith("%time"):
            return self._handle_time(stripped)

        # %who / %whos
        if stripped in ("%who", "%whos"):
            return self._handle_who(stripped)

        # %reset
        if stripped == "%reset":
            self._kernel.reset()
            return {"magic": True, "output": "Kernel reset.", "display": []}

        # %run
        if stripped.startswith("%run "):
            return self._handle_run(stripped[5:].strip())

        # %pip [...]: install packages into the running interpreter
        if stripped == "%pip" or stripped.startswith("%pip "):
            return self._handle_pip(stripped[4:].strip())

        # !shell command (Jupyter parity)
        if stripped.startswith("!"):
            return self._handle_shell(stripped[1:].strip())

        # %matplotlib inline (no-op — we always use Agg)
        if stripped.startswith("%matplotlib"):
            return {
                "magic": True,
                "output": "matplotlib backend: Agg (inline)",
                "display": [],
            }

        # %%skip / unknown — return None to execute normally
        return None

    def _handle_time(self, code: str) -> dict:
        lines = code.split("\n")
        actual_code = (
            "\n".join(lines[1:])
            if lines[0].strip() in ("%time", "%%time")
            else lines[0][6:].strip()
        )
        if not actual_code.strip():
            return {
                "magic": True,
                "output": "Usage: %time <code> or %%time\n<code>",
                "display": [],
            }
        start = time.perf_counter()
        result = self._kernel.execute(actual_code)
        elapsed = time.perf_counter() - start
        result["output"] = (result["output"] or "") + f"\nWall time: {elapsed:.4f}s"
        return result

    def _handle_timeit(self, code: str) -> dict:
        import timeit

        lines = code.strip().split("\n")
        first = lines[0].strip()
        # %%timeit — body is everything after first line
        if first in ("%%timeit", "%timeit") or first.startswith("%%timeit"):
            actual_code = "\n".join(lines[1:]).strip()
        else:
            actual_code = first.replace("%timeit", "").strip()
        if not actual_code:
            return {"magic": True, "output": "Usage: %timeit <code>", "display": []}
        try:
            number = 100
            t = timeit.timeit(
                actual_code, globals=self._kernel._namespace, number=number
            )
            avg = t / number * 1000
            output = f"{number} loops, best of 3: {avg:.4f} ms per loop"
        except Exception as e:
            output = f"timeit error: {e}"
        return {"magic": True, "output": output, "display": []}

    def _handle_who(self, cmd: str) -> dict:
        ns = self._kernel.namespace
        if not ns:
            return {"magic": True, "output": "No variables defined.", "display": []}
        if cmd == "%whos":
            lines = [f"{'Variable':<20} {'Type':<15} {'Value'}", "-" * 60]
            for k, v in ns.items():
                vtype = type(self._kernel._namespace[k]).__name__
                lines.append(f"{k:<20} {vtype:<15} {v[:30]}")
            return {"magic": True, "output": "\n".join(lines), "display": []}
        return {"magic": True, "output": "  ".join(ns.keys()), "display": []}

    def _handle_run(self, filepath: str) -> dict:
        try:
            with open(filepath, "r") as f:
                code = f.read()
            return self._kernel.execute(code)
        except FileNotFoundError:
            return {
                "magic": True,
                "output": None,
                "error": f"FileNotFoundError: {filepath}",
                "display": [],
            }

    def _handle_pip(self, args: str) -> dict:
        """Run `python -m pip <args>` and return its output (stdlib only)."""
        import subprocess
        import sys

        if not getattr(self._kernel, "allow_pip", True):
            return {
                "magic": True,
                "output": None,
                "error": "PermissionError: %pip is disabled on this kernel.",
                "display": [],
            }

        if not args.strip():
            return {
                "magic": True,
                "output": "Usage: %pip install <package> [...]",
                "display": [],
            }
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "pip", *args.split()],
                capture_output=True,
                text=True,
                timeout=300,
            )
            output = (proc.stdout or "") + (proc.stderr or "")
            output = output.strip() or None
            error = None if proc.returncode == 0 else f"pip exited with code {proc.returncode}"
            return {"magic": True, "output": output, "error": error, "display": []}
        except Exception as e:
            return {"magic": True, "output": None, "error": f"pip failed: {e}", "display": []}

    def _handle_shell(self, cmd: str) -> dict:
        """Run `!cmd` via the system shell (stdlib only)."""
        import subprocess

        if not getattr(self._kernel, "allow_shell", True):
            return {
                "magic": True,
                "output": None,
                "error": "PermissionError: shell escapes are disabled on this kernel.",
                "display": [],
            }

        if not cmd:
            return {"magic": True, "output": None, "display": []}
        try:
            proc = subprocess.run(
                cmd, shell=True, capture_output=True, text=True, timeout=120
            )
            output = (proc.stdout or "").strip() or None
            error = (proc.stderr or "").strip() or None
            if proc.returncode != 0 and error is None:
                error = f"shell exited with code {proc.returncode}"
            return {"magic": True, "output": output, "error": error, "display": []}
        except Exception as e:
            return {"magic": True, "output": None, "error": f"shell failed: {e}", "display": []}

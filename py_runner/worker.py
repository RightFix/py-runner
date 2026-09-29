"""Child-process execution engine (stdio JSON-RPC). Standard library only.

Launched as ``python -u -m py_runner.worker`` by the parent session.
Protocol is one JSON object per line on stdin/stdout:

    parent -> child: {"op": "execute", "code": str, "max_output_length": int,
                      "max_display_length": int}
    parent -> child: {"op": "reset", "keep_imports": bool}
    parent -> child: {"op": "stats"}
    parent -> child: {"op": "namespace"}
    parent -> child: {"op": "input_reply", "value": str} | {"op": "input_reply", "error": str}
    parent -> child: {"op": "shutdown"}

    child -> parent: {"event": "result", "result": {...}}
    child -> parent: {"event": "stream", "data": str}
    child -> parent: {"event": "input_request", "prompt": str}
    child -> parent: {"event": "ok"} | {"event": "stats", ...} | {"event": "namespace", ...}

Cell stdout/stderr never touch the pipe directly — they are captured by the
in-process Kernel exactly as in library mode. SIGINT is trapped and forwarded
to the kernel's cooperative interrupt flag so the parent can stop cells via
signals without killing the loop itself.
"""

import argparse
import json
import signal
import sys

from .kernel import Kernel


def _send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="py_runner.worker")
    parser.add_argument("--max-history", type=int, default=100)
    parser.add_argument("--allow-shell", dest="allow_shell", action="store_true", default=True)
    parser.add_argument("--no-shell", dest="allow_shell", action="store_false")
    parser.add_argument("--allow-pip", dest="allow_pip", action="store_true", default=True)
    parser.add_argument("--no-pip", dest="allow_pip", action="store_false")
    args = parser.parse_args(argv)

    kernel = Kernel(
        max_history=args.max_history,
        allow_shell=args.allow_shell,
        allow_pip=args.allow_pip,
    )

    def _on_sigint(signum, frame):
        # Cooperative: the cell tracer aborts at the next Python line.
        # Never raise here — the main loop sits in readline() and must survive.
        kernel._interrupt.set()

    try:
        signal.signal(signal.SIGINT, _on_sigint)
    except Exception:
        pass

    def on_input(prompt: str = "") -> str:
        _send({"event": "input_request", "prompt": str(prompt)})
        line = sys.stdin.readline()
        if not line:
            raise EOFError("parent went away during input()")
        try:
            msg = json.loads(line)
        except Exception:
            raise EOFError("bad input reply from parent")
        if msg.get("op") != "input_reply":
            raise EOFError(f"unexpected message during input(): {msg.get('op')}")
        if "error" in msg and msg["error"]:
            raise EOFError(msg["error"])
        return msg.get("value", "")

    def on_stream(chunk: str) -> None:
        _send({"event": "stream", "data": chunk})

    # NOTE: pure readline() loop — never mix `for line in sys.stdin`
    # iteration with explicit readline(): the iterator's readahead can
    # swallow lines (e.g. input replies) that readline() then waits on forever.
    while True:
        line = sys.stdin.readline()
        if not line:
            break  # parent went away (EOF)
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        op = msg.get("op")
        if op == "execute":
            result = kernel.execute(
                msg.get("code", ""),
                max_output_length=int(msg.get("max_output_length", 10000)),
                max_display_length=int(msg.get("max_display_length", 50000)),
                on_stream=on_stream,
                on_input=on_input,
            )
            _send({"event": "result", "result": result})
        elif op == "reset":
            kernel.reset(keep_imports=bool(msg.get("keep_imports", False)))
            _send({"event": "ok"})
        elif op == "stats":
            _send({"event": "stats", "stats": kernel.stats()})
        elif op == "namespace":
            _send({"event": "namespace", "namespace": kernel.namespace})
        elif op == "shutdown":
            break
        # Unknown ops and stray input_reply lines are ignored.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

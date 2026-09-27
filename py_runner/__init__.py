"""PyRunner - string in, JSON out. Standard library only."""

from .display import DisplayCollector, _DisplayCollector
from .kernel import Kernel
from .magics import MagicHandler
from .streams import StreamingBuffer, ThreadRoutedProxy, _StreamingBuffer
from .utils import _try_import, set_address_space_limit, try_import

__all__ = [
    "DisplayCollector",
    "Kernel",
    "MagicHandler",
    "StreamingBuffer",
    "ThreadRoutedProxy",
    "execute",
    "execute_json",
    "main",
    "reset",
    "set_address_space_limit",
    "try_import",
]

# ── Module-level default kernel ───────────────────────────────────────────────
_default_kernel = Kernel()


def execute(
    code: str,
    on_stream=None,
    on_input=None,
    max_output_length=10000,
    max_display_length=50000,
    timeout=None,
    heartbeat_interval=None,
    on_heartbeat=None,
) -> dict:
    return _default_kernel.execute(
        code,
        on_stream=on_stream,
        on_input=on_input,
        max_output_length=max_output_length,
        max_display_length=max_display_length,
        timeout=timeout,
        heartbeat_interval=heartbeat_interval,
        on_heartbeat=on_heartbeat,
    )


def execute_json(code: str, timeout=None) -> str:
    return _default_kernel.execute_json(code, timeout=timeout)


def reset(keep_imports: bool = False):
    _default_kernel.reset(keep_imports=keep_imports)


def main(argv=None) -> int:
    """CLI entry point (`py-runner`): code string in, JSON out."""
    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser(
        prog="py-runner",
        description="Execute Python code and print simplified JSON result.",
    )
    parser.add_argument("file", nargs="?", help="Python file to run (default: stdin)")
    parser.add_argument(
        "-c", "--code", help="Code string to execute (overrides file/stdin)"
    )
    parser.add_argument(
        "--timeout", type=float, default=None, help="Max seconds per run"
    )
    parser.add_argument(
        "--no-shell", action="store_true", help="Disable !cmd shell escapes"
    )
    parser.add_argument("--no-pip", action="store_true", help="Disable %pip")
    args = parser.parse_args(argv)

    if args.code is not None:
        code = args.code
    elif args.file is not None:
        with open(args.file, "r") as f:
            code = f.read()
    elif not sys.stdin.isatty():
        code = sys.stdin.read()
    else:
        parser.print_help()
        return 2

    kernel = Kernel(allow_shell=not args.no_shell, allow_pip=not args.no_pip)
    result = kernel.execute(code, timeout=args.timeout)
    sys.stdout.write(json.dumps(result, indent=2))
    sys.stdout.write("\n")
    return 0 if not result.get("error") else 1


if __name__ == "__main__":
    raise SystemExit(main())

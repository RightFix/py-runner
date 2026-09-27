"""Stdout replacement that streams chunks via callback. Standard library only."""

import io
import re
from typing import Callable, Optional

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _render_terminal(raw: str) -> str:
    """Emulate terminal \r handling + strip ANSI codes.

    tqdm and training loops rewrite the current line with '\\r'.
    Keeping every intermediate bar would flood `output`, so the
    final value keeps only the text after the last '\\r' per line.
    Live `on_chunk` still receives the raw chunks for responsiveness.
    """
    out_lines = []
    for line in raw.split("\n"):
        line = _ANSI_RE.sub("", line)
        if "\r" in line:
            line = line.rsplit("\r", 1)[-1]
        out_lines.append(line)
    return "\n".join(out_lines)


class StreamingBuffer(io.StringIO):
    """
    A stdout replacement that calls a callback on each write,
    enabling streaming output for long-running cells.
    """
    def __init__(self, on_chunk: Optional[Callable[[str], None]]):
        super().__init__()
        self._on_chunk = on_chunk
        self._buf = []

    def write(self, s: str):
        self._buf.append(s)
        if self._on_chunk:
            try:
                self._on_chunk(s)
            except Exception:
                pass
        return len(s)

    def getvalue(self, clean: bool = True):
        raw = "".join(self._buf)
        return _render_terminal(raw) if clean else raw

    def raw(self) -> str:
        return "".join(self._buf)


# Backwards-compat alias (old private name).
_StreamingBuffer = StreamingBuffer


class ThreadRoutedProxy:
    """A sys.stdout/stderr stand-in routing worker output by thread.

    The cell runs in a worker thread while the caller waits. A plain
    redirect would hijack output globally, swallowing prints from other
    threads into the cell. This proxy routes only the owner's writes to
    the cell buffer; every other thread falls through to the previous
    stream. Nesting chains via fallback. Standard library only.
    """

    def __init__(self, fallback):
        import threading

        self._threading = threading
        self._fallback = fallback
        self._routes = {}

    def route(self, buffer) -> int:
        ident = self._threading.get_ident()
        self._routes[ident] = buffer
        return ident

    def unroute(self, ident=None) -> None:
        ident = ident if ident is not None else self._threading.get_ident()
        self._routes.pop(ident, None)

    def write(self, s):
        buf = self._routes.get(self._threading.get_ident())
        if buf is not None:
            return buf.write(s)
        return self._fallback.write(s)

    def writelines(self, lines):
        for line in lines:
            self.write(line)

    def flush(self):
        buf = self._routes.get(self._threading.get_ident())
        target = buf if buf is not None else self._fallback
        try:
            return target.flush()
        except Exception:
            return None

    def isatty(self):
        return False

    @property
    def encoding(self):
        return getattr(self._fallback, "encoding", "utf-8")

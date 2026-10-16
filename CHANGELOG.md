# Changelog

## [0.1.3]

- Notebook GUI: `py-runner lab` serves a JupyterLab-like editor (vanilla
  HTML/CSS/JS, offline) + kernel API in one localhost process. File browser
  jailed to `--dir` with subdirectories, recursive search, and save;
  per-notebook isolated sessions; kernel pill shows the active venv `prefix`.
  Frontend split by function (`js/api|markdown|outputs|state|session|files|
  cells|app`, `css/`).
- CodeMirror cell editors (bundled at build time, no runtime deps) with
  auto-close brackets, `Ctrl+Enter` to run, visible caret, focus retention
  across re-renders.
- LSP integration: `/lsp` WebSocket bridge to one shared `pylsp` process,
  per-cell documents, completions on typing, hover docs, lint gutter with
  pushed diagnostics, graceful plain-editor fallback. `python-lsp-server`
  is now a (floor-pinned) hard dependency; `/api/lsp-status` reports
  availability and the workspace root.
- Dual-stack localhost: IPv4 + IPv6 loopback sockets so `localhost` and
  `127.0.0.1` URLs both work; `Cache-Control: no-cache` on GUI assets;
  query strings no longer break API routing.
- Worker stdio loop hardened to pure `readline()` (iterator+readline mixing
  could stall input replies).
- Django-style request logs (`[day/Mon/year hh:mm:ss] "METHOD path" status`)
  with slow-execute durations and TTY-only colors; startup banners report
  version, dir, LSP state, and URL.
- `/health` reports the package `version` (no more hardcoded banner);
  PyPI metadata gains Homepage/Repository/Issues links.
- Subprocess-per-session isolation reverted: sessions run in-process again
  (`process.py`/`worker.py` kept dormant); same for its test suite.

## [0.1.2]

- HTTP server: CORS preflight (`OPTIONS → 204`) + `Access-Control-Allow-Origin: *` so WebView frontends (Acode) can fetch it.

## [0.1.1]

- HTTP server (`py-runner-serve`): session registry with server-generated UUIDs, execute/interrupt/stats/list/delete, `--max-sessions` cap, no idle timeout.
- Kernel: worker-thread execution with `timeout=`, cooperative `interrupt()`, `heartbeat_interval`/`on_heartbeat`, thread-routed stdout/stderr, logging + warnings capture, tqdm `\\r` collapsing, ANSI stripping.
- Kernel: `input()` via `on_input` callbacks; `ast`-based last-expression eval; trailing `;` suppression; display caps + tensor summaries.
- Magics: `%pip`, `!shell` (both flaggable via `allow_pip`/`allow_shell`).
- Ops: bounded `Out[N]` history (`max_history`), `reset(keep_imports=…)`, `clear_history()`, `stats()`, `set_address_space_limit()` (Unix).
- Distribution renamed to `py-runner-kernel` (PyPI `py-runner` collides with `pyrunner`); flat layout; hatchling backend.
- Licensing: AGPL-3.0-or-later, `LICENSE` shipped in both artifacts.

## [0.1.0]

- Initial release as `py-runner`: persistent `Kernel` with `execute`/`execute_json`, magics (`%time`, `%timeit`, `%who`, `%run`, `%matplotlib`, `%reset`), matplotlib/PIL/cv2/pandas/numpy display capture, `py-runner` CLI.

# Changelog

## [0.1.3]

- Notebook GUI: `py-runner lab` serves a JupyterLab-like editor (vanilla
  HTML/CSS/JS, offline) + kernel API in one localhost process. File browser
  jailed to `--dir`; per-notebook isolated sessions; kernel pill shows the
  active venv `prefix`. Frontend split by function (`js/api|markdown|outputs|
  state|session|files|cells|app`, `css/`).
- Worker stdio loop hardened to pure `readline()` (iterator+readline mixing
  could stall input replies).

## [Unreleased]

- `/health` reports the package `version`; server banner no longer hardcodes `0.1.0`.
- PyPI metadata: Homepage/Repository/Issues links.

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

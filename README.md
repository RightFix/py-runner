# py-runner

[![License: AGPL-3.0-or-later](https://img.shields.io/badge/License-AGPL--3.0--or--later-blue.svg)](LICENSE)
[![Python >=3.10](https://img.shields.io/badge/python-%3E%3D3.10-blue.svg)](pyproject.toml)
[![TestPyPI](https://img.shields.io/badge/TestPyPI-py--runner-green.svg)](https://test.pypi.org/project/py-runner/)

Persistent Python code runner: **string in, JSON out**. A lightweight,
dependency-free, ipykernel-like execution library — no Jupyter server, no ZMQ,
no required third-party packages.

```python
from py_runner import Kernel

k = Kernel()
k.execute("x = 40 + 2")
print(k.execute("x + 1"))
# {"output": null,
#  "display": [{"type": "text/plain", "data": "43"}],
#  "error": null, "execution_count": 2, "execution_time": 0.0001}
```

## Install

```bash
pip install py-runner-kernel
# Opt-in stacks (core stays dependency-free):
pip install "py-runner-kernel[data]"   # pandas, numpy, matplotlib, pillow, tqdm
pip install "py-runner-kernel[ml]"     # scikit-learn, torch
pip install "py-runner-kernel[full]"   # everything incl. opencv, plotly
```

Requires Python >= 3.10.

## Quickstart

```python
from py_runner import Kernel, execute, execute_json

# Module-level shared kernel
execute("import math")
print(execute_json("math.sqrt(16)"))

# Isolated session kernel (one per user/request)
k = Kernel(on_input=lambda prompt: "bob", max_history=100)
r = k.execute('name = input("who? ")\nname.upper()')
assert r["display"][0]["data"] == "'BOB'"
```

Each result is a JSON-serializable dict:

| key | value |
|---|---|
| `output` | captured stdout (`str \| None`) |
| `display` | rich values (`[{type, data}]`: `text/plain`, `text/html`, `image/png` base64, …) |
| `error` | traceback / warning text (`str \| None`) |
| `execution_count` | incrementing cell number |
| `execution_time` | seconds (float) |

## Features

- **Persistent namespace** with Jupyter-style `_`, `__`, `___` and bounded `Out[N]` history
- **Last-expression display** — DataFrames as HTML, arrays/tensors summarized, matplotlib figures as PNG, trailing `;` suppresses output
- **Magics**: `%time`, `%timeit`, `%who`/`%whos`, `%run`, `%pip`, `!shell`, `%reset`
- **Long runs**: `timeout=`, cooperative `interrupt()`, `on_stream` chunks, `on_heartbeat` for silent `fit()` loops, tqdm `\r` collapsing, `logging`/`warnings` capture
- **Sandboxing**: `allow_shell=False`, `allow_pip=False`, `set_address_space_limit(mb)` (Unix), `reset(keep_imports=True)`
- **CLI**: `py-runner -c "1+1"`, `py-runner script.py`, `... | py-runner`, with `--timeout`, `--no-shell`, `--no-pip`

## CLI

```bash
py-runner -c "print(2 + 3)"
py-runner analysis.py --timeout 300 > result.json
cat cell.py | py-runner
python -m py_runner -c "1+1"
```

## Project structure

```text
py_runner/
├── __init__.py   # public API: Kernel, execute, execute_json, reset, main
├── kernel.py     # persistent execution kernel (threaded, timeouts, input)
├── magics.py     # %time, %pip, !shell, … (sandbox-flaggable)
├── display.py    # rich display collector (pandas/numpy/PIL/matplotlib)
├── streams.py    # thread-routed streaming stdout, tqdm \r handling
└── utils.py      # guarded imports, address-space limit helper
```

## Development

```bash
uv sync            # create .venv
uv run python -c "from py_runner import execute; print(execute('1+1'))"
uv build           # wheel + sdist in dist/
uv publish --publish-url https://test.pypi.org/legacy/  # trial run
```

## License

AGPL-3.0-or-later — see [LICENSE](LICENSE). Note the network clause
(§13): if you serve a modified version over a network, you must offer its
source to those users.

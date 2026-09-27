# py-runner

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
pip install py-runner
# Opt-in stacks (core stays dependency-free):
pip install "py-runner[data]"   # pandas, numpy, matplotlib, pillow, tqdm
pip install "py-runner[ml]"     # scikit-learn, torch
pip install "py-runner[full]"   # everything incl. opencv, plotly
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

## License

AGPL-3.0-or-later — see [LICENSE](LICENSE). Note the network clause
(§13): if you serve a modified version over a network, you must offer its
source to those users.

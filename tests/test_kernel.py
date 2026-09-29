"""Kernel semantics: execution, state, display, input, limits, magics."""

import threading
import time

from py_runner import Kernel, execute, execute_json, reset


def test_last_expression_display():
    k = Kernel()
    r = k.execute("40 + 2")
    assert r["error"] is None
    assert r["display"] == [{"type": "text/plain", "data": "42"}]
    assert r["execution_count"] == 1


def test_state_persists_across_cells():
    k = Kernel()
    k.execute("x = 40")
    r = k.execute("x + 2")
    assert r["display"][0]["data"] == "42"


def test_multiline_and_compound_statements():
    k = Kernel()
    assert k.execute("def f():\n return 7\nf()")["display"][0]["data"] == "7"
    assert k.execute("for i in range(3):\n x = i\nx")["display"][0]["data"] == "2"
    assert k.execute("with open('/dev/null') as f:\n y = 1\ny")["display"][0]["data"] == "1"


def test_trailing_semicolon_suppresses_display():
    k = Kernel()
    assert k.execute("x = 5;")["display"] == []


def test_print_goes_to_output():
    k = Kernel()
    r = k.execute("print('hi')")
    assert r["output"] == "hi"
    assert r["display"] == []


def test_error_captured_as_traceback():
    k = Kernel()
    r = k.execute("1/0")
    assert r["error"] and "ZeroDivisionError" in r["error"]
    assert r["display"] == []


def test_empty_cells_are_clean():
    k = Kernel()
    for code in ("", "   "):
        r = k.execute(code)
        assert r["error"] is None and r["display"] == []


def test_underscore_history():
    k = Kernel()
    k.execute("10")
    k.execute("20")
    r = k.execute("_ + __")
    assert r["display"][0]["data"] == "30"


def test_input_requires_callback():
    k = Kernel()
    r = k.execute("input('name? ')")
    assert "EOFError" in (r["error"] or "")
    assert r["output"] == "name?"  # prompt echoed to stdout


def test_input_with_callback():
    k = Kernel(on_input=lambda prompt: "bob")
    r = k.execute("name = input('Enter: ')\nname.upper()")
    assert r["error"] is None
    assert r["display"][0]["data"] == "'BOB'"
    assert r["output"] == "Enter:"


def test_display_truncation():
    k = Kernel()
    r = k.execute("list(range(100000))", max_display_length=500)
    assert len(r["display"][0]["data"]) < 2000
    assert "truncated" in r["display"][0]["data"]


def test_stdout_truncation():
    k = Kernel()
    r = k.execute("print('x' * 20000)", max_output_length=100)
    assert "truncated" in (r["output"] or "")


def test_timeout_interrupts_pure_python_loop():
    k = Kernel()
    t0 = time.perf_counter()
    r = k.execute("while True:\n pass", timeout=0.5)
    dt = time.perf_counter() - t0
    assert "TimeoutError" in (r["error"] or "")
    assert dt < 5


def test_interrupt_from_another_thread():
    k = Kernel()

    def interrupter():
        time.sleep(0.3)
        k.interrupt()

    threading.Thread(target=interrupter, daemon=True).start()
    t0 = time.perf_counter()
    r = k.execute("i = 0\nwhile True:\n i += 1")
    assert "KeyboardInterrupt" in (r["error"] or "")
    assert time.perf_counter() - t0 < 5


def test_busy_kernel_rejects_second_cell():
    k = Kernel()
    done = threading.Event()

    def run():
        k.execute("import time\ntime.sleep(1.0)")
        done.set()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    time.sleep(0.2)
    r2 = k.execute("1+1")
    assert "KernelBusy" in (r2["error"] or "")
    assert done.wait(timeout=5)
    t.join(timeout=5)


def test_magics():
    k = Kernel()
    assert k.execute("%who")["output"] == "No variables defined."
    k.execute("a = 1")
    assert k.execute("%who")["output"] == "a"
    assert k.execute("%reset")["output"] == "Kernel reset."
    assert k.execute("%who")["output"] == "No variables defined."
    assert k.execute("!echo hi")["output"] == "hi"
    assert "Usage" in (k.execute("%pip")["output"] or "")


def test_shell_and_pip_gating():
    k = Kernel(allow_shell=False, allow_pip=False)
    assert "disabled" in (k.execute("!echo hi")["error"] or "")
    assert "disabled" in (k.execute("%pip install x")["error"] or "")


def test_history_eviction():
    k = Kernel(max_history=3)
    for i in range(5):
        k.execute(f"{i} * 10")
    assert len(k._out_history) == 3
    assert k._namespace["_"] == 40
    assert k._namespace["__"] == 30
    assert k._namespace["___"] == 20


def test_reset_keep_imports():
    k = Kernel()
    k.execute("import math\nx = 1")
    k.reset(keep_imports=True)
    assert "math" in k._namespace and "x" not in k._namespace
    k.reset()
    assert "math" not in k._namespace


def test_clear_history_and_stats():
    k = Kernel()
    k.execute("1")
    k.clear_history()
    assert k._out_history == {} and "_" not in k._namespace
    s = k.stats()
    assert s["history_len"] == 0 and s["execution_count"] == 1
    assert s["is_busy"] is False


def test_sessions_are_isolated():
    a, b = Kernel(), Kernel()
    a.execute("v = 'A'")
    b.execute("v = 'B'")
    assert a.execute("v")["display"][0]["data"] == "'A'"
    assert b.execute("v")["display"][0]["data"] == "'B'"


def test_module_level_api():
    reset()
    assert execute("2 + 2")["display"][0]["data"] == "4"
    assert '"data": "4"' in execute_json("2 + 2")
    reset()


def test_streaming_and_cr_collapse():
    k = Kernel()
    chunks = []
    code = "import sys\nsys.stdout.write('10%' + chr(13) + '100%' + chr(10))"
    r = k.execute(code, on_stream=chunks.append)
    assert r["output"] == "100%"
    assert len(chunks) >= 1


def test_logging_and_warnings_captured():
    k = Kernel()
    r = k.execute('import logging, warnings\nlogging.warning("w-train")\nwarnings.warn("dep")')
    assert "w-train" in (r["output"] or "")
    assert "UserWarning: dep" in (r["error"] or "")

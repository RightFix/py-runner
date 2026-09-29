"""Process isolation, one shared interpreter. Stdlib only.

One module-scoped ProcessKernel serves all non-destructive tests (a single
child interpreter for the whole file). Tests that kill their kernel
(crash, C-level timeout) get a dedicated instance they close themselves.
faulthandler dumps tracebacks on hang instead of burning RAM silently.
"""

import faulthandler
import time

import pytest

from py_runner import ProcessKernel

faulthandler.dump_traceback_later(120, exit=True)


@pytest.fixture(scope="module")
def k():
    kernel = ProcessKernel()
    yield kernel
    kernel.close()


def test_basic_execute_and_state(k):
    k.execute("x = 40 + 2")
    r = k.execute("x + 1")
    assert r["error"] is None
    assert r["display"] == [{"type": "text/plain", "data": "43"}]


def test_magics_and_gating():
    k = ProcessKernel(allow_shell=False, allow_pip=False)
    try:
        assert "disabled" in (k.execute("!echo hi")["error"] or "")
        assert k.execute("%who")["output"] == "No variables defined."
        k.execute("a = 1")
        assert k.execute("%who")["output"] == "a"
    finally:
        k.close()


def test_input_roundtrip_across_pipe():
    k = ProcessKernel(on_input=lambda prompt: "bob")
    try:
        r = k.execute("name = input('who? ')\nname.upper()")
        assert r["error"] is None, r
        assert r["display"] == [{"type": "text/plain", "data": "'BOB'"}]
        assert r["output"] == "who?"
    finally:
        k.close()


def test_input_without_callback_errors(k):
    r = k.execute("input('x')")
    assert "EOFError" in (r["error"] or ""), r


def test_streaming_across_boundary(k):
    chunks = []
    r = k.execute("print('hello')", on_stream=chunks.append)
    assert r["output"] == "hello"
    assert any("hello" in c for c in chunks), chunks


def test_stats_reports_process_info(k):
    s = k.stats()
    assert s["crashed"] is False
    assert s["pid"] and s["pid"] > 0
    assert s["executable"] and s["prefix"]
    assert s["restarts"] == 0


def test_reset_and_namespace(k):
    k.execute("import math\nz = 5")
    k.reset(keep_imports=True)
    r = k.execute("math.floor(2.7)")
    assert r["error"] is None
    assert r["display"] == [{"type": "text/plain", "data": "2"}]


def test_sessions_do_not_share_state():
    a, b = ProcessKernel(), ProcessKernel()
    try:
        a.execute("v = 'A'")
        r = b.execute("v")
        assert r["error"] is not None  # NameError in b
        assert a.execute("v")["display"][0]["data"] == "'A'"
    finally:
        a.close()
        b.close()


def test_child_crash_respawns():
    k = ProcessKernel()
    try:
        r = k.execute("import os; os._exit(3)")
        assert "SessionCrashed" in (r["error"] or ""), r
        r2 = k.execute("6 * 7")
        assert r2["error"] is None
        assert r2["display"] == [{"type": "text/plain", "data": "42"}]
        assert k.stats()["restarts"] >= 1
    finally:
        k.close()


def test_c_level_sleep_timeout_returns():
    k = ProcessKernel()
    try:
        t0 = time.perf_counter()
        r = k.execute("import time; time.sleep(30)", timeout=2.0)
        dt = time.perf_counter() - t0
        assert "TimeoutError" in (r["error"] or ""), r
        assert dt < 10, dt  # SIGINT fails on sleep; SIGKILL must land
    finally:
        k.close()


def test_interrupt_stops_python_loop():
    import threading

    k = ProcessKernel()
    try:

        def interrupter():
            time.sleep(0.5)
            k.interrupt()

        threading.Thread(target=interrupter, daemon=True).start()
        t0 = time.perf_counter()
        r = k.execute("i = 0\nwhile True:\n i += 1")
        assert "KeyboardInterrupt" in (r["error"] or ""), r
        assert time.perf_counter() - t0 < 8
    finally:
        k.close()

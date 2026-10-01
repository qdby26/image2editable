"""Deterministic timeout/ownership tests for the embed worker subprocess.

No live COM: ``font_embed._embed_worker_command`` is patched to launch
``sys.executable -u -c`` fake workers that write the same status/result
protocol then block. All fault workers are this test's own processes.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import psutil
import pytest

from scripts import font_embed

# Public embed_fonts is a Windows-only supervisor; the fault workers below
# are this test's own processes and never touch real COM/PowerPoint.
pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="font embedding is Windows-only"
)

_TEST_SRC = "a.pptx"


@pytest.fixture
def fast_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IMAGE2EDITABLE_FONT_ACTIVATION_TIMEOUT", "0.4")
    monkeypatch.setenv("IMAGE2EDITABLE_FONT_EMBED_TIMEOUT", "0.4")
    monkeypatch.setattr(
        font_embed, "_CLEANUP_TIMEOUT_S", 0.4, raising=False
    )
    # Preflight seams: pretend a usable PowerPoint exists and none is running.
    monkeypatch.setattr(
        font_embed, "_powerpoint_exe_path", lambda: Path(sys.executable)
    )
    monkeypatch.setattr(font_embed, "_powerpoint_running", lambda: False)


def _factory(code: str, marker: Path | None = None, extra: str = ""):
    """Return an _embed_worker_command factory for a -c fake worker."""
    def factory(src, dst, status_path, result_path):
        argv = [
            sys.executable, "-u", "-c", textwrap.dedent(code),
            str(status_path), str(result_path), str(dst),
        ]
        if marker is not None:
            argv.append(str(marker))
        if extra:
            argv.append(extra)
        return argv
    return factory


def _patch_worker(
    monkeypatch: pytest.MonkeyPatch, code: str, **kwargs
) -> None:
    monkeypatch.setattr(
        font_embed, "_embed_worker_command", _factory(code, **kwargs)
    )


_WRITE_STATUS = (
    "def status(phase=None, attempt=1, owned=None, protected=None):\n"
    "    doc = {\n"
    "        'phase': phase, 'attempt': attempt,\n"
    "        'owned': owned or [], 'protected': protected or [],\n"
    "        'worker_pid': os.getpid(),\n"
    "    }\n"
    "    for _ in range(10):\n"
    "        try:\n"
    "            tmp = STATUS + '.tmp'\n"
    "            open(tmp, 'w').write(json.dumps(doc))\n"
    "            os.replace(tmp, STATUS)\n"
    "            break\n"
    "        except OSError:\n"
    "            time.sleep(0.02)\n"
    "open(STATUS + '.pid', 'w').write(str(os.getpid()))\n"
)

_PREAMBLE = (
    "import sys, time, json, os\n"
    "STATUS, RESULT, DST = sys.argv[1], sys.argv[2], sys.argv[3]\n"
    "MARKER = sys.argv[4] if len(sys.argv) > 4 else STATUS\n"
    "EXTRA = sys.argv[5] if len(sys.argv) > 5 else ''\n"
    + _WRITE_STATUS.replace("STATUS + '.pid'", "MARKER + '.pid'")
)


def _deadline_then_sleep(status_calls: str, sleep_s: float = 60) -> str:
    return _PREAMBLE + status_calls + f"\ntime.sleep({sleep_s})\n"


def _pid_from_marker(path: Path) -> int:
    return int(path.read_text(encoding="ascii").strip())


def test_activation_startup_timeout_kills_silent_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fast_timeouts
) -> None:
    _patch_worker(monkeypatch, _PREAMBLE + "time.sleep(60)\n")
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="activation"):
        font_embed.embed_fonts(tmp_path / _TEST_SRC, tmp_path / "b.pptx")
    assert time.monotonic() - started < 10
    # Silent worker never wrote its pid marker file; pid comes from status
    # only, so just verify the destination was never produced.
    assert not (tmp_path / "b.pptx").exists()


def test_activation_phase_hang(tmp_path, monkeypatch, fast_timeouts) -> None:
    _patch_worker(
        monkeypatch,
        _deadline_then_sleep('status("activation", 1)\n'),
    )
    with pytest.raises(RuntimeError, match="activation"):
        font_embed.embed_fonts(tmp_path / _TEST_SRC, tmp_path / "b.pptx")


def test_embedding_phase_hang(tmp_path, monkeypatch, fast_timeouts) -> None:
    _patch_worker(
        monkeypatch,
        _deadline_then_sleep(
            'status("activation", 1)\nstatus("embedding", 1)\n'
        ),
    )
    with pytest.raises(RuntimeError, match="embedding"):
        font_embed.embed_fonts(tmp_path / _TEST_SRC, tmp_path / "b.pptx")


def test_cleanup_phase_hang(tmp_path, monkeypatch, fast_timeouts) -> None:
    _patch_worker(
        monkeypatch,
        _deadline_then_sleep(
            'status("activation", 1)\nstatus("embedding", 1)\n'
            'status("cleanup", 1)\n'
        ),
    )
    with pytest.raises(RuntimeError, match="cleanup"):
        font_embed.embed_fonts(tmp_path / _TEST_SRC, tmp_path / "b.pptx")


def test_ownership_updates_do_not_renew_deadline(
    tmp_path, monkeypatch, fast_timeouts
) -> None:
    # Same (attempt, phase) rewritten with fresh owned records forever —
    # the activation deadline must still fire.
    code = _PREAMBLE + (
        'n = 0\n'
        'while True:\n'
        '    status("activation", 1, owned=[{"pid": 1, "n": n}])\n'
        '    n += 1\n'
        '    time.sleep(0.02)\n'
    )
    _patch_worker(monkeypatch, code)
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="activation"):
        font_embed.embed_fonts(tmp_path / _TEST_SRC, tmp_path / "b.pptx")
    assert time.monotonic() - started < 10


def test_worker_crash_without_result_rejected(
    tmp_path, monkeypatch, fast_timeouts
) -> None:
    _patch_worker(
        monkeypatch,
        _PREAMBLE + "sys.stderr.write('boom\\n')\nsys.exit(3)\n",
    )
    with pytest.raises(RuntimeError, match="worker"):
        font_embed.embed_fonts(tmp_path / _TEST_SRC, tmp_path / "b.pptx")


def test_worker_malformed_result_rejected(
    tmp_path, monkeypatch, fast_timeouts
) -> None:
    code = (
        _PREAMBLE
        + 'status("activation", 1)\n'
        + 'open(RESULT, "w").write("not json")\n'
    )
    _patch_worker(monkeypatch, code)
    with pytest.raises(RuntimeError):
        font_embed.embed_fonts(tmp_path / _TEST_SRC, tmp_path / "b.pptx")


def test_worker_error_result_raises_original_message(
    tmp_path, monkeypatch, fast_timeouts
) -> None:
    code = (
        _PREAMBLE
        + 'status("activation", 1)\n'
        + 'open(RESULT, "w").write(json.dumps('
        + '{"ok": False, "error": "real Microsoft PowerPoint unavailable: x"}'
        + "))\n"
    )
    _patch_worker(monkeypatch, code)
    with pytest.raises(
        RuntimeError, match="real Microsoft PowerPoint unavailable"
    ):
        font_embed.embed_fonts(tmp_path / _TEST_SRC, tmp_path / "b.pptx")


def test_worker_success_result_passthrough(
    tmp_path, monkeypatch, fast_timeouts
) -> None:
    code = (
        _PREAMBLE
        + 'status("activation", 1)\nstatus("embedding", 1)\n'
        + 'status("cleanup", 1)\n'
        + 'open(RESULT, "w").write(json.dumps('
        + '{"ok": True, "result": {"portable": True, "marker": "ok"}}'
        + "))\n"
    )
    _patch_worker(monkeypatch, code)
    result = font_embed.embed_fonts(tmp_path / _TEST_SRC, tmp_path / "b.pptx")
    assert result == {"portable": True, "marker": "ok"}


@pytest.mark.parametrize(
    "env",
    [
        "IMAGE2EDITABLE_FONT_ACTIVATION_TIMEOUT",
        "IMAGE2EDITABLE_FONT_EMBED_TIMEOUT",
    ],
)
@pytest.mark.parametrize("value", ["nan", "inf", "0", "-5", "abc"])
def test_timeout_env_rejected_before_spawn(
    env, value, tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("IMAGE2EDITABLE_FONT_ACTIVATION_TIMEOUT", "0.4")
    monkeypatch.setenv("IMAGE2EDITABLE_FONT_EMBED_TIMEOUT", "0.4")
    monkeypatch.setenv(env, value)
    monkeypatch.setattr(
        font_embed, "_powerpoint_exe_path", lambda: Path(sys.executable)
    )
    monkeypatch.setattr(font_embed, "_powerpoint_running", lambda: False)
    def explode(*args, **kwargs):
        raise AssertionError("Popen must not run")
    monkeypatch.setattr(subprocess, "Popen", explode)
    with pytest.raises(ValueError, match=env):
        font_embed.embed_fonts(tmp_path / _TEST_SRC, tmp_path / "b.pptx")


def test_embed_pptx_in_place_hung_worker_preserves_source(
    tmp_path, monkeypatch, fast_timeouts
) -> None:
    from pptx import Presentation

    deck = tmp_path / "deck.pptx"
    Presentation().save(deck)
    original = deck.read_bytes()
    _patch_worker(
        monkeypatch, _deadline_then_sleep('status("embedding", 1)\n')
    )
    payload = font_embed.embed_pptx_in_place(deck)
    assert payload["skipped"] is True
    assert payload["embedded"] is False
    assert "embedding" in payload["reason"]
    assert deck.read_bytes() == original


def _worker_with_owned_child(
    protect_child: bool = False, exit_code: int | None = None
) -> str:
    """Fake worker spawning a real child and publishing it as owned."""
    ending = (
        f"sys.exit({exit_code})\n"
        if exit_code is not None
        else "time.sleep(60)\n"
    )
    return (
        "import sys, time, json, os, subprocess\n"
        "import psutil\n"
        "STATUS, RESULT, DST = sys.argv[1], sys.argv[2], sys.argv[3]\n"
        "MARKER = sys.argv[4]\n"
        "VICTIM = sys.argv[5] if len(sys.argv) > 5 else ''\n"
        + _WRITE_STATUS.replace("STATUS + '.pid'", "MARKER + '.pid'")
        + "child = subprocess.Popen("
        + "[sys.executable, '-c', 'import time;time.sleep(60)'],"
        + " stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        + "open(MARKER + '.child', 'w').write(str(child.pid))\n"
        + "rec = {\n"
        + "  'pid': child.pid,\n"
        + "  'creation_time': psutil.Process(child.pid).create_time(),\n"
        + "  'exe': sys.executable,\n"
        + "  'parent_pid': os.getpid(),\n"
        + "}\n"
        + "owned = [rec]\n"
        + "protected = []\n"
        + ("protected = [child.pid]\n" if protect_child else "")
        + "if VICTIM:\n"
        + "    owned.append({'pid': int(VICTIM), 'creation_time': 1.0,\n"
        + "                  'exe': 'C:/nope/x.exe',\n"
        + "                  'parent_pid': os.getpid()})\n"
        + "status('activation', 1, owned=owned, protected=protected)\n"
        + ending
    )


def test_owned_child_killed_on_timeout(
    tmp_path, monkeypatch, fast_timeouts
) -> None:
    marker = tmp_path / "marker"
    _patch_worker(
        monkeypatch, _worker_with_owned_child(), marker=marker,
    )
    with pytest.raises(RuntimeError, match="activation"):
        font_embed.embed_fonts(tmp_path / _TEST_SRC, tmp_path / "b.pptx")
    child_pid = _pid_from_marker(Path(str(marker) + ".child"))
    worker_pid = _pid_from_marker(Path(str(marker) + ".pid"))
    time.sleep(0.1)
    assert not psutil.pid_exists(child_pid)
    assert not psutil.pid_exists(worker_pid)


def test_foreign_and_protected_processes_survive(
    tmp_path, monkeypatch, fast_timeouts
) -> None:
    # An unrelated live process recorded as "owned" but with a wrong
    # creation_time/exe/parent_pid must never be killed; a protected record
    # likewise.
    innocent = subprocess.Popen(
        [sys.executable, "-c", "import time;time.sleep(60)"]
    )
    marker = tmp_path / "marker"
    child_pid = None
    try:
        _patch_worker(
            monkeypatch,
            _worker_with_owned_child(protect_child=True),
            marker=marker,
            extra=str(innocent.pid),
        )
        with pytest.raises(RuntimeError):
            font_embed.embed_fonts(tmp_path / _TEST_SRC, tmp_path / "b.pptx")
        child_pid = _pid_from_marker(Path(str(marker) + ".child"))
        assert psutil.pid_exists(innocent.pid)
        assert psutil.pid_exists(child_pid)
    finally:
        innocent.kill()
        innocent.wait()
        if child_pid is not None and psutil.pid_exists(child_pid):
            psutil.Process(child_pid).kill()


def test_start_powerpoint_rejects_foreign_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeProc:
        pid = 43210

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

    class FakeApp:
        def __init__(self):
            self.quit_called = False

        @property
        def Path(self):  # noqa: N802 - COM-style attribute
            return str(Path(sys.executable).parent)

        def Quit(self):
            self.quit_called = True

    app = FakeApp()
    monkeypatch.setattr(font_embed, "_com_powerpoint", lambda: app)
    monkeypatch.setattr(font_embed, "_is_real_powerpoint", lambda a: True)
    monkeypatch.setattr(font_embed, "_powerpoint_running", lambda: False)
    monkeypatch.setattr(
        font_embed, "_powerpoint_exe_path", lambda: Path(sys.executable)
    )
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: FakeProc())
    # The COM app's owning thread belongs to a different process.
    monkeypatch.setattr(font_embed, "_app_process_id", lambda a: 9999)
    monkeypatch.setattr(psutil, "Process", lambda pid: type(
        "P", (), {"create_time": lambda self: 1.0}
    )())
    result, proc = font_embed._start_powerpoint(timeout_s=0.3)
    assert result is None and proc is None
    assert app.quit_called is False


def test_start_powerpoint_protects_user_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = type(
        "P", (), {"pid": 43211, "poll": lambda self: None,
                  "terminate": lambda self: None}
    )()

    class FakePresentations:
        Count = 1

    class FakeApp:
        Path = str(Path(sys.executable).parent)
        Presentations = FakePresentations()

        def Quit(self):
            self.quit_called = True

    app = FakeApp()
    published = []
    monkeypatch.setattr(font_embed, "_com_powerpoint", lambda: app)
    monkeypatch.setattr(font_embed, "_is_real_powerpoint", lambda a: True)
    monkeypatch.setattr(font_embed, "_powerpoint_running", lambda: False)
    monkeypatch.setattr(
        font_embed, "_powerpoint_exe_path", lambda: Path(sys.executable)
    )
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: proc)
    monkeypatch.setattr(font_embed, "_app_process_id", lambda a: 43211)
    monkeypatch.setattr(psutil, "Process", lambda pid: type(
        "P", (), {"create_time": lambda self: 1.0}
    )())
    result, got_proc = font_embed._start_powerpoint(
        timeout_s=0.5,
        publish=lambda event, payload: published.append((event, payload)),
    )
    assert result is None and got_proc is None
    assert getattr(app, "quit_called", False) is False
    assert ("protect", 43211) in published


def test_start_powerpoint_returns_matched_empty_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = type(
        "P", (), {"pid": 43212, "poll": lambda self: None,
                  "terminate": lambda self: None}
    )()

    class FakePresentations:
        Count = 0

    class FakeApp:
        Path = str(Path(sys.executable).parent)
        Presentations = FakePresentations()

    app = FakeApp()
    published = []
    monkeypatch.setattr(font_embed, "_com_powerpoint", lambda: app)
    monkeypatch.setattr(font_embed, "_is_real_powerpoint", lambda a: True)
    monkeypatch.setattr(font_embed, "_powerpoint_running", lambda: False)
    monkeypatch.setattr(
        font_embed, "_powerpoint_exe_path", lambda: Path(sys.executable)
    )
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: proc)
    monkeypatch.setattr(font_embed, "_app_process_id", lambda a: 43212)
    monkeypatch.setattr(psutil, "Process", lambda pid: type(
        "P", (), {"create_time": lambda self: 1.0}
    )())
    result, got_proc = font_embed._start_powerpoint(
        timeout_s=0.5,
        publish=lambda event, payload: published.append((event, payload)),
    )
    assert result is app and got_proc is proc
    assert ("own", {
        "pid": 43212, "creation_time": 1.0,
        "exe": str(Path(sys.executable).resolve()), "parent_pid": os.getpid(),
    }) in published


def test_delayed_first_activation_status_shares_startup_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fast_timeouts
) -> None:
    # The first ('activation', 1) status must NOT start a fresh budget —
    # startup and attempt-1 activation share one deadline. Budget 0.5s,
    # status arrives at ~0.25s: a renewing implementation would fire at
    # ~0.75s+; a correct one at ~0.5s.
    code = (
        _PREAMBLE
        + "time.sleep(0.25)\nstatus('activation', 1)\ntime.sleep(60)\n"
    )
    _patch_worker(monkeypatch, code)
    monkeypatch.setenv("IMAGE2EDITABLE_FONT_ACTIVATION_TIMEOUT", "0.5")
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="activation"):
        font_embed.embed_fonts(tmp_path / _TEST_SRC, tmp_path / "b.pptx")
    assert time.monotonic() - started < 0.75


def test_success_result_with_nonzero_exit_rejected(
    tmp_path, monkeypatch, fast_timeouts
) -> None:
    code = (
        _PREAMBLE
        + 'status("activation", 1)\n'
        + 'open(RESULT, "w").write(json.dumps('
        + '{"ok": True, "result": {"portable": True}}))\n'
        + "sys.exit(3)\n"
    )
    _patch_worker(monkeypatch, code)
    with pytest.raises(RuntimeError):
        font_embed.embed_fonts(tmp_path / _TEST_SRC, tmp_path / "b.pptx")


def test_crash_cleans_recorded_owned_child(
    tmp_path, monkeypatch, fast_timeouts
) -> None:
    marker = tmp_path / "marker"
    _patch_worker(
        monkeypatch,
        _worker_with_owned_child(exit_code=3),
        marker=marker,
    )
    with pytest.raises(RuntimeError):
        font_embed.embed_fonts(tmp_path / _TEST_SRC, tmp_path / "b.pptx")
    child_pid = _pid_from_marker(Path(str(marker) + ".child"))
    assert not psutil.pid_exists(child_pid)


def test_worker_state_in_error_result_used_for_cleanup(
    tmp_path, monkeypatch, fast_timeouts
) -> None:
    # Result-document worker_state carries the latest owned set even when
    # the status file never recorded it; the parent must clean it.
    code = (
        "import sys, time, json, os, subprocess\nimport psutil\n"
        "STATUS, RESULT, DST = sys.argv[1], sys.argv[2], sys.argv[3]\n"
        "MARKER = sys.argv[4]\n"
        "child = subprocess.Popen("
        "[sys.executable, '-c', 'import time;time.sleep(60)'],"
        " stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "open(MARKER + '.child', 'w').write(str(child.pid))\n"
        "rec = {'pid': child.pid, 'creation_time': "
        "psutil.Process(child.pid).create_time(), 'exe': sys.executable, "
        "'parent_pid': os.getpid()}\n"
        "open(RESULT, 'w').write(json.dumps({'ok': False, 'error': 'boom',"
        " 'worker_state': {'owned': [rec], 'protected': []}}))\n"
        "sys.exit(1)\n"
    )
    marker = tmp_path / "marker"
    _patch_worker(monkeypatch, code, marker=marker)
    with pytest.raises(RuntimeError, match="boom"):
        font_embed.embed_fonts(tmp_path / _TEST_SRC, tmp_path / "b.pptx")
    child_pid = _pid_from_marker(Path(str(marker) + ".child"))
    assert not psutil.pid_exists(child_pid)


def test_worker_status_flush_raises_on_persistent_failure(
    tmp_path: Path,
) -> None:
    status = font_embed._WorkerStatus(tmp_path / "missing" / "s.json")
    with pytest.raises(OSError):
        status.publish("phase", {"phase": "activation", "attempt": 1})


def _fake_proc(pid: int):
    return type(
        "P",
        (),
        {
            "pid": pid,
            "terminated": False,
            "poll": lambda self: None,
            "wait": lambda self, timeout=None: None,
            "terminate": lambda self: setattr(self, "terminated", True),
        },
    )()


def test_start_powerpoint_own_publish_failure_terminates_spawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = _fake_proc(43220)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: proc)
    monkeypatch.setattr(
        font_embed, "_powerpoint_exe_path", lambda: Path(sys.executable)
    )
    monkeypatch.setattr(font_embed, "_powerpoint_running", lambda: False)
    monkeypatch.setattr(psutil, "Process", lambda pid: type(
        "P", (), {"create_time": lambda self: 1.0}
    )())

    def publish(kind, payload):
        raise OSError("status dir gone")

    with pytest.raises(OSError):
        font_embed._start_powerpoint(timeout_s=0.2, publish=publish)
    assert proc.terminated is True


def test_start_powerpoint_protect_publish_failure_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = _fake_proc(43221)

    class FakePresentations:
        Count = 1

    class FakeApp:
        Path = str(Path(sys.executable).parent)
        Presentations = FakePresentations()
        quit_called = False

        def Quit(self):
            self.quit_called = True

    app = FakeApp()
    monkeypatch.setattr(font_embed, "_com_powerpoint", lambda: app)
    monkeypatch.setattr(font_embed, "_is_real_powerpoint", lambda a: True)
    monkeypatch.setattr(font_embed, "_powerpoint_running", lambda: False)
    monkeypatch.setattr(
        font_embed, "_powerpoint_exe_path", lambda: Path(sys.executable)
    )
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: proc)
    monkeypatch.setattr(font_embed, "_app_process_id", lambda a: 43221)
    monkeypatch.setattr(psutil, "Process", lambda pid: type(
        "P", (), {"create_time": lambda self: 1.0}
    )())

    def publish(kind, payload):
        if kind == "protect":
            raise OSError("status gone")

    with pytest.raises(OSError):
        font_embed._start_powerpoint(timeout_s=0.5, publish=publish)
    assert app.quit_called is False


def test_start_powerpoint_unreadable_count_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = _fake_proc(43222)

    class _Broken:
        @property
        def Count(self):
            raise RuntimeError("dispatch dead")

    class FakeApp:
        Path = str(Path(sys.executable).parent)
        Presentations = _Broken()
        quit_called = False

        def Quit(self):
            self.quit_called = True

    app = FakeApp()
    published = []
    monkeypatch.setattr(font_embed, "_com_powerpoint", lambda: app)
    monkeypatch.setattr(font_embed, "_is_real_powerpoint", lambda a: True)
    monkeypatch.setattr(font_embed, "_powerpoint_running", lambda: False)
    monkeypatch.setattr(
        font_embed, "_powerpoint_exe_path", lambda: Path(sys.executable)
    )
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: proc)
    monkeypatch.setattr(font_embed, "_app_process_id", lambda a: 43222)
    monkeypatch.setattr(psutil, "Process", lambda pid: type(
        "P", (), {"create_time": lambda self: 1.0}
    )())
    result, got_proc = font_embed._start_powerpoint(
        timeout_s=0.5,
        publish=lambda kind, payload: published.append((kind, payload)),
    )
    assert result is None and got_proc is None
    assert app.quit_called is False
    assert ("protect", 43222) in published


def test_embed_pptx_in_place_invalid_embed_env_skips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pptx import Presentation

    deck = tmp_path / "deck.pptx"
    Presentation().save(deck)
    original = deck.read_bytes()
    monkeypatch.setenv("IMAGE2EDITABLE_FONT_EMBED_TIMEOUT", "abc")
    monkeypatch.setenv("IMAGE2EDITABLE_FONT_ACTIVATION_TIMEOUT", "0.4")
    monkeypatch.setattr(
        font_embed, "_powerpoint_exe_path", lambda: Path(sys.executable)
    )
    monkeypatch.setattr(font_embed, "_powerpoint_running", lambda: False)
    payload = font_embed.embed_pptx_in_place(deck)
    assert payload["skipped"] is True
    assert payload["embedded"] is False
    assert "IMAGE2EDITABLE_FONT_EMBED_TIMEOUT" in payload["reason"]
    assert deck.read_bytes() == original


def test_embed_pptx_in_place_crash_worker_preserves_source(
    tmp_path, monkeypatch, fast_timeouts
) -> None:
    from pptx import Presentation

    deck = tmp_path / "deck.pptx"
    Presentation().save(deck)
    original = deck.read_bytes()
    _patch_worker(
        monkeypatch, _PREAMBLE + "sys.stderr.write('x\\n')\nsys.exit(2)\n"
    )
    payload = font_embed.embed_pptx_in_place(deck)
    assert payload["skipped"] is True
    assert deck.read_bytes() == original


def test_fresh_import_with_invalid_embed_env_succeeds() -> None:
    env = {
        **os.environ,
        "IMAGE2EDITABLE_FONT_EMBED_TIMEOUT": "abc",
        "IMAGE2EDITABLE_FONT_ACTIVATION_TIMEOUT": "nan",
    }
    root = Path(font_embed.__file__).resolve().parents[1]
    subprocess.run(
        [sys.executable, "-c", "import scripts.font_embed"],
        env=env, cwd=root, check=True, capture_output=True, timeout=60,
    )


def test_app_process_id_uses_typed_hwnd_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ctypes
    from ctypes import wintypes

    seen: dict = {}

    def fake(hwnd, pid_ptr):
        seen["hwnd"] = hwnd
        pid_ptr._obj.value = 4321
        return 7

    monkeypatch.setattr(
        ctypes.windll.user32, "GetWindowThreadProcessId", fake
    )
    app = type("A", (), {"HWND": 0x1_0000_00FF})()  # >32-bit handle
    assert font_embed._app_process_id(app) == 4321
    assert isinstance(seen["hwnd"], wintypes.HWND)
    assert seen["hwnd"].value == 0x1_0000_00FF


_CHILD_WORKER_PREFIX = (
    "import sys, time, json, os, subprocess\nimport psutil\n"
    "STATUS, RESULT, DST = sys.argv[1], sys.argv[2], sys.argv[3]\n"
    "MARKER = sys.argv[4]\n"
    + _WRITE_STATUS.replace("STATUS + '.pid'", "MARKER + '.pid'")
    + "child = subprocess.Popen("
    "[sys.executable, '-c', 'import time;time.sleep(60)'],"
    " stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
    "open(MARKER + '.child', 'w').write(str(child.pid))\n"
    "rec = {'pid': child.pid, 'creation_time': "
    "psutil.Process(child.pid).create_time(), 'exe': sys.executable, "
    "'parent_pid': os.getpid()}\n"
)


def test_malformed_phase_value_bounded_and_cleans_child(
    tmp_path, monkeypatch, fast_timeouts
) -> None:
    # A non-string phase must not raise TypeError inside the supervision
    # loop; the startup/activation deadline still bounds the worker and
    # the recorded owned child is cleaned.
    code = (
        _CHILD_WORKER_PREFIX
        + "status(['activation'], 1, owned=[rec])\n"
        + "time.sleep(60)\n"
    )
    marker = tmp_path / "marker"
    _patch_worker(monkeypatch, code, marker=marker)
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="activation"):
        font_embed.embed_fonts(tmp_path / _TEST_SRC, tmp_path / "b.pptx")
    assert time.monotonic() - started < 10
    child_pid = _pid_from_marker(Path(str(marker) + ".child"))
    worker_pid = _pid_from_marker(Path(str(marker) + ".pid"))
    time.sleep(0.1)
    assert not psutil.pid_exists(child_pid)
    assert not psutil.pid_exists(worker_pid)


def test_malformed_owned_protected_collections_still_clean(
    tmp_path, monkeypatch, fast_timeouts
) -> None:
    # Malformed owned/protected values must not abort cleanup; the valid
    # owned record still gets killed and an unrelated process survives.
    innocent = subprocess.Popen(
        [sys.executable, "-c", "import time;time.sleep(60)"]
    )
    code = (
        _CHILD_WORKER_PREFIX
        + "status('activation', 1, owned=[rec], protected=42)\n"
        + "time.sleep(60)\n"
    )
    marker = tmp_path / "marker"
    child_pid = None
    try:
        _patch_worker(monkeypatch, code, marker=marker)
        with pytest.raises(RuntimeError, match="activation"):
            font_embed.embed_fonts(
                tmp_path / _TEST_SRC, tmp_path / "b.pptx"
            )
        child_pid = _pid_from_marker(Path(str(marker) + ".child"))
        assert not psutil.pid_exists(child_pid)
        assert psutil.pid_exists(innocent.pid)
    finally:
        innocent.kill()
        innocent.wait()
        if child_pid is not None and psutil.pid_exists(child_pid):
            psutil.Process(child_pid).kill()


def test_unexpected_supervision_error_cleans_owned_child(
    tmp_path, monkeypatch, fast_timeouts
) -> None:
    # Any unexpected supervision exception must still route through
    # cleanup of the worker AND its recorded owned children.
    code = (
        _CHILD_WORKER_PREFIX
        + "status('activation', 1, owned=[rec])\n"
        + "open(MARKER + '.ready', 'w').write('1')\n"
        + "time.sleep(60)\n"
    )
    marker = tmp_path / "marker"
    child_pid = None
    innocent = subprocess.Popen(
        [sys.executable, "-c", "import time;time.sleep(60)"]
    )
    try:
        _patch_worker(monkeypatch, code, marker=marker)

        def explode(*args, **kwargs):
            # Wait for the worker's status write to land so the finally
            # cleanup has a record to act on.
            deadline = time.monotonic() + 10
            while (
                not Path(str(marker) + ".ready").exists()
                and time.monotonic() < deadline
            ):
                time.sleep(0.05)
            raise KeyError("supervision exploded")

        monkeypatch.setattr(
            font_embed, "_supervise_embed_worker", explode
        )
        with pytest.raises(KeyError, match="supervision exploded"):
            font_embed.embed_fonts(
                tmp_path / _TEST_SRC, tmp_path / "b.pptx"
            )
        child_pid = _pid_from_marker(Path(str(marker) + ".child"))
        worker_pid = _pid_from_marker(Path(str(marker) + ".pid"))
        assert not psutil.pid_exists(child_pid)
        assert not psutil.pid_exists(worker_pid)
        assert psutil.pid_exists(innocent.pid)
    finally:
        innocent.kill()
        innocent.wait()
        if child_pid is not None and psutil.pid_exists(child_pid):
            psutil.Process(child_pid).kill()

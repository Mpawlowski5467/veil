"""The Windows exercise must distinguish denied access from unrelated failures."""

import errno
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location(
    "windows_standard_user_harness",
    Path(__file__).resolve().parents[1] / "benchmarks" / "windows_standard_user.py",
)
harness = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(harness)


@pytest.mark.parametrize(
    ("kind", "number", "winerror", "passes"),
    [
        (PermissionError, errno.EACCES, None, True),  # _wopen / CRT FileIO
        (PermissionError, errno.EACCES, 5, True),  # Native directory APIs
        (
            PermissionError,
            errno.EACCES,
            32,
            False,
        ),  # Sharing conflict is not ACL proof.
        (PermissionError, errno.EPERM, None, False),
        (FileNotFoundError, errno.ENOENT, 2, False),
        (OSError, errno.EIO, None, False),
    ],
)
def test_python_probe_retains_only_denial_codes(kind, number, winerror, passes):
    report = {"access_probes": []}
    error = kind(number, "fictional-private-message", "fictional-private-path")
    error.winerror = winerror

    def attempt():
        raise error

    if passes:
        harness.python_denial_probe(attempt, code="test_operation", report=report)
    else:
        with pytest.raises(harness.CheckError):
            harness.python_denial_probe(attempt, code="test_operation", report=report)
    probe = report["access_probes"][0]
    assert probe["state"] == ("pass" if passes else "fail")
    assert probe["errno"] == number
    assert probe["winerror"] == winerror
    assert "fictional-private" not in json.dumps(report)


def test_python_probe_never_accepts_successful_access():
    report = {"access_probes": []}
    with pytest.raises(harness.CheckError):
        harness.python_denial_probe(lambda: None, code="test_operation", report=report)
    assert report["access_probes"][0]["state"] == "fail"


@pytest.mark.parametrize(
    ("denied", "native_error", "passes"),
    [
        (True, 5, True),
        (True, 0, False),
        (True, 2, False),
        (True, 32, False),
        (False, 0, True),
        (False, 5, False),
    ],
)
def test_native_probe_requires_exact_denial_or_positive_control(
    monkeypatch, denied, native_error, passes
):
    report = {"access_probes": []}
    calls, closed = [], []
    handle = harness.w.HANDLE(-1).value if native_error else 123

    def create(*args):
        calls.append(args)
        return handle

    def close(value):
        closed.append(value)
        return True

    kernel = SimpleNamespace(CreateFileW=create, CloseHandle=close)
    monkeypatch.setattr(harness.ctypes, "WinDLL", lambda *a, **k: kernel, raising=False)
    monkeypatch.setattr(harness.ctypes, "set_last_error", lambda _: None, raising=False)
    monkeypatch.setattr(
        harness.ctypes, "get_last_error", lambda: native_error, raising=False
    )

    def run():
        harness.native_open_probe(
            Path("fictional-private-path"),
            1,
            directory=True,
            denied=denied,
            code="test_directory",
            report=report,
        )

    if passes:
        run()
    else:
        with pytest.raises(harness.CheckError):
            run()
    probe = report["access_probes"][0]
    assert probe == {
        "code": "test_directory",
        "state": "pass" if passes else "fail",
        "winerror": native_error,
    }
    assert calls[0][4:6] == (3, 0x02000000)  # OPEN_EXISTING + directory flag.
    assert closed == ([] if native_error else [123])
    assert "fictional-private" not in json.dumps(report)

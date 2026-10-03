"""Installed-wheel checks launched as two ordinary Windows users by the PS1 runner.

Only fixed outcome codes and software versions leave the disposable accounts.
This checks local CLI/storage behavior, not an interactive client journey.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import importlib.metadata
import json
import os
import re
import subprocess
import sys
import time
import zipfile
from contextlib import ExitStack, closing
from ctypes import wintypes as w
from pathlib import Path


class CheckError(Exception):
    """A failed check whose details must not enter the report."""


class SidAttributes(ctypes.Structure):
    """A SID_AND_ATTRIBUTES entry, including native pointer alignment."""

    _fields_ = [("sid", w.LPVOID), ("attributes", w.DWORD)]


class TokenGroups(ctypes.Structure):
    """The variable-length TOKEN_GROUPS header and first entry."""

    _fields_ = [("count", w.DWORD), ("groups", SidAttributes * 1)]


def require(ok: bool) -> None:
    """Fail without including private values or paths."""
    if not ok:
        raise CheckError


def token_identity(expected_sid: str) -> None:
    """Reject admin, filtered-admin, elevated, or unexpected account tokens.

    Check every group, including deny-only SIDs. IsUserAnAdmin alone would
    mistakenly accept the filtered token of an Administrators-group account.
    The API declarations here are independent of Veil's ACL implementation.
    """
    require(os.name == "nt")
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.LocalFree.argtypes = [w.HLOCAL]
    adv.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)]
    adv.GetTokenInformation.argtypes = [
        w.HANDLE,
        ctypes.c_int,
        w.LPVOID,
        w.DWORD,
        ctypes.POINTER(w.DWORD),
    ]
    adv.ConvertSidToStringSidW.argtypes = [w.LPVOID, ctypes.POINTER(w.LPWSTR)]
    token = w.HANDLE()
    require(
        bool(adv.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)))
    )

    def information(kind: int):
        size = w.DWORD()
        adv.GetTokenInformation(token, kind, None, 0, ctypes.byref(size))
        require(size.value > 0)
        data = ctypes.create_string_buffer(size.value)
        require(
            bool(adv.GetTokenInformation(token, kind, data, size, ctypes.byref(size)))
        )
        return data

    def sid_text(pointer) -> str:
        result = w.LPWSTR()
        require(bool(adv.ConvertSidToStringSidW(pointer, ctypes.byref(result))))
        try:
            return result.value or ""
        finally:
            kernel.LocalFree(result)

    try:
        user = information(1)  # TokenUser
        actual_sid = sid_text(SidAttributes.from_buffer(user).sid)
        require(actual_sid == expected_sid)
        groups = information(2)  # TokenGroups
        count = TokenGroups.from_buffer(groups).count
        entries = (SidAttributes * count).from_buffer(groups, TokenGroups.groups.offset)
        require(all(sid_text(entry.sid) != "S-1-5-32-544" for entry in entries))
        require(w.DWORD.from_buffer(information(20)).value == 0)  # TokenElevation
        require(w.DWORD.from_buffer(information(18)).value == 1)  # Default, not Limited
    finally:
        kernel.CloseHandle(token)


def command(*args: str, text: str = "", cwd: Path) -> str:
    """Invoke the installed public CLI without printing captured output."""
    result = subprocess.run(
        [sys.executable, "-I", "-m", "veil", *args],
        input=text,
        capture_output=True,
        encoding="utf-8",
        cwd=cwd,
        timeout=60,
        check=False,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    require(result.returncode == 0)
    return result.stdout


def prepare_environment(root: Path, expected_sid: str) -> None:
    """Use the logged-on account's real profile and a private temporary folder."""
    import winreg

    from veil.gateway import prepare_data_dir

    key_name = (
        r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList"
        + "\\"
        + expected_sid
    )
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_name) as key:
        profile, _ = winreg.QueryValueEx(key, "ProfileImagePath")
    profile = os.path.expandvars(profile)
    require(Path(profile).is_dir())
    os.environ["USERPROFILE"] = profile
    temporary = prepare_data_dir(root / "temp")
    os.environ["TMP"] = os.environ["TEMP"] = str(temporary)


def installation(runtime: Path) -> str:
    """Confirm the imported package lives in the staged installed environment."""
    import veil

    require(Path(veil.__file__).resolve().is_relative_to(runtime.resolve()))
    version = importlib.metadata.version("veil")
    require(version == veil.__version__)
    require(
        re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:(?:a|b|rc)[0-9]+)?", version)
        is not None
    )
    return version


def beta_check(root: Path, pack_path: Path, version: str) -> None:
    """Run the shipped beta CLI exercises from the installed wheel."""
    destination = root / "beta"
    with zipfile.ZipFile(pack_path) as pack:
        for member in pack.namelist():
            path = Path(member)
            require(not path.is_absolute() and ".." not in path.parts)
        pack.extractall(destination)
    exercise = destination / f"veil-{version}-beta"
    report_path = root / "beta-report.json"
    completed = subprocess.run(
        [sys.executable, "-I", "check.py", "--output", str(report_path)],
        cwd=exercise,
        capture_output=True,
        timeout=120,
        check=False,
    )
    require(completed.returncode == 0)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    require(report["veil_version"] == version)
    require(report["scope"] == "fictional_local_cli_only")
    require(report["client_journey"] == "not_run")
    require(
        report["checks"]
        == [
            {"code": code, "state": "pass"}
            for code in (
                "registration",
                "mask",
                "restore",
                "forget",
                "registration_removal",
            )
        ]
    )


def setup_undo(root: Path) -> None:
    """Exercise desktop-extra setup and undo against a disposable config."""
    from veil.gateway import prepare_data_dir

    folder = prepare_data_dir(root / "codex")
    config = folder / "config.toml"
    original = b'# Fictional settings\r\nmodel = "keep"\r\n'
    config.write_bytes(original)
    command(
        "--data-dir",
        str(root / "setup-data"),
        "setup",
        "codex",
        "--config",
        str(config),
        cwd=root,
    )
    require('model_provider = "veil"' in config.read_text(encoding="utf-8"))
    command("undo", "codex", "--config", str(config), cwd=root)
    require(config.read_bytes() == original)


def reject_shared_directory(root: Path, peer_sid: str) -> None:
    """An intentionally unsafe, empty specimen must be refused, never repaired."""
    from veil.gateway.config import SettingsError, prepare_data_dir

    specimen = prepare_data_dir(root / "shared-specimen")
    result = subprocess.run(
        ["icacls", str(specimen), "/grant", f"*{peer_sid}:(R)"],
        capture_output=True,
        timeout=15,
        check=False,
    )
    require(result.returncode == 0)
    try:
        prepare_data_dir(specimen)
    except SettingsError:
        return
    raise CheckError


def owner(args: argparse.Namespace, report: dict) -> None:
    """Exercise private storage, keeping SQLite sidecars live for the peer."""
    from veil import SQLiteVault, _windows
    from veil.gateway import prepare_data_dir
    from veil.gateway.store import SQLiteLedger

    root = args.root

    def step(code: str, action) -> None:
        report["checks"].append({"code": code, "state": "fail"})
        action()
        report["checks"][-1]["state"] = "pass"

    step(
        "packaged_beta_cli", lambda: beta_check(root, args.pack, report["veil_version"])
    )
    step("desktop_setup_undo", lambda: setup_undo(root))
    step("shared_acl_refused", lambda: reject_shared_directory(root, args.peer_sid))
    report["checks"].append({"code": "private_storage_and_sessions", "state": "fail"})
    data = prepare_data_dir(root / "data")
    child = data / "inherited.txt"
    child.write_text("fictional-private-storage", encoding="utf-8")
    # This file has an accessible parent: its own ACL must deny the other user.
    standalone = root / "standalone.txt"
    _windows.create_private(standalone)
    standalone.write_text("fictional-private-file", encoding="utf-8")
    with ExitStack() as stack:
        vault = stack.enter_context(SQLiteVault(data / "vault.db", session="owner"))
        placeholder = vault.get_or_create("fictional@example.org", "EMAIL")
        other = stack.enter_context(SQLiteVault(data / "vault.db", session="other"))
        require(other.get_value(placeholder) is None)
        reopened = stack.enter_context(SQLiteVault(data / "vault.db", session="owner"))
        require(reopened.get_value(placeholder) == "fictional@example.org")
        ledger = stack.enter_context(closing(SQLiteLedger(data / "ledger.db", "owner")))
        ledger.record_text("fictional@example.org", placeholder)
        other_ledger = stack.enter_context(
            closing(SQLiteLedger(data / "ledger.db", "other"))
        )
        require(other_ledger.masked_text("fictional@example.org") is None)
        for path in [data, child, standalone, *(data / name for name in PRIVATE_FILES)]:
            require(path.exists())
            _windows.check_private(path)
        report["checks"][-1]["state"] = "pass"
        (root / "traverse.txt").write_text("fictional-public-marker", encoding="utf-8")
        report["checks"].append({"code": "peer_probe_completed", "state": "fail"})
        save_report(root, report)
        (root / "ready").write_text("ready", encoding="utf-8")
        deadline = time.monotonic() + 180
        while not (root / "finish").exists():
            require(time.monotonic() < deadline)
            time.sleep(0.1)
        require((root / "finish").read_text(encoding="utf-8-sig").strip() == "pass")
        require(child.read_text(encoding="utf-8") == "fictional-private-storage")
        require(standalone.read_text(encoding="utf-8") == "fictional-private-file")
        require(vault.get_value(placeholder) == "fictional@example.org")
        report["checks"][-1]["state"] = "pass"


PRIVATE_FILES = (
    "vault.db",
    "vault.db-wal",
    "vault.db-shm",
    "ledger.db",
    "ledger.db-wal",
    "ledger.db-shm",
)


def native_open_probe(
    path: Path,
    access: int,
    *,
    directory: bool = False,
    denied: bool,
    code: str,
    report: dict,
) -> None:
    """Verify exact Win32 access results without CRT errno translation.

    OPEN_EXISTING never creates or truncates files. Directory rights are
    checked with BACKUP_SEMANTICS; these ordinary tokens have no backup bypass.
    Public-path positive controls exercise the same API before private probes.
    """
    probe = {"code": code, "state": "fail", "winerror": None}
    report["access_probes"].append(probe)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [
        w.LPCWSTR,
        w.DWORD,
        w.DWORD,
        w.LPVOID,
        w.DWORD,
        w.DWORD,
        w.HANDLE,
    ]
    kernel.CreateFileW.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.CloseHandle.restype = w.BOOL
    ctypes.set_last_error(0)
    handle = kernel.CreateFileW(
        str(path), access, 7, None, 3, 0x02000000 if directory else 0x80, None
    )
    if handle != w.HANDLE(-1).value:
        probe["winerror"] = 0
        require(bool(kernel.CloseHandle(handle)))
        require(not denied)
    else:
        probe["winerror"] = ctypes.get_last_error()
        require(denied and probe["winerror"] == 5)  # ERROR_ACCESS_DENIED only.
    probe["state"] = "pass"


def python_denial_probe(action, *, code: str, report: dict) -> None:
    """Record CRT/native Python denial differences using only fixed fields."""
    probe = {
        "code": code,
        "state": "fail",
        "error_type": "none",
        "errno": None,
        "winerror": None,
    }
    report["access_probes"].append(probe)
    try:
        action()
    except OSError as error:
        permission = isinstance(error, PermissionError)
        probe["error_type"] = "PermissionError" if permission else "OSError"
        probe["errno"] = error.errno
        probe["winerror"] = getattr(error, "winerror", None)
        # Python's _wopen-based file I/O reports EACCES without winerror.
        # Native probes above independently require ERROR_ACCESS_DENIED.
        require(
            permission
            and probe["errno"] == errno.EACCES
            and probe["winerror"] in {None, 5}
        )
        probe["state"] = "pass"
        return
    raise CheckError


def outsider(args: argparse.Namespace, report: dict) -> None:
    """Attempt actual reads/writes as a distinct ordinary Windows account."""
    parent = args.peer_root
    report["checks"].append({"code": "parent_traversal", "state": "fail"})
    require(
        (parent / "traverse.txt").read_text(encoding="utf-8")
        == "fictional-public-marker"
    )
    require("data" in {path.name for path in parent.iterdir()})
    native_open_probe(
        parent,
        1,  # FILE_LIST_DIRECTORY
        directory=True,
        denied=False,
        code="public_parent_list",
        report=report,
    )
    native_open_probe(
        parent / "traverse.txt",
        0x80000000,  # GENERIC_READ
        denied=False,
        code="public_marker_read",
        report=report,
    )
    own_temp = args.root / "temp"
    write_control = own_temp / "write-control.txt"
    write_control.write_text("fictional-write-control", encoding="utf-8")
    native_open_probe(
        own_temp,
        2,  # FILE_ADD_FILE
        directory=True,
        denied=False,
        code="own_directory_add",
        report=report,
    )
    native_open_probe(
        write_control,
        0x40000000,  # GENERIC_WRITE, still OPEN_EXISTING without truncation.
        denied=False,
        code="own_file_write",
        report=report,
    )
    require(write_control.read_text(encoding="utf-8") == "fictional-write-control")
    report["checks"][-1]["state"] = "pass"
    report["checks"].append({"code": "private_directory_denied", "state": "fail"})
    data = parent / "data"
    for code, access in [("private_directory_list", 1), ("private_directory_add", 2)]:
        native_open_probe(
            data,
            access,  # FILE_LIST_DIRECTORY / FILE_ADD_FILE
            directory=True,
            denied=True,
            code=code,
            report=report,
        )
    python_denial_probe(
        lambda: list(data.iterdir()), code="python_directory_list", report=report
    )
    python_denial_probe(
        lambda: (data / "outsider.txt").write_bytes(b"fictional"),
        code="python_file_create",
        report=report,
    )
    report["checks"][-1]["state"] = "pass"
    report["checks"].append(
        {"code": "private_files_and_sidecars_denied", "state": "fail"}
    )
    files = [
        ("standalone", parent / "standalone.txt"),
        ("inherited", data / "inherited.txt"),
    ]
    files.extend(
        (name.replace(".", "_").replace("-", "_"), data / name)
        for name in PRIVATE_FILES
    )
    for name, path in files:
        for operation, access in [("read", 0x80000000), ("write", 0x40000000)]:
            native_open_probe(
                path,
                access,
                denied=True,
                code=f"{name}_{operation}",
                report=report,
            )
    report["checks"][-1]["state"] = "pass"


def save_report(root: Path, report: dict) -> None:
    """Save fixed outcome codes, including partial progress before a peer wait."""
    (root / "result.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )


def main() -> int:
    """Save an allowlisted report even when a check fails."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["owner", "outsider"])
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--expected-sid", required=True)
    parser.add_argument("--peer-sid", required=True)
    parser.add_argument("--peer-root", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--pack", type=Path, required=True)
    args = parser.parse_args()
    report = {
        "schema": 1,
        "scope": "windows_standard_user_local_cli_storage",
        "mode": args.mode,
        "python_version": ".".join(str(v) for v in sys.version_info[:3]),
        "veil_version": None,
        "client_journey": "not_run",
        "state": "fail",
        "checks": [],
        "access_probes": [],
    }
    try:
        report["checks"].append({"code": "ordinary_user_token", "state": "fail"})
        token_identity(args.expected_sid)
        report["checks"][-1]["state"] = "pass"
        report["checks"].append({"code": "installed_wheel", "state": "fail"})
        report["veil_version"] = installation(args.runtime)
        report["checks"][-1]["state"] = "pass"
        report["checks"].append({"code": "ordinary_user_environment", "state": "fail"})
        prepare_environment(args.root, args.expected_sid)
        report["checks"][-1]["state"] = "pass"
        (owner if args.mode == "owner" else outsider)(args, report)
        report["state"] = "pass"
    except Exception:
        # Do not print exception messages, paths, account identifiers, or data.
        if all(check["state"] == "pass" for check in report["checks"]):
            report["checks"].append({"code": "unexpected_failure", "state": "fail"})
    save_report(args.root, report)
    print(f"{args.mode}: {report['state']}")
    return 0 if report["state"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())

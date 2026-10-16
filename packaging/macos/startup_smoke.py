"""Exercise the installed app through real launchd with disposable fictional data."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


def main() -> None:
    """Test launch, crash recovery, stop persistence, and receipt-checked removal."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app", type=Path)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    if sys.platform != "darwin":
        raise SystemExit("This check requires a logged-in macOS graphical session.")
    app = args.app.resolve()
    helper = app / "Contents/MacOS/veil-runtime"
    manifest = json.loads((app / "Contents/Resources/build-manifest.json").read_text())
    subprocess.run(
        ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)], check=True
    )
    root = Path(tempfile.mkdtemp(prefix="veil-native-startup-", dir="/private/tmp"))
    data = root / "private data"
    data.mkdir(mode=0o700)
    (data / "config.json").write_text('{"identity":false}')
    label = "org.veil.gateway." + hashlib.sha256(str(data).encode()).hexdigest()[:16]
    domain = f"gui/{os.getuid()}/{label}"
    target = Path.home() / "Library/LaunchAgents" / f"{label}.plist"
    checks: list[str] = []
    environment = {**os.environ, "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}

    def run(*arguments: str, source: str = "", success: bool = True) -> str:
        result = subprocess.run(
            [str(helper), "--data-dir", str(data), *arguments],
            input=source,
            text=True,
            capture_output=True,
            cwd=root,
            env=environment,
            timeout=65,
        )
        if success and result.returncode:
            raise RuntimeError(f"{' '.join(arguments)} failed: {result.stderr}")
        return result.stdout.strip()

    def status() -> dict[str, Any]:
        return json.loads(run("status", "--service", "--json", success=False))[
            "service"
        ]

    def manager(
        *arguments: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["/bin/launchctl", *arguments],
            check=check,
            text=True,
            capture_output=True,
            timeout=20,
        )

    def supervisor_pid() -> int:
        result = manager("print", domain, check=False)
        match = re.search(r"^\s*pid = (\d+)$", result.stdout, re.MULTILINE)
        return int(match[1]) if match else 0

    def wait_for(predicate: Any, *, seconds: float = 45) -> Any:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.25)
        raise RuntimeError(
            "The temporary startup service did not reach its expected state."
        )

    def lifecycle(operation: str) -> None:
        def attempt() -> bool:
            result = subprocess.run(
                [str(helper), "--data-dir", str(data), operation],
                text=True,
                capture_output=True,
                env=environment,
                cwd=root,
                timeout=65,
            )
            if "another lifecycle command is running" in result.stderr:
                return False
            if result.returncode:
                raise RuntimeError(result.stderr)
            return True

        wait_for(attempt)

    def passed(code: str) -> None:
        checks.append(code)
        print("PASS: " + code, flush=True)

    cleanup_required = False
    cleaned = False
    try:
        assert not target.exists()
        assert not target.is_symlink()
        assert manager("print", domain, check=False).returncode != 0
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = str(sock.getsockname()[1])
        run("entities", "add", "PERSON", "--stdin", source="Jane Example")
        source = "Jane Example: jane@example.com"
        masked = run("mask", "--session", "startup-rc", source=source)
        assert "Jane Example" not in masked
        assert "jane@example.com" not in masked
        run(
            "start",
            "--port",
            port,
            "--api",
            "anthropic",
            "--auth",
            "api-key",
            "--cwd",
            str(root),
        )
        lifecycle("stop")
        cleanup_required = True
        run("startup", "enable")
        wait_for(lambda: status()["state"] == "running")
        enabled = json.loads(run("startup", "status", "--json"))
        assert enabled["state"] == "enabled"
        assert enabled["active"] is True
        descriptor = plistlib.loads(target.read_bytes())
        assert descriptor["Label"] == label
        assert descriptor["ProgramArguments"][:3] == [
            str(helper),
            "--veil-worker",
            "cli",
        ]
        assert descriptor["RunAtLoad"]
        assert descriptor["KeepAlive"]
        assert "EnvironmentVariables" not in descriptor
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        passed("native_launchd_registration_and_bundled_gateway_start")

        # Derive the child from the live OS process tree, not from saved state.
        parent = supervisor_pid()
        assert parent
        processes = subprocess.check_output(
            ["/bin/ps", "-axo", "pid=,ppid=,command="], text=True, timeout=10
        )
        children = []
        pattern = re.compile(
            re.escape(f"{helper} --veil-worker gateway {data} ")
            + r"[0-9a-f]{32} [0-9]+$"
        )
        for line in processes.splitlines():
            parts = line.strip().split(None, 2)
            if (
                len(parts) == 3
                and int(parts[1]) == parent
                and pattern.fullmatch(parts[2])
            ):
                children.append(int(parts[0]))
        assert len(children) == 1
        before = status()
        assert before["pid"] == children[0]
        assert before["state"] == "running"
        os.kill(children[0], signal.SIGKILL)
        wait_for(
            lambda: (
                status().get("pid") not in (None, children[0])
                and status()["state"] == "running"
            )
        )
        passed("native_supervisor_recovers_its_crashed_gateway_child")

        before_worker = status()["pid"]
        manager("kill", "SIGKILL", domain)
        wait_for(lambda: supervisor_pid() not in (0, parent))
        wait_for(lambda: status()["state"] == "running")
        assert status()["pid"] == before_worker
        passed("launchd_recovers_supervisor_without_duplicate_gateway")

        lifecycle("stop")
        assert status()["state"] == "stopped"
        stopped_instance = json.loads((data / "service.json").read_text())["instance"]
        supervisor = supervisor_pid()
        manager("kill", "SIGKILL", domain)
        wait_for(lambda: supervisor_pid() not in (0, supervisor))
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            assert status()["state"] == "stopped"
            time.sleep(0.25)
        assert (
            json.loads((data / "service.json").read_text())["instance"]
            == stopped_instance
        )
        passed("explicit_stop_persists_across_supervisor_relaunch")

        lifecycle("start")
        wait_for(lambda: status()["state"] == "running")
        passed("explicit_start_resumes_registered_gateway")
        run("startup", "disable")
        assert json.loads(run("startup", "status", "--json"))["state"] == "disabled"
        assert manager("print", domain, check=False).returncode != 0
        assert not target.exists()
        assert not (data / "startup.json").exists()
        assert status()["state"] == "stopped"
        assert run("restore", "--session", "startup-rc", source=masked) == source
        passed("disable_removes_login_job_stops_gateway_and_preserves_mappings")
        cleaned = True
    finally:
        if cleanup_required and not cleaned:
            try:
                run("startup", "disable")
                cleaned = (
                    not target.exists()
                    and manager("print", domain, check=False).returncode != 0
                )
            except Exception:
                print(
                    f"Cleanup needs attention; preserve {root} "
                    "and run startup disable.",
                    file=sys.stderr,
                )
        elif not cleanup_required:
            run("stop", success=False)
            cleaned = True
        if cleaned:
            shutil.rmtree(root)
    report = {
        "version": manifest["version"],
        "source_sha256": manifest["source_sha256"],
        "scope": "installed_app_real_launchd_disposable_fictional_data",
        "checks": checks,
        "temporary_registration_removed": cleaned,
        "normal_startup_setting": "unchanged",
        "microphone": "not_opened",
        "logout_reboot": "not_tested",
        "live_providers": "not_called",
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()

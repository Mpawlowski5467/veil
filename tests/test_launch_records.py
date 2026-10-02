"""Owner-only records that let other terminals find a launcher's gateway."""

import json
import os
import shlex
import stat
import subprocess
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from veil import Shield, _windows
from veil.gateway import Gateway, Sessions, prepare_data_dir
from veil.launches import (
    FOLDER,
    Launch,
    find_launch,
    published,
    records,
    review_command,
    running_launches,
)

SECRET = "fictional-launch-secret-0001"
POSIX = pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")


def gateway():
    return Gateway(Sessions(lambda _: Shield()))


@pytest.fixture
def data(tmp_path):
    return prepare_data_dir(tmp_path / "data")


def record(port=5, **changes):
    body = {
        "version": 1,
        "client": "claude",
        "port": port,
        "secret": SECRET,
        "started": 1_790_000_000,
    }
    body.update(changes)
    return json.dumps(body)


def write(data, name, text):
    folder = prepare_data_dir(data / FOLDER)
    path = folder / name
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return path


def assert_private(path, mode):
    if _windows.is_windows():
        _windows.check_private(path)
    else:
        assert stat.S_IMODE(path.stat().st_mode) == mode


@pytest.mark.parametrize("fails", [False, True])
def test_a_record_is_private_while_the_launch_runs_and_removed_after(data, fails):
    url = "http://127.0.0.1:4321"
    expect = pytest.raises(RuntimeError) if fails else nullcontext()
    with expect, published(data, "codex", url, SECRET) as path:
        assert path == data / FOLDER / "4321.json"
        assert_private(data / FOLDER, 0o700)
        assert_private(path, 0o600)
        body = json.loads(path.read_text(encoding="utf-8"))
        assert set(body) == {"version", "client", "port", "secret", "started"}
        assert body["client"] == "codex"
        assert body["port"] == 4321
        assert body["secret"] == SECRET
        assert os.listdir(data / FOLDER) == ["4321.json"]
        if fails:
            raise RuntimeError("fictional client failure")
    assert not (data / FOLDER).exists()


def test_the_folder_stays_while_another_launch_holds_a_record(data):
    with published(data, "claude", "http://127.0.0.1:4001", SECRET):
        with published(data, "codex", "http://127.0.0.1:4002", SECRET):
            assert sorted(os.listdir(data / FOLDER)) == ["4001.json", "4002.json"]
        assert os.listdir(data / FOLDER) == ["4001.json"]
    assert not (data / FOLDER).exists()


def test_a_data_folder_made_only_for_the_record_is_removed(tmp_path):
    data = tmp_path / "fresh"
    with published(data, "claude", "http://127.0.0.1:4003", SECRET) as path:
        assert path is not None
        assert path.exists()
    assert not data.exists()


def test_an_unusable_data_folder_only_warns(tmp_path, capsys):
    data = tmp_path / "data"
    data.write_text("not a folder")
    with published(data, "claude", "http://127.0.0.1:4004", SECRET) as path:
        assert path is None
    err = capsys.readouterr().err
    assert err.count("\n") == 1
    assert "other terminals can't find this launch" in err
    assert SECRET not in err
    assert str(data) not in err
    assert data.read_text() == "not a folder"


def test_the_secret_is_never_in_a_repr(data):
    launch = Launch("claude", 5, SECRET, 1_790_000_000, data / FOLDER / "5.json")
    assert SECRET not in repr(launch)
    assert launch.url == "http://127.0.0.1:5"
    assert launch.describe().startswith("http://127.0.0.1:5 (claude launch, started ")
    assert SECRET not in launch.describe()


def test_running_launches_are_proved_and_stopped_ones_cleaned_up(data):
    # A stopped launch. Nothing listens on port 5, and it is below the
    # ephemeral range, so unlike a just-closed gateway's port it can't be
    # handed to a gateway opened next.
    stale = write(data, "5.json", record())
    with gateway() as live, gateway() as other:
        # Another gateway's port with a wrong secret: busy or reused, so kept.
        wrong = write(
            data, f"{other.port}.json", record(other.port, secret="fictional-wrong")
        )
        with published(data, "claude", live.url, live.secret):
            found = running_launches(data)
            assert [(launch.url, launch.secret) for launch in found] == [
                (live.url, live.secret)
            ]
            assert found[0].client == "claude"
            assert not stale.exists()
            assert wrong.exists()
            assert len(records(data)) == 2


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("abc.json", record()),
        ("0.json", record(0)),
        ("5.json", record(6)),
        ("5.txt", record()),
        ("5.json", record(extra="fictional")),
        ("5.json", json.dumps({"version": 1, "client": "claude", "port": 5})),
        ("5.json", record(version=2)),
        ("5.json", record(version=True)),
        ("5.json", record(client="other")),
        ("5.json", record(port="5")),
        ("5.json", record(secret="")),
        ("5.json", record(secret="fictional\nsecret")),
        ("5.json", record(secret="x" * 257)),
        ("5.json", record(started=1_790_000_000.5)),
        ("5.json", "not json"),
        ("5.json", "[]"),
    ],
)
def test_invalid_records_are_ignored(data, name, text):
    write(data, name, text)
    assert records(data) == []
    assert find_launch(data, 5) is None
    assert find_launch(data, 6) is None
    assert running_launches(data) == []


def test_a_valid_record_is_found_by_port(data):
    write(data, "5.json", record())
    assert [launch.port for launch in records(data)] == [5]
    launch = find_launch(data, 5)
    assert launch is not None
    assert launch.secret == SECRET
    assert find_launch(data, 6) is None


@POSIX
def test_a_record_others_can_read_is_ignored(data):
    write(data, "5.json", record()).chmod(0o644)
    assert records(data) == []
    assert find_launch(data, 5) is None


@POSIX
def test_a_linked_record_is_ignored(data, tmp_path):
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text(record())
    elsewhere.chmod(0o600)
    prepare_data_dir(data / FOLDER)
    (data / FOLDER / "5.json").symlink_to(elsewhere)
    assert records(data) == []
    assert find_launch(data, 5) is None


@POSIX
def test_a_folder_others_can_use_is_ignored(data):
    write(data, "5.json", record())
    (data / FOLDER).chmod(0o755)
    assert records(data) == []
    assert find_launch(data, 5) is None


@POSIX
def test_a_linked_folder_is_ignored(data, tmp_path):
    target = prepare_data_dir(tmp_path / "target")
    write(target, "5.json", record())
    (data / FOLDER).symlink_to(target / FOLDER)
    assert records(data) == []
    assert find_launch(data, 5) is None


def test_a_record_failing_the_windows_check_is_ignored(data, monkeypatch):
    write(data, "5.json", record())

    def refuse(path):
        raise OSError("fictional ACL grant to another account")

    monkeypatch.setattr(
        "veil.launches._windows",
        SimpleNamespace(is_windows=lambda: True, check_private=refuse),
    )
    assert records(data) == []
    assert find_launch(data, 5) is None


def test_readers_never_create_folders(tmp_path):
    parent = tmp_path / "parent"
    parent.mkdir()
    data = parent / "data"
    parent.chmod(0o500)
    try:
        assert records(data) == []
        assert find_launch(data, 5) is None
        assert running_launches(data) == []
        assert not data.exists()
    finally:
        parent.chmod(0o700)
    present = prepare_data_dir(tmp_path / "present")
    assert records(present) == []
    assert running_launches(present) == []
    assert not (present / FOLDER).exists()


def test_review_command_names_a_folder_only_when_not_the_default(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    url = "http://127.0.0.1:5"
    assert review_command(url, home / ".veil") == f"veil review --gateway-url {url}"
    folder = tmp_path / "my data"
    words = ["veil", "--data-dir", str(folder), "review", "--gateway-url", url]
    command = review_command(url, folder)
    if os.name == "nt":
        assert command == subprocess.list2cmdline(words)
        assert f'"{folder}"' in command
    else:
        assert command == shlex.join(words)
        assert f"'{folder}'" in command

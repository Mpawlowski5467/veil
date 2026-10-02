"""A request held under a launcher is reviewed from another terminal."""

import functools
import io
import json
import os
import re
import shlex
import sys
import threading
import time
from pathlib import Path

import pytest

from test_gateway_server import FakeAPI
from veil import cli, codex
from veil.gateway import Gateway, prepare_data_dir
from veil.gateway.compat import TESTED_CLAUDE_CODE

pytestmark = pytest.mark.skipif(os.name == "nt", reason="shebang stub clients")

PHRASE = "fictional four word password"

# A client that sends one request through its gateway, waits while the user
# reviews it in another terminal, then sends it again.
STUB = """#!{python}
import http.client, json, os, sys, time
from urllib.parse import urlsplit

args = sys.argv[1:]
if args == ["--version"]:
    print({version!r})
    sys.exit(0)
if {client!r} == "claude":
    settings_path = args[args.index("--settings") + 1]
    env = json.load(open(settings_path))["env"]
    url = env["ANTHROPIC_BASE_URL"]
    name, _, secret = env["ANTHROPIC_CUSTOM_HEADERS"].splitlines()[-1].partition(": ")
    path = "/v1/messages"
    body = {{
        "model": "test",
        "max_tokens": 30,
        "messages": [{{"role": "user", "content": {text!r}}}],
    }}
    headers = {{name: secret, "x-claude-code-session-id": "s-1"}}
else:
    settings_path = None
    url, secret = os.environ["VEIL_GATEWAY_URL"], os.environ["VEIL_GATEWAY_SECRET"]
    path = "/v1/responses"
    body = {{"model": "test", "input": {text!r}}}
    headers = {{
        "x-gateway-secret": secret,
        "thread-id": "s-1",
        "Authorization": "Bearer " + os.environ["OPENAI_API_KEY"],
    }}
headers["Content-Type"] = "application/json"


def send():
    conn = http.client.HTTPConnection(urlsplit(url).netloc, timeout=10)
    conn.request("POST", path, body=json.dumps(body), headers=headers)
    response = conn.getresponse()
    raw = response.read().decode()
    conn.close()
    return response.status, raw


def write(name, value):
    with open(os.environ[name] + ".tmp", "w") as f:
        json.dump(value, f)
    os.replace(os.environ[name] + ".tmp", os.environ[name])


status, raw = send()
message = json.loads(raw)["error"]["message"] if status != 200 else ""
write("HELD", {{
    "status": status,
    "message": message,
    "url": url,
    "secret": secret,
    "settings": settings_path,
}})
deadline = time.monotonic() + 30
while not os.path.exists(os.environ["APPROVED"]) and time.monotonic() < deadline:
    time.sleep(0.05)
status, raw = send()
write("DONE", {{"status": status, "body": raw}})
"""


class Terminal(io.StringIO):
    """The user's own interactive terminal."""

    def isatty(self):
        return True


def reply(client):
    if client == "codex":
        return {
            "output": [
                {
                    "type": "message",
                    "id": "msg_test",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "[PASSWORD_1]",
                            "annotations": [],
                        }
                    ],
                }
            ]
        }
    return {"content": [{"type": "text", "text": "[PASSWORD_1]"}]}


@pytest.fixture
def world(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    for name in (
        "VEIL_GATEWAY_URL",
        "VEIL_GATEWAY_SECRET",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_CUSTOM_HEADERS",
    ):
        monkeypatch.delenv(name, raising=False)
    data = prepare_data_dir(tmp_path / "my data")
    (data / "config.json").write_text('{"identity": false, "secret_review": true}')
    fake = FakeAPI()
    gateway = functools.partial(Gateway, upstream=fake.host, secure=False)
    monkeypatch.setattr(cli, "Gateway", gateway)
    monkeypatch.setattr(codex, "Gateway", gateway)
    files = {name: tmp_path / name.lower() for name in ("HELD", "APPROVED", "DONE")}
    for name, path in files.items():
        monkeypatch.setenv(name, str(path))
    yield tmp_path, data, fake, files
    fake.close()


def stub(tmp_path, monkeypatch, client):
    path = tmp_path / "bin" / client
    path.parent.mkdir()
    path.write_text(
        STUB.format(
            python=sys.executable,
            version=f"{TESTED_CLAUDE_CODE} (Claude Code)",
            client=client,
            text=f'Use "{PHRASE}" to sign in.',
        )
    )
    path.chmod(0o755)
    monkeypatch.setenv("PATH", f"{path.parent}{os.pathsep}{os.environ['PATH']}")


def wait_for(path, seconds=30):
    deadline = time.monotonic() + seconds
    while not path.exists():
        assert time.monotonic() < deadline, f"{path.name} never appeared"
        time.sleep(0.05)
    return json.loads(path.read_text())


@pytest.mark.parametrize("how", ["plain", "quoted"])
@pytest.mark.parametrize(
    ("client", "forget"), [("claude", False), ("codex", False), ("claude", True)]
)
def test_review_from_another_terminal_releases_the_held_request(
    world, monkeypatch, capfd, client, forget, how
):
    tmp_path, data, fake, files = world
    stub(tmp_path, monkeypatch, client)
    fake.replies.append((200, "application/json", [json.dumps(reply(client)).encode()]))
    options = ["--data-dir", str(data), *(["--forget-after-run"] if forget else [])]
    if client == "codex":
        monkeypatch.setenv("OPENAI_API_KEY", "fictional-orchard-42")
        argv = [*options, "codex", "--auth", "api-key", "--", "exec", "hi"]
    else:
        argv = [*options, "claude", "-p", "hi"]
    seen = {}

    def other_terminal():
        try:
            held = wait_for(files["HELD"])
            seen["held"] = held
            seen["records"] = sorted(os.listdir(data / "launches"))
            terminal = Terminal()
            monkeypatch.setattr("sys.stdin", Terminal())
            monkeypatch.setattr("sys.stdout", terminal)
            monkeypatch.setattr(
                "veil.review_cli.ask_terminal", lambda value, reason: "PASSWORD"
            )
            if how == "plain":
                command = ["--data-dir", str(data), "review"]
            else:
                quoted = re.search(r"Run `([^`]*)`", held["message"]).group(1)
                words = shlex.split(quoted)
                assert words[0] == "veil"
                command = words[1:]
            seen["review"] = cli.main([*command, "--terminal"])
            seen["review_out"] = terminal.getvalue()
        finally:
            files["APPROVED"].touch()

    reviewer = threading.Thread(target=other_terminal)
    reviewer.start()
    try:
        code = cli.main(argv)
    finally:
        files["APPROVED"].touch()
        reviewer.join(60)
    assert code == 0
    assert seen["review"] == 0
    held, done = seen["held"], wait_for(files["DONE"])
    url = held["url"]
    port = url.rsplit(":", 1)[1]
    # The record was in the real data folder while the client ran.
    assert seen["records"] == [f"{port}.json"]
    assert held["status"] == 403
    command = shlex.join(
        ["veil", "--data-dir", str(data), "review", "--gateway-url", url]
    )
    assert f"Run `{command}` in your own local terminal" in held["message"]
    assert done["status"] == 200
    assert PHRASE in done["body"]
    assert len(fake.received) == 1
    sent = fake.received[0][3].decode()
    assert "[PASSWORD_1]" in sent
    assert PHRASE not in sent
    assert not (data / "launches").exists()
    if forget:
        # Mappings lived in a temporary folder that is gone; nothing new here.
        assert not Path(held["settings"]).parent.parent.exists()
        assert sorted(os.listdir(data)) == ["config.json"]
    output = capfd.readouterr()
    assert held["secret"] not in output.out + output.err + seen["review_out"]

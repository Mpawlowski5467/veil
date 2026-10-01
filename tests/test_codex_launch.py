"""Managed Codex routing and private configuration generation."""

import json
import stat
from urllib.parse import urlsplit

import pytest

from veil import Shield, cli, codex
from veil.gateway import Gateway, Sessions


@pytest.fixture
def data_dir(tmp_path):
    directory = tmp_path / "veil"
    directory.mkdir(mode=0o700)
    (directory / "config.json").write_text('{"identity":false}')
    return directory


@pytest.mark.parametrize("auth", ["chatgpt", "api-key"])
def test_launcher_pins_the_provider_and_keeps_secret_out_of_argv(
    data_dir, monkeypatch, auth
):
    monkeypatch.setenv("OPENAI_API_KEY", "fictional")
    seen = []

    def child(command, env, cwd):
        seen.append(command)
        assert "--no-daemon" in command
        assert env["VEIL_GATEWAY_SECRET"] not in " ".join(command)
        assert 'model_provider="veil"' in command
        config = command[command.index('model_provider="veil"') + 2]
        assert '"base_url"="http://127.0.0.1:' in config
        assert '"supports_websockets"=false' in config
        assert f'"requires_openai_auth"={str(auth == "chatgpt").lower()}' in config
        assert "features.apps=false" in command
        assert 'web_search="disabled"' in command
        assert command[-2:] == ["exec", "hello"]
        # Other terminals find the gateway through an owner-only record.
        port = urlsplit(env["VEIL_GATEWAY_URL"]).port
        record = json.loads((data_dir / "launches" / f"{port}.json").read_text())
        assert record["client"] == "codex"
        assert record["secret"] == env["VEIL_GATEWAY_SECRET"]
        return 17

    monkeypatch.setattr(cli, "_run_child", child)
    assert (
        codex.run_codex(
            ["exec", "hello"], data_dir=data_dir, codex="fake-codex", auth=auth
        )
        == 17
    )
    assert len(seen) == 1
    assert not (data_dir / "launches").exists()


@pytest.mark.parametrize(
    "args",
    [
        ["--config", 'model_provider="openai"'],
        ["-cmodel_provider=openai"],
        ["--profile=other"],
        ["--oss"],
        ["--remote", "ws://elsewhere"],
        ["exec", "--enable", "apps"],
        ["--model", "gpt-6-sol", "app"],
        ["cloud"],
    ],
)
def test_bypass_options_never_start_a_child(data_dir, monkeypatch, args):
    def child(*_):
        pytest.fail("a bypassing client was started")

    monkeypatch.setattr(cli, "_run_child", child)
    assert codex.run_codex(args, data_dir=data_dir, codex="fake-codex") == 2


def test_missing_api_key_does_not_start(data_dir, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert (
        codex.run_codex([], data_dir=data_dir, codex="fake-codex", auth="api-key") == 2
    )


def test_cli_forwards_only_codex_arguments(data_dir, monkeypatch):
    seen = []
    monkeypatch.setattr(
        codex, "run_codex", lambda args, **kw: seen.append((args, kw)) or 0
    )
    assert (
        cli.main(
            [
                "--data-dir",
                str(data_dir),
                "codex",
                "--auth",
                "api-key",
                "--",
                "exec",
                "--model",
                "gpt-6-luna",
                "hello",
            ]
        )
        == 0
    )
    assert seen == [
        (
            ["exec", "--model", "gpt-6-luna", "hello"],
            {"data_dir": data_dir, "auth": "api-key"},
        )
    ]


def test_desktop_configuration_is_private_and_uses_selected_auth(data_dir):
    with Gateway(
        Sessions(lambda _: Shield(), api="openai"), api="openai", openai_auth="chatgpt"
    ) as gateway:
        path = codex.write_configuration(gateway, data_dir)
        text = path.read_text()
        assert 'model_provider = "veil"' in text
        assert "requires_openai_auth = true" in text
        assert "env_key" not in text
        assert gateway.secret in text
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        # Refreshing the fragment does not append duplicate tables.
        codex.write_configuration(gateway, data_dir)
        assert path.read_text() == text
        assert len(list(data_dir.glob("*"))) == 2


def test_chatgpt_gateway_prints_matching_settings(data_dir, capsys):
    assert (
        cli.run_gateway(
            data_dir=data_dir, port=0, api="openai", auth="chatgpt", stop=lambda: True
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "requires_openai_auth = true" in out
    assert "uses chatgpt authentication" in out
    assert (data_dir / "codex-provider.toml").exists()


def test_non_openai_gateway_refuses_chatgpt_auth(data_dir, capsys):
    assert cli.main(["--data-dir", str(data_dir), "gateway", "--auth", "chatgpt"]) == 2
    assert "requires --api openai" in capsys.readouterr().err


def test_toml_strings_escape_code_and_paths():
    value = 'a"b\\c\nnext'
    assert codex.toml_value(value) == json.dumps(value)

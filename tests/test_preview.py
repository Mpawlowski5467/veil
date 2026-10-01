"""Local preview detection, privacy boundaries, and bounded input handling."""

import json
import threading
from urllib.parse import urlsplit

import pytest

from test_secret_review import browser_call
from veil.preview import MAX_TEXT, preview, preview_server, run_preview


def test_preview_masks_reviews_restores_and_forgets_between_requests(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    text = 'Email jane@example.org. Use "fictional meadow phrase" to sign in.'
    source = {"text": text, "choices": {}}
    report = preview(source)
    assert "jane@example.org" not in report["masked"]
    assert report["findings"][0]["value"] == "fictional meadow phrase"
    assert report["unresolved"] == 1
    assert report["restored"] == text
    assert report["masks"][0]["reason"]
    decided = preview({**source, "choices": {"0": "PASSWORD"}})
    assert "fictional meadow phrase" not in decided["masked"]
    assert "[PASSWORD_1]" in decided["masked"]
    assert decided["unresolved"] == 0
    assert decided["restored"] == text
    assert preview(source) == report
    assert preview({**source, "choices": {"0": "IGNORE"}})["masked"] == report["masked"]
    assert list(tmp_path.iterdir()) == []


def test_literal_placeholders_are_not_accidentally_restored():
    text = "[EMAIL_1] jane@example.org"
    result = preview({"text": text, "choices": {}})
    assert result["restored"] == text
    assert result["masked"] != text


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"text": 3, "choices": {}},
        {"text": "x" * (MAX_TEXT + 1), "choices": {}},
        {"text": "", "choices": []},
        {"text": "", "choices": {"0": "PASSWORD"}},
        {"text": "", "choices": {"-1": "IGNORE"}},
        {"text": "", "choices": {"0": []}},
        {"text": "", "choices": {"\u0660": "IGNORE"}},
        {"text": "", "choices": {}, "config": "private"},
    ],
)
def test_malformed_preview_rejected(payload):
    with pytest.raises(ValueError, match=r"^$"):
        preview(payload)


def test_preview_transport_requires_local_capability_and_does_not_echo_errors(capsys):
    with preview_server() as server:
        worker = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}
        )
        worker.start()
        try:
            response, html = browser_call(server, path="/", auth=False)
            assert response.status == 200
            assert server.token.encode() not in html
            assert server.launch.encode() not in html
            assert response.getheader("Cache-Control") == "no-store"
            assert "connect-src 'self'" in response.getheader("Content-Security-Policy")
            assert response.getheader("Access-Control-Allow-Origin") is None
            for headers in (
                {"Host": "evil.test"},
                {"Origin": "https://evil.test"},
                {"Sec-Fetch-Site": "cross-site"},
            ):
                assert browser_call(server, headers=headers)[0].status == 403
            assert browser_call(server, auth=False)[0].status == 403
            assert browser_call(server, path="/config.json")[0].status == 403
            assert (
                browser_call(server, path="/data?token=" + server.token)[0].status
                == 403
            )
            data = json.dumps({"text": "jane@example.org", "choices": {}})
            response, body = browser_call(server, "POST", data=data)
            assert response.status == 200
            assert json.loads(body)["masked"] == "[EMAIL_1]"
            private = "fictional-private-error-value"
            response, body = browser_call(
                server,
                "POST",
                data=json.dumps({"text": private, "choices": {"99": "TOKEN"}}),
            )
            assert response.status == 400
            assert private.encode() not in body
            # Reject an oversized declared body before reading any bytes. If
            # we race to upload the whole body, early refusal may reset TCP
            # before the client can read the 400 on some operating systems.
            assert (
                browser_call(server, "POST", headers={"Content-Length": "262145"})[
                    0
                ].status
                == 400
            )
        finally:
            server.shutdown()
            worker.join()
    captured = capsys.readouterr()
    assert not captured.out + captured.err


def test_run_preview_opens_only_a_one_time_link(monkeypatch, capsys):
    opened, servers = [], []
    clock = iter([0, 10_000])

    def capture():
        servers.append(preview_server())
        return servers[-1]

    monkeypatch.setattr("veil.preview.webbrowser.open", opened.append)
    monkeypatch.setattr("veil.preview.preview_server", capture)
    monkeypatch.setattr("veil.preview.time.monotonic", lambda: next(clock, 10_000))
    assert run_preview() == 0
    [server] = servers
    assert opened == [server.url]
    assert urlsplit(server.url).fragment == server.launch
    assert server.launch != server.token
    out = capsys.readouterr().out
    assert server.url in out
    assert server.token not in out

"""Verify actual request evidence against scripted local provider responses."""

import copy
import json
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from test_gateway_server import FakeAPI, call, sse, stream_reply, text_reply
from veil import Shield
from veil.cli import main
from veil.gateway import Gateway, Sessions
from veil.gateway.activity import ACTIVITY_PATH, VERIFY_PATH, Activity
from veil.verification import endpoint


@pytest.fixture(params=["anthropic", "openai"])
def running(request):
    api = FakeAPI()
    with Gateway(
        Sessions(lambda _: Shield(), api=request.param),
        api=request.param,
        upstream=api.host,
        secure=False,
    ) as gateway:
        yield api, gateway
    api.close()


def local(gateway, path, method="GET", **kwargs):
    response, body = call(gateway, method=method, path=path, **kwargs)
    return response.status, json.loads(body)


def create(gateway):
    status, result = local(gateway, VERIFY_PATH, "POST", body={})
    assert status == 200
    return result


def check(gateway, token):
    status, result = local(gateway, VERIFY_PATH + "?id=" + token)
    assert status == 200
    return result


def request_body(gateway, prompt):
    if gateway.api == "anthropic":
        return {
            "model": "fixture",
            "max_tokens": 80,
            "messages": [{"role": "user", "content": prompt}],
        }
    return {
        "model": "fixture",
        "store": False,
        "input": [
            {"role": "user", "content": [{"type": "input_text", "text": prompt}]}
        ],
    }


def json_reply(gateway, text="[EMAIL_1]"):
    if gateway.api == "anthropic":
        return {
            "id": "msg",
            "type": "message",
            "role": "assistant",
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": text}],
        }
    return {
        "id": "resp",
        "status": "completed",
        "output": [
            {
                "id": "msg",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
        ],
    }


def respond(api, gateway, text="[EMAIL_1]", stream=False):
    body = json_reply(gateway, text)
    if not stream:
        api.replies.append((200, "application/json", [json.dumps(body).encode()]))
    elif gateway.api == "anthropic":
        api.replies.append(text_reply(text[:4], text[4:]))
    else:
        api.replies.append(
            stream_reply(sse({"type": "response.completed", "response": body}))
        )


def send(gateway, body, session="client-session"):
    response = call(
        gateway,
        path="/v1/responses" if gateway.api == "openai" else "/v1/messages",
        body=body,
        headers={"thread-id": session, "x-claude-code-session-id": session},
    )
    # The last response bytes can reach the client before the handler's finally
    # block commits activity. Wait for that bookkeeping, without presuming a
    # successful outcome: the tests below still assert verified vs incomplete.
    deadline = time.monotonic() + 2
    while any(
        item["requests"] != item["completed"] + item["failed"]
        for item in gateway.activity.summary()["sessions"]
    ):
        if time.monotonic() >= deadline:
            pytest.fail("gateway did not finish recording request evidence")
        time.sleep(0.005)
    return response


@pytest.mark.parametrize("stream", [False, True])
def test_real_request_mask_forward_restore_and_scoped_activity(running, stream):
    api, gateway = running
    probe = create(gateway)
    token = probe["verification_id"]
    assert api.received == []
    assert check(gateway, token)["state"] == "pending"
    respond(api, gateway, stream=stream)
    response, reply = send(
        gateway, request_body(gateway, probe["prompt"]), "private-session-id"
    )
    assert response.status == 200
    canary = f"veil-check-{token}@example.com"
    assert canary.encode() not in api.received[-1][3]
    assert b"[EMAIL_1]" in api.received[-1][3]
    assert canary.encode() in reply
    result = check(gateway, token)
    assert result["state"] == "verified"
    assert all(
        result[field] for field in ("masked", "forwarded", "restored", "completed")
    )
    _, activity = local(gateway, ACTIVITY_PATH)
    session = activity["sessions"][0]
    assert result["session_ref"] == session["session_ref"]
    assert session["recently_verified"] is True
    assert session["placeholder_occurrences"] == {"EMAIL": 1}
    assert session["requests"] == session["completed"] == session["forwarded"] == 1
    serialized = json.dumps(activity)
    assert "private-session-id" not in serialized
    assert token not in serialized
    assert canary not in serialized
    assert gateway.secret not in serialized


@pytest.mark.parametrize(
    "failure", ["no_echo", "raw_echo", "upstream_error", "truncated", "late_error"]
)
def test_partial_or_failed_round_trip_never_verifies(running, failure):
    api, gateway = running
    probe = create(gateway)
    token = probe["verification_id"]
    if failure == "no_echo":
        respond(api, gateway, "I cannot repeat that.")
    elif failure == "raw_echo":
        respond(api, gateway, f"veil-check-{token}@example.com")
    elif failure == "upstream_error":
        api.replies.append((401, "application/json", [b'{"error":"unauthorized"}']))
    else:
        respond(api, gateway, stream=True)
        status, content_type, parts = api.replies.pop()
        if gateway.api == "anthropic":
            parts = parts[:-1]
        else:
            # Emit restored text, but omit successful response completion.
            parts = [
                sse(
                    {
                        "type": "response.output_item.done",
                        "item": json_reply(gateway)["output"][0],
                    }
                )
            ]
        if failure == "late_error":
            parts.append(sse({"type": "error", "error": {"message": "fixture error"}}))
        api.replies.append((status, content_type, parts))
    send(gateway, request_body(gateway, probe["prompt"]))
    result = check(gateway, token)
    assert result["state"] == "incomplete"
    assert result["verified_at"] is None
    assert local(gateway, ACTIVITY_PATH)[1]["sessions"][0]["recently_verified"] is False


def test_no_masking_means_no_verification():
    api = FakeAPI()
    with Gateway(
        Sessions(lambda _: Shield(detectors=[])), upstream=api.host, secure=False
    ) as gateway:
        probe = create(gateway)
        respond(api, gateway, probe["prompt"].splitlines()[-1])
        send(gateway, request_body(gateway, probe["prompt"]))
        result = check(gateway, probe["verification_id"])
        assert result["state"] == "incomplete"
        assert result["masked"] is False
    api.close()


@pytest.mark.parametrize("surface", ["history", "tool_result", "system"])
def test_only_latest_plain_user_prompt_consumes_probe(running, surface):
    api, gateway = running
    probe = create(gateway)
    body = request_body(gateway, probe["prompt"])
    key = "input" if gateway.api == "openai" else "messages"
    if surface == "history":
        body[key].append({"role": "user", "content": "A different question."})
    elif surface == "system":
        body[key][0]["role"] = "system"
    elif gateway.api == "openai":
        body[key] = [
            {"type": "function_call_output", "call_id": "c1", "output": probe["prompt"]}
        ]
    else:
        body[key][0]["content"] = [
            {"type": "tool_result", "tool_use_id": "t1", "content": probe["prompt"]}
        ]
    respond(api, gateway)
    send(gateway, body)
    assert check(gateway, probe["verification_id"])["state"] == "pending"


def test_probe_is_one_use_and_does_not_verify_a_second_session(running):
    api, gateway = running
    probe = create(gateway)
    for session in ("first", "second"):
        respond(api, gateway)
        send(gateway, request_body(gateway, probe["prompt"]), session)
    activity = local(gateway, ACTIVITY_PATH)[1]["sessions"]
    assert sum(item["recently_verified"] for item in activity) == 1
    assert activity[0]["recently_verified"] is False


def test_local_endpoints_require_secret_and_refuse_browsers(running):
    api, gateway = running
    for path in (VERIFY_PATH, ACTIVITY_PATH):
        for headers, code in (
            ({"x-gateway-secret": "bad"}, 401),
            ({"Origin": "http://evil.test"}, 403),
        ):
            status, _ = local(gateway, path, headers=headers)
            assert status == code
    assert local(gateway, VERIFY_PATH + "?id=private@example.com")[0] == 400
    assert local(gateway, VERIFY_PATH + "?id=" + "a" * 32 + "&id=" + "b" * 32)[0] == 400
    assert local(gateway, VERIFY_PATH, "POST", body={"secret": "private"})[0] == 400
    assert api.received == []


def test_counts_repeat_history_but_hide_custom_names_and_original_ids(running):
    api, gateway = running
    with gateway.sessions.use("sensitive@example.com") as session:
        session.shield.add_entity("secret name", "PRIVATE_CUSTOM_LABEL")
    body = request_body(gateway, "Email alice@example.com about secret name")
    for _ in range(2):
        respond(api, gateway, "OK")
        send(gateway, body, "sensitive@example.com")
    report = local(gateway, ACTIVITY_PATH)[1]
    assert report["sessions"][0]["placeholder_occurrences"] == {"EMAIL": 2, "CUSTOM": 2}
    serialized = json.dumps(report)
    for value in (
        "sensitive@example.com",
        "secret name",
        "PRIVATE_CUSTOM_LABEL",
        "alice@example.com",
    ):
        assert value not in serialized


def test_expiration_restart_and_bounded_retention(monkeypatch):
    from veil.gateway import activity

    clock = [1000.0]
    monkeypatch.setattr(
        activity,
        "time",
        SimpleNamespace(monotonic=lambda: clock[0], time=lambda: clock[0]),
    )
    book = Activity()
    probe = book.create_probe()
    clock[0] += 601
    assert book.probe(probe["verification_id"])["state"] == "expired"
    assert Activity().probe(probe["verification_id"])["state"] == "unknown"
    for _ in range(65):
        book.create_probe()
    assert book.probe(probe["verification_id"])["state"] == "unknown"
    for number in range(129):
        shield = Shield()
        observation = book.begin(str(number), {}, {}, shield, "anthropic")
        book.finish(observation, False)
    assert len(book.summary()["sessions"]) == 128
    clock[0] += 3601
    assert book.summary()["sessions"] == []


def test_concurrent_probe_claims_only_one_session():
    book = Activity()
    probe = book.create_probe()
    request = {"messages": [{"role": "user", "content": probe["prompt"]}]}

    def start(number):
        shield = Shield()
        masked = copy.deepcopy(request)
        masked["messages"][0]["content"] = shield.mask(probe["prompt"]).text
        return book.begin(str(number), request, masked, shield, "anthropic")

    with ThreadPoolExecutor(max_workers=4) as executor:
        observations = list(executor.map(start, range(8)))
    assert sum(item.token is not None for item in observations) == 1


def test_finishing_an_evicted_inflight_session_keeps_consistent_counts():
    book = Activity()
    shield = Shield()
    old = book.begin("first", {}, {}, shield, "anthropic")
    for number in range(129):
        book.begin(str(number), {}, {}, shield, "anthropic")
    new = book.begin("first", {}, {}, shield, "anthropic")
    book.finish(old, False)
    book.finish(new, False)
    item = book.summary()["sessions"][0]
    assert item["requests"] == item["failed"] == 2
    assert item["last_request_at"] is not None


def test_events_after_successful_completion_do_not_verify(running):
    api, gateway = running
    probe = create(gateway)
    respond(api, gateway, stream=True)
    status, content_type, parts = api.replies.pop()
    parts += [
        sse(
            {
                "type": "message_stop"
                if gateway.api == "anthropic"
                else "response.created"
            }
        )
    ]
    api.replies.append((status, content_type, parts))
    send(gateway, request_body(gateway, probe["prompt"]))
    assert check(gateway, probe["verification_id"])["state"] == "incomplete"


def test_token_count_requests_do_not_claim_probes_or_count_as_activity():
    api = FakeAPI()
    with Gateway(
        Sessions(lambda _: Shield()), upstream=api.host, secure=False
    ) as gateway:
        probe = create(gateway)
        api.replies.append((200, "application/json", [b'{"input_tokens": 12}']))
        response, _ = call(
            gateway,
            path="/v1/messages/count_tokens",
            body=request_body(gateway, probe["prompt"]),
        )
        assert response.status == 200
        assert check(gateway, probe["verification_id"])["state"] == "pending"
        assert local(gateway, ACTIVITY_PATH)[1]["sessions"] == []
    api.close()


def test_cli_from_inherited_launcher_environment(running, monkeypatch, capsys):
    api, gateway = running
    monkeypatch.setenv("VEIL_GATEWAY_URL", gateway.url)
    monkeypatch.setenv("VEIL_GATEWAY_SECRET", gateway.secret)
    assert main(["verify", "--json"]) == 0
    probe = json.loads(capsys.readouterr().out)
    assert main(["verify", "--check", probe["verification_id"], "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["state"] == "pending"
    respond(api, gateway)
    send(gateway, request_body(gateway, probe["prompt"]))
    assert main(["status", "--verification", probe["verification_id"], "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["verification"]["state"] == "verified"
    assert gateway.secret not in json.dumps(result)
    assert main(["status", "--service", "--activity"]) == 2


def test_explicit_gateway_cli_and_invalid_ids(running, tmp_path, capsys):
    _, gateway = running
    secret = tmp_path / "gateway-secret"
    secret.write_text(gateway.secret)
    secret.chmod(0o600)
    options = ["--data-dir", str(tmp_path)]
    assert main([*options, "verify", "--gateway-url", gateway.url, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "pending"
    assert (
        main([*options, "verify", "--check", "bad?query", "--gateway-url", gateway.url])
        == 2
    )
    assert (
        main([*options, "verify", "--gateway-url", "https://remote.example.com"]) == 2
    )


def test_codex_configuration_endpoint_and_identity_failure(
    running, tmp_path, monkeypatch, capsys
):
    _, gateway = running
    from veil.codex_setup import setup_codex

    config = tmp_path / "codex" / "config.toml"
    data = tmp_path / "data"
    assert setup_codex(data, config, gateway.port, "api-key") == 0
    # Setup has a different secret: proof must fail before it is sent in a header.
    assert main(["verify", "--config", str(config)]) == 2
    assert "identity" in capsys.readouterr().err
    monkeypatch.setenv("VEIL_GATEWAY_URL", "https://bad.invalid")
    selected = endpoint(config=config)
    assert selected.url == gateway.url
    assert selected.secret not in repr(selected)

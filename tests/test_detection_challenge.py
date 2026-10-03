"""Separate challenges retain misses and test explicit registration/review behavior."""

import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from test_gateway_server import FakeAPI, call
from veil import Shield
from veil.gateway import Gateway, Session, Sessions

SPEC = importlib.util.spec_from_file_location(
    "veil_detection_challenge",
    Path(__file__).resolve().parents[1] / "benchmarks" / "challenge.py",
)
challenge = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = challenge
SPEC.loader.exec_module(challenge)


@pytest.fixture(scope="module")
def report():
    return challenge.evaluate()


def case(text, registrations=()):
    return challenge.Challenge(
        challenge.leaks.Document(
            "test", "prose", "en", (challenge.leaks.unpack("prompt", text),)
        ),
        registrations,
    )


def test_explicit_registrations_are_separate_from_privacy_labels():
    fixture = case("Write to [[PERSON|Nora Fictitia]].", (("PERSON", "Nora Fictitia"),))
    default = challenge.measure(fixture, "openai", False)
    registered = challenge.measure(fixture, "openai", False, registered=True)
    assert default["masked_characters"] == [0]
    assert default["registrations_applied"] == 0
    assert registered["masked_characters"] == [13]
    assert registered["registrations_applied"] == 1
    assert registered["restored"] is True
    # Label removal does not change detection or apply an oracle registration.
    unlabeled = challenge.measure(case("Write to Nora Fictitia."), "openai", False)
    assert unlabeled["exact_tp"] == default["exact_tp"] == 0
    assert unlabeled["review_findings"] == default["review_findings"] == 0


def test_review_is_withholding_and_not_automatic_masking():
    fixture = case("Use '[[PASSWORD|fictional purple forest]]' to sign in.")
    row = challenge.measure(fixture, "anthropic", True)
    assert row["status"] == "held"
    assert row["masked_characters"] == [0]
    assert row["review_characters"] == [23]
    summary = challenge.leaks.summarize([fixture.document], [row])["total"]
    assert summary["fully_masked"] == 0
    assert summary["exposed_in_ready_requests"] == 0
    assert row["restored"] is True


def test_unknown_literal_placeholder_preserves_text_but_reports_warning():
    row = challenge.measure(case("The sample uses [PERSON_1]."), "openai", False)
    assert row["restored"] is True
    assert row["restoration_warnings"] == 1
    assert row["exact_fp"] == 0
    with pytest.raises(ValueError, match="unrecognized placeholder"):
        challenge.trace_masks("changed text", "[PERSON_1]", Shield())


@pytest.mark.parametrize(
    "mutation", ["duplicate", "empty", "field", "surface", "registration"]
)
def test_challenge_corpus_is_validated_before_detection(tmp_path, mutation):
    data = json.loads(challenge.CORPUS.read_text(encoding="utf-8"))[:1]
    if mutation == "duplicate":
        data *= 2
    elif mutation == "empty":
        data = []
    elif mutation == "field":
        data[0]["skip"] = True
    elif mutation == "surface":
        data[0]["sections"][0]["surface"] = "attachment"
    else:
        data[0]["registrations"] = [{"type": "PERSON", "value": ""}]
    path = tmp_path / "corpus.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="corpus"):
        challenge.load_corpus(path)


def test_challenge_baseline_preserves_all_cases_and_restoration(report):
    baseline = json.loads(challenge.BASELINE.read_text(encoding="utf-8"))
    assert not challenge.regressions(report, baseline)
    for run in report["runs"].values():
        assert all(row["restored"] for row in run["cases"])
        assert run["total"]["requests"] == 44
        assert run["total"]["annotations"] == 36
        assert run["total"]["negative_requests"] == 11


def test_windows_checkout_line_endings_preserve_comparable_evidence(tmp_path, report):
    path = tmp_path / "crlf.json"
    path.write_bytes(
        challenge.CORPUS.read_text(encoding="utf-8").replace("\n", "\r\n").encode()
    )
    assert challenge.leaks.snapshot(
        challenge.evaluate(path)
    ) == challenge.leaks.snapshot(report)


@pytest.mark.parametrize(
    ("metric", "value"),
    [
        ("masked_characters", [0]),
        ("masked_or_flagged_characters", [0]),
        ("overmasked_characters", 999),
        ("exact_fp", 999),
        ("restoration_warnings", 999),
        ("restored", False),
        ("status", "refused"),
    ],
)
def test_baseline_rejects_per_case_regressions(report, metric, value):
    changed = copy.deepcopy(report)
    row = next(
        row
        for row in changed["runs"]["openai/review"]["cases"]
        if row["id"] == "python-typed-literal"
    )
    row[metric] = value
    assert challenge.regressions(changed, challenge.leaks.snapshot(report))


def test_corpus_relabelling_requires_explicit_baseline_review(report):
    baseline = challenge.leaks.snapshot(report)
    baseline["corpus_sha256"] = "different"
    assert challenge.regressions(report, baseline)


def test_report_contains_counts_not_source_text_or_registrations(report):
    serialized = json.dumps(report)
    for fixture in challenge.load_corpus():
        for section in fixture.document.sections:
            assert section.text not in serialized
        for _, value in fixture.registrations:
            assert value not in serialized


@pytest.mark.parametrize("api", ["anthropic", "openai"])
@pytest.mark.parametrize("review", [False, True], ids=["defaults", "review"])
@pytest.mark.parametrize(
    "registered", [False, True], ids=["unregistered", "registered"]
)
def test_challenge_status_and_masks_match_actual_local_egress(
    report, api, review, registered
):
    """Check representative success, miss, hold, registration and harmless cases."""
    selected = {
        "env-credential-shaped-literal",
        "python-typed-literal",
        "toml-multiline-password",
        "labelled-customer-address",
        "prose-sign-in-phrase",
        "harmless-public-build-digest",
        "registered-client-name",
        "registered-name-case-variant",
        "harmless-literal-placeholder",
    }
    fixtures = {
        fixture.document.id: fixture
        for fixture in challenge.load_corpus()
        if fixture.document.id in selected
    }

    def make_session(session_id):
        fixture = fixtures[session_id]
        registrations = fixture.registrations if registered else ()
        shield, ledger, masker = challenge.make_masker(api, review, registrations)
        return Session(shield, ledger, masker)

    upstream = FakeAPI()
    route = "/v1/responses" if api == "openai" else "/v1/messages"
    mode = ("registered-" if registered else "") + ("review" if review else "defaults")
    rows = {row["id"]: row for row in report["runs"][f"{api}/{mode}"]["cases"]}
    empty = {"output": []} if api == "openai" else {"content": []}
    try:
        with (
            Sessions(make_session=make_session) as sessions,
            Gateway(
                sessions, api=api, upstream=upstream.host, secure=False, keepalive=0.05
            ) as gateway,
        ):
            for fixture in fixtures.values():
                document = fixture.document
                row = rows[document.id]
                body, paths = challenge.leaks.request(document, api)
                before = len(upstream.received)
                upstream.replies[:] = [
                    (200, "application/json", [json.dumps(empty).encode()])
                ]
                headers = {
                    "thread-id"
                    if api == "openai"
                    else "x-claude-code-session-id": document.id
                }
                response, _ = call(gateway, "POST", route, body, headers=headers)
                if row["status"] == "held":
                    assert response.status == 403, document.id
                    assert len(upstream.received) == before, document.id
                    continue
                assert row["status"] == "ready", document.id
                assert response.status == 200, document.id
                assert len(upstream.received) == before + 1, document.id
                received = json.loads(upstream.received[-1][3])
                shield = sessions.get(document.id).shield
                coverage = []
                for section, path in zip(document.sections, paths, strict=True):
                    output = challenge.leaks.at_path(received, path)
                    hidden = challenge.leaks.positions(
                        challenge.trace_masks(section.text, output, shield)
                    )
                    coverage.extend(
                        len(set(range(mark.start, mark.end)) & hidden)
                        for mark in section.marks
                    )
                    assert shield.restore(output).text == section.text, document.id
                assert coverage == row["masked_characters"], document.id
                echo = "\n".join(
                    challenge.leaks.at_path(received, path) for path in paths
                )
                echoed = (
                    {
                        "output": [
                            {
                                "type": "message",
                                "role": "assistant",
                                "content": [{"type": "output_text", "text": echo}],
                            }
                        ]
                    }
                    if api == "openai"
                    else {"content": [{"type": "text", "text": echo}]}
                )
                upstream.replies.append(
                    (200, "application/json", [json.dumps(echoed).encode()])
                )
                response, restored = call(gateway, "POST", route, body, headers=headers)
                assert response.status == 200, document.id
                decoded = json.loads(restored)
                output = (
                    decoded["output"][0]["content"][0]["text"]
                    if api == "openai"
                    else decoded["content"][0]["text"]
                )
                assert output == "\n".join(
                    section.text for section in document.sections
                ), document.id
    finally:
        upstream.close()

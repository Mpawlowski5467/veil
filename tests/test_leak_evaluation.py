"""Check the evaluator's accounting and verify its predictions on real local HTTP."""

import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from test_gateway_server import FakeAPI, call
from veil import LiteralPlaceholderDetector, RegexDetector, Shield
from veil.gateway import (
    Gateway,
    MemoryLedger,
    RequestMasker,
    ResponsesRequestMasker,
    Session,
    Sessions,
)

SPEC = importlib.util.spec_from_file_location(
    "veil_leak_evaluation",
    Path(__file__).resolve().parents[1] / "benchmarks" / "leaks.py",
)
leaks = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = leaks
SPEC.loader.exec_module(leaks)


@pytest.fixture(scope="module")
def report():
    return leaks.evaluate()


def document(text, *, surface="prompt"):
    return leaks.Document("test", "prose", "en", (leaks.unpack(surface, text),))


def test_annotations_preserve_offsets_newlines_and_unicode():
    section = leaks.unpack(
        "prompt", 'π [[PASSWORD|a\\"b\n🦊]] + [[EMAIL|a@example.org]]'
    )
    assert section.text == 'π a\\"b\n🦊 + a@example.org'
    assert section.marks == (leaks.Mark("PASSWORD", 2, 8), leaks.Mark("EMAIL", 11, 24))


@pytest.mark.parametrize(
    "text", ["[[PASSWORD|]]", "[[oops|value]]", "[[PASSWORD|outer [[TOKEN|inner]]]]"]
)
def test_malformed_annotations_are_not_silently_dropped(text):
    with pytest.raises(ValueError, match="annotation"):
        leaks.unpack("prompt", text)


@pytest.mark.parametrize(
    "mutation", ["duplicate", "unknown-field", "empty", "invalid-surface"]
)
def test_corpus_validation(tmp_path, mutation):
    source = [
        {
            "id": "one",
            "format": "prose",
            "language": "en",
            "sections": [{"surface": "prompt", "text": "hello"}],
        }
    ]
    if mutation == "duplicate":
        source *= 2
    elif mutation == "unknown-field":
        source[0]["skip"] = True
    elif mutation == "empty":
        source = []
    else:
        source[0]["sections"][0]["surface"] = "attachment"
    corpus = tmp_path / "corpus.json"
    corpus.write_text(json.dumps(source), encoding="utf-8")
    with pytest.raises(ValueError, match="corpus"):
        leaks.load_corpus(corpus)


def test_partial_mask_is_still_exposed_even_when_full_value_disappears():
    doc = document(
        "My password is '[[PASSWORD|mira@example.org plus fictional words]]'"
    )
    row = leaks.measure(doc, "openai", False)
    assert row["masked_characters"] == [16]
    assert row["exposed_annotations"] == 1
    assert row["status"] == "ready"
    summary = leaks.summarize([doc], [row])["total"]
    assert summary["partially_masked"] == 1
    assert summary["fully_masked"] == 0
    assert summary["exposed_in_ready_requests"] == 1


def test_review_is_not_credited_as_a_mask_or_an_automatic_confirmation():
    doc = document("My password is '[[PASSWORD|fictional four word phrase]]'")
    row = leaks.measure(doc, "openai", True)
    assert row["masked_characters"] == [0]
    assert row["review_characters"] == [26]
    assert row["status"] == "held"
    summary = leaks.summarize([doc], [row])["total"]
    assert summary["fully_masked"] == 0
    assert summary["exposed_in_ready_requests"] == 0
    assert summary["fully_flagged_for_review"] == 1


def test_false_positive_and_literal_placeholder_accounting():
    row = leaks.measure(
        document("password = settings.database_password"), "openai", False
    )
    assert row["exact_fp"] == 1
    assert row["overmasked_characters"] == len("settings.database_password")
    literal = leaks.measure(
        document("See [PASSWORD_1] in the example."), "openai", True
    )
    assert literal["exact_fp"] == literal["overmasked_characters"] == 0
    assert literal["restored"] is True


def test_annotations_never_teach_the_detector_the_answer():
    labelled = leaks.measure(document("[[PASSWORD|orchard-sun]]"), "openai", True)
    plain = leaks.measure(document("orchard-sun"), "openai", True)
    assert labelled["masked_characters"] == [0]
    assert labelled["review_characters"] == [0]
    assert labelled["status"] == plain["status"] == "ready"
    assert labelled["exact_tp"] == plain["exact_tp"] == 0


def test_trace_rejects_unexplained_mutation_and_unknown_placeholder():
    shield = Shield()
    with pytest.raises(ValueError, match="trace"):
        leaks.trace_masks("original", "changed", shield)
    with pytest.raises(ValueError, match="placeholder"):
        leaks.trace_masks("original", "[PASSWORD_1]", shield)


def test_corpus_baseline(report):
    baseline = json.loads(leaks.BASELINE.read_text(encoding="utf-8"))
    assert not leaks.regressions(report, baseline)
    assert all(
        row["restored"] for run in report["runs"].values() for row in run["cases"]
    )


def test_original_workflow_corpus_has_no_exposed_ready_requests_with_review(report):
    for api in ("anthropic", "openai"):
        run = report["runs"][f"{api}/review"]
        assert run["total"]["exposed_in_ready_requests"] == 0
        # Credit only masks or findings that cover the actual annotation;
        # an incidental hold for an unrelated value is not enough.
        for document, row in zip(leaks.load_corpus(), run["cases"], strict=True):
            lengths = [
                mark.end - mark.start
                for section in document.sections
                for mark in section.marks
            ]
            assert row["masked_or_flagged_characters"] == lengths, document.id


def test_windows_checkout_line_endings_do_not_change_baseline(tmp_path, report):
    corpus = tmp_path / "crlf.json"
    corpus.write_bytes(
        leaks.CORPUS.read_text(encoding="utf-8").replace("\n", "\r\n").encode("utf-8")
    )
    assert leaks.snapshot(leaks.evaluate(corpus)) == leaks.snapshot(report)


@pytest.mark.parametrize(
    ("metric", "value"),
    [
        ("masked_characters", [0, 0]),
        ("masked_or_flagged_characters", [0, 0]),
        ("exact_fp", 99),
        ("exact_fn", 99),
        ("overmasked_characters", 99),
        ("review_harmless_characters", 99),
        ("restored", False),
        ("status", "refused"),
    ],
)
def test_regression_check_detects_worse_individual_cases(report, metric, value):
    changed = copy.deepcopy(report)
    changed["runs"]["openai/defaults"]["cases"][0][metric] = value
    assert leaks.regressions(changed, leaks.snapshot(report))


def test_baseline_cannot_hide_deleted_or_relabelled_documents(report):
    baseline = leaks.snapshot(report)
    baseline["corpus_sha256"] = "changed"
    assert leaks.regressions(report, baseline)
    baseline = copy.deepcopy(leaks.snapshot(report))
    baseline["runs"]["openai/defaults"].pop()
    assert leaks.regressions(report, baseline)


def test_removed_review_hold_must_not_expose_a_miss(report):
    changed = copy.deepcopy(report)
    row = next(
        row
        for row in changed["runs"]["openai/review"]["cases"]
        if row["id"] == "shell-command-password"
    )
    row["status"] = "ready"
    assert any(
        "hold removed" in reason
        for reason in leaks.regressions(changed, leaks.snapshot(report))
    )


def test_automatic_mask_can_replace_review_without_being_a_regression(report):
    changed = copy.deepcopy(report)
    row = next(
        row
        for row in changed["runs"]["openai/review"]["cases"]
        if row["id"] == "shell-command-password"
    )
    row.update(
        masked_characters=[28],
        review_characters=[0],
        status="ready",
        exposed_annotations=0,
        exact_tp=1,
        exact_fn=0,
        review_findings=0,
    )
    assert not leaks.regressions(changed, leaks.snapshot(report))


def test_new_hold_on_harmless_input_fails_regression_check(report):
    changed = copy.deepcopy(report)
    row = next(
        row
        for row in changed["runs"]["openai/review"]["cases"]
        if row["id"] == "ordinary-prose"
    )
    row["status"] = "held"
    assert any(
        "harmless input" in reason
        for reason in leaks.regressions(changed, leaks.snapshot(report))
    )


def test_report_never_contains_fixture_text_or_private_values(report):
    serialized = json.dumps(report)
    for doc in leaks.load_corpus():
        for section in doc.sections:
            for mark in section.marks:
                assert section.text[mark.start : mark.end] not in serialized


@pytest.mark.parametrize("api", ["anthropic", "openai"])
@pytest.mark.parametrize("review", [False, True], ids=["defaults", "review"])
def test_entire_corpus_matches_actual_local_gateway_egress(report, api, review):
    """A held prediction must produce zero requests at the fake provider."""

    def make_session(_session_id):
        regex = RegexDetector()
        shield = Shield(
            detectors=[LiteralPlaceholderDetector(regex.entity_types), regex],
            redact_warnings=True,
        )
        ledger = MemoryLedger()
        cls = ResponsesRequestMasker if api == "openai" else RequestMasker
        return Session(
            shield, ledger, cls(shield, ledger, secret_review=review, note=None)
        )

    upstream = FakeAPI()
    route = "/v1/responses" if api == "openai" else "/v1/messages"
    reply = {"output": []} if api == "openai" else {"content": []}
    try:
        with (
            Sessions(make_session=make_session) as sessions,
            Gateway(
                sessions, api=api, upstream=upstream.host, secure=False, keepalive=0.05
            ) as gateway,
        ):
            mode = "review" if review else "defaults"
            rows = report["runs"][f"{api}/{mode}"]["cases"]
            for doc, row in zip(leaks.load_corpus(), rows, strict=True):
                body, paths = leaks.request(doc, api)
                count = len(upstream.received)
                upstream.replies[:] = [
                    (200, "application/json", [json.dumps(reply).encode()])
                ]
                headers = {
                    "thread-id"
                    if api == "openai"
                    else "x-claude-code-session-id": doc.id
                }
                response, _ = call(
                    gateway,
                    "POST",
                    route,
                    body,
                    headers=headers,
                )
                if row["status"] == "held":
                    assert response.status == 403, doc.id
                    assert len(upstream.received) == count, doc.id
                    continue
                assert row["status"] == "ready", doc.id
                assert response.status == 200, doc.id
                assert len(upstream.received) == count + 1, doc.id
                received = json.loads(upstream.received[-1][3])
                shield = sessions.get(doc.id).shield
                coverage = []
                for section, path in zip(doc.sections, paths, strict=True):
                    text = leaks.at_path(received, path)
                    spans = leaks.trace_masks(section.text, text, shield)
                    hidden = leaks.positions(spans)
                    coverage.extend(
                        len(set(range(m.start, m.end)) & hidden) for m in section.marks
                    )
                    assert shield.restore(text).text == section.text, doc.id
                assert coverage == row["masked_characters"], doc.id
                # Echo the actually observed masked text on an identical retry.
                # This checks the real response restorer as well as the request.
                echo = "\n".join(leaks.at_path(received, path) for path in paths)
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
                assert response.status == 200, doc.id
                decoded = json.loads(restored)
                output = (
                    decoded["output"][0]["content"][0]["text"]
                    if api == "openai"
                    else decoded["content"][0]["text"]
                )
                assert output == "\n".join(section.text for section in doc.sections), (
                    doc.id
                )
    finally:
        upstream.close()

"""Explicit fragment labels and source-bounded header review, using fictional data."""

import json
import time

import pytest

from test_gateway_server import FakeAPI, call
from veil import Shield
from veil.gateway import (
    Gateway,
    MemoryLedger,
    RequestMasker,
    Settings,
    open_sessions,
    prepare_data_dir,
)
from veil.gateway.openai_request import ResponsesRequestMasker
from veil.secret_review import ReviewError, candidates, request_candidates

CUE = "The credential was split into pieces for storage."


def values(text, **kwargs):
    return [finding.value for finding in candidates(text, **kwargs)]


@pytest.mark.parametrize(
    ("label", "value"),
    [
        ("First part", "birch731"),
        ("SECOND PIECE", "maple914"),
        ("third fragment", "pine_82"),
        ("fourth part", "oak/739+"),
        ("last part", "bark=ab=="),
        ("next piece", "teal-82"),
        ("part 1", "willow48"),
        ("piece 23", "cedar31"),
        ("fragment A", "alder62"),
        ("part", "lime42"),
    ],
)
def test_explicit_fragment_labels_have_complete_source_boundaries(label, value):
    # A quoted fragment can contain symbols outside the bare token class.
    spelling = f'"{value}"' if "=" in value[:-2] else value
    source = f"{CUE} {label}: {spelling}; archive: public."
    found = candidates(source)
    assert [(f.value, f.kind) for f in found] == [(value, "CREDENTIAL")]
    assert value not in repr(found)
    assert Shield().mask(source).text == source  # A hold is not an automatic mask.


@pytest.mark.parametrize(
    "credential", ["password", "API key", "access token", "secret", "credentials"]
)
def test_fragment_cue_is_not_tied_to_one_corpus_word(credential):
    source = (
        f"The {credential} has two fragments.\npiece A = amber861; piece B = silver492."
    )
    assert values(source) == ["amber861", "silver492"]


@pytest.mark.parametrize(
    ("spelling", "expected"),
    [
        ('"x"', "x"),
        ("'two fictional words'", "two fictional words"),
        ('"fictional\\"birch\\n73"', 'fictional\\"birch\\n73'),
        ("`settings.first_piece`", "settings.first_piece"),
        ('"unfinished fictional phrase', "unfinished fictional phrase"),
    ],
)
def test_quoted_fragments_preserve_escapes_and_truncated_line(spelling, expected):
    assert values(f"{CUE}\nFirst part: {spelling}") == [expected]


def test_multiline_values_are_separate_fragments_and_not_decoded():
    source = f'{CUE}\r\nFirst part: "fir\\n73";\r\nsecond part: ash929.'
    assert values(source) == [r"fir\n73", "ash929"]
    # An actual newline ends this bounded phrase; arbitrary multiline quoted
    # fragment expressions are outside the rule, rather than joined or decoded.
    source = f'{CUE}\nFirst part: "fir73\nsecond part: ash929'
    assert values(source) == ["fir73", "ash929"]


@pytest.mark.parametrize(
    "source",
    [
        "The chapter was split. First part: introduction; second part: examples.",
        "The credential needs rotation. First part: introduction.",
        "Split the source file into pieces. First part: settings.token.",
        "The credential was revised.\nThe chapter was split. First part: intro42.",
        CUE + " First part describes the login flow.",
        CUE + " First part: render_view().",
        CUE + " First part: settings.first_piece.",
        CUE + " First part: get_piece(index).",
        CUE + " First part: data[0].",
        CUE + " First part: ${FIRST_PART}; second part: $NEXT_PART.",
        CUE + " First part: [TOKEN_1]; second part: <provided-locally>.",
        CUE + " First part: the chapter introduction.",
        CUE + " First part birch731; second part maple914.",
        CUE + " First part: birch731 second part: maple914.",
        CUE + " First part: abc; second part: 12.",
    ],
)
def test_fragment_negatives_keep_code_references_and_descriptions_readable(source):
    assert not candidates(source)


def test_request_scoping_masked_cues_and_partial_fragment_values():
    source = "First part: hazel483; second part: beech725."
    assert not request_candidates([(source, source)])
    assert not request_candidates([(CUE, " " * len(CUE)), (source, source)])
    protected = source.replace("hazel483", " " * 8)
    findings = request_candidates([(CUE, CUE), (source, protected)])
    assert [f.value for f in findings] == ["beech725"]
    partially_protected = source.replace("hazel", " " * 5).replace("beech725", " " * 8)
    findings = request_candidates([(CUE, CUE), (source, partially_protected)])
    assert [f.value for f in findings] == ["hazel483"]
    assert not request_candidates([(source, source)])


@pytest.mark.parametrize("quoted", [False, True])
def test_fragment_limits_refuse_whole_oversized_values_without_echo(quoted):
    value = "x" * 16_385
    spelling = f'"{value}"' if quoted else value
    with pytest.raises(ReviewError) as error:
        candidates(f"{CUE}\nFirst part: {spelling}")
    assert value[:50] not in str(error.value)
    assert values(f"{CUE}\nFirst part: {'x' * 16_384}") == ["x" * 16_384]


def test_fragment_count_limit_preserves_safe_failure():
    with pytest.raises(ReviewError, match="too many or oversized findings"):
        candidates(
            CUE + "\n" + "; ".join(f"part {i}: birch{i:04d}" for i in range(101))
        )


def test_repeated_ambiguous_fragment_labels_do_not_rescan_whole_line():
    source = CUE + "\n" + "first part: descriptive words " * 12_000
    started = time.monotonic()
    assert not candidates(source)
    assert time.monotonic() - started < 5


def body(api, source):
    if api == "openai":
        return {"model": "test", "input": source}
    return {
        "model": "test",
        "max_tokens": 20,
        "messages": [{"role": "user", "content": source}],
    }


def text(api, request):
    return request["input"] if api == "openai" else request["messages"][0]["content"]


@pytest.mark.parametrize("api", ["anthropic", "openai"])
@pytest.mark.parametrize("option", ["-H ", "-H", "--header ", "--header="])
@pytest.mark.parametrize("quote", ['"', "'"])
def test_quoted_header_boundaries_remove_only_redundant_hold(api, option, quote):
    value = "fictional-alder-842"
    source = (
        f"curl {option}{quote}X-Api-Key: {value}{quote} https://example.test/health\n"
    )
    shield = Shield()
    cls = ResponsesRequestMasker if api == "openai" else RequestMasker
    masker = cls(shield, MemoryLedger(), secret_review=True, note=None)
    masked = text(api, masker.mask(body(api, source)))
    assert masked == source.replace(value, "[API_KEY_1]")
    assert not masker.review_findings()
    assert shield.restore(masked).text == source


@pytest.mark.parametrize(
    "source",
    [
        'curl -H "X-Api-Key: fictional-alder-842" https://example.test/health',
        'curl --header="X-Api-Key: fictional-alder-842"',
    ],
)
def test_header_value_still_held_if_all_or_part_remains_visible(source):
    value = "fictional-alder-842"
    assert values(source) == [value]
    partially_protected = source.replace(
        "fictional-alder", " " * len("fictional-alder")
    )
    assert values(source, visible=partially_protected) == [value]
    assert not values(source, visible=source.replace(value, " " * len(value)))


@pytest.mark.parametrize("separator", ["\u00a0", "\v", "\f", "\r"])
def test_non_shell_whitespace_does_not_waive_private_header_suffix(separator):
    source = (
        'curl -H "X-Api-Key: fictional-alder-842"'
        + separator
        + "fictional-private-suffix"
    )
    shield = Shield()
    masker = RequestMasker(shield, MemoryLedger(), secret_review=True, note=None)
    masked = text("anthropic", masker.mask(body("anthropic", source)))
    assert "fictional-private-suffix" in masked
    assert any(
        "fictional-private-suffix" in finding.value
        for finding in masker.review_findings()
    )


@pytest.mark.parametrize("separator", [" ", "\t", "\n", "\r\n"])
def test_supported_shell_header_separators_do_not_add_redundant_hold(separator):
    source = (
        'curl -H "X-Api-Key: fictional-alder-842"'
        + separator
        + "https://example.test/health"
    )
    shield = Shield()
    masker = RequestMasker(shield, MemoryLedger(), secret_review=True, note=None)
    masked = text("anthropic", masker.mask(body("anthropic", source)))
    assert masked == source.replace("fictional-alder-842", "[API_KEY_1]")
    assert not masker.review_findings()
    assert shield.restore(masked).text == source


@pytest.mark.parametrize(
    "prefix",
    [
        "  curl ",
        "\t curl -sS -X POST ",
        "\r\n\t curl --connect-timeout 3 ",
        "\n curl https://example.test/health ",
    ],
)
def test_simple_initial_curl_words_establish_an_unquoted_header_option(prefix):
    source = prefix + '-H "X-Api-Key: fictional-alder-842" https://example.test/health'
    shield = Shield()
    masker = RequestMasker(shield, MemoryLedger(), secret_review=True, note=None)
    masked = text("anthropic", masker.mask(body("anthropic", source)))
    assert masked == source.replace("fictional-alder-842", "[API_KEY_1]")
    assert not masker.review_findings()
    assert shield.restore(masked).text == source


@pytest.mark.parametrize(
    ("prefix", "suffix"),
    [
        ("curl --data 'prefix ", "'"),
        ("printf '%s\\n' 'prefix ", "'"),
        ("echo '\ncurl ", "\n'"),
        ("curl escaped\\ ", ""),
        ('curl "earlier argument" ', ""),
        ("curl $(printf safe) ", ""),
        ("curl `printf safe` ", ""),
        ("echo public; curl ", ""),
        ("curl " + "a" * 256 + " ", ""),
        ("public note\ncurl ", ""),
        ("curl %CURL_OPTIONS% ", ""),
    ],
)
def test_nested_escaped_or_unproven_prefix_cannot_waive_visible_suffix(prefix, suffix):
    source = (
        prefix + '-H "X-Api-Key: fictional-alder-842" fictional-private-suffix' + suffix
    )
    shield = Shield()
    masker = RequestMasker(shield, MemoryLedger(), secret_review=True, note=None)
    masked = text("anthropic", masker.mask(body("anthropic", source)))
    assert "fictional-private-suffix" in masked
    assert any(
        "fictional-private-suffix" in finding.value
        for finding in masker.review_findings()
    )


@pytest.mark.parametrize(
    "source",
    [
        'curl -H "X-Api-Key: fictional-alder-842\\" remaining-private-letters" url',
        "X-Api-Key=fictional-alder-842 remaining-private-letters",
        'example="X-Api-Key: fictional-alder-842" remaining-private-letters',
    ],
)
def test_ambiguous_header_syntax_never_waives_visible_suffix(source):
    visible = source.replace("fictional-alder-842", " " * 19)
    found = candidates(source, visible=visible)
    assert found
    assert any("private" in f.value for f in found)


@pytest.mark.parametrize(
    "source",
    [
        'curl -H "X-Api-Key: fictional-alder-842"unquoted-private-suffix url',
        "curl -H \"X-Api-Key: fictional-alder-842' remaining-private-letters",
        'curl -H "X-Api-Key: fictional-alder-842 remaining-private-letters',
    ],
)
def test_unbounded_header_values_keep_full_conservative_automatic_coverage(source):
    shield = Shield()
    masker = RequestMasker(shield, MemoryLedger(), secret_review=True, note=None)
    masked = text("anthropic", masker.mask(body("anthropic", source)))
    assert masked == 'curl -H "X-Api-Key: [API_KEY_1]'
    assert shield.restore(masked).text == source


@pytest.mark.parametrize("api", ["anthropic", "openai"])
def test_gateway_fragment_decisions_and_header_ready_restore_exactly(tmp_path, api):
    upstream = FakeAPI()
    source = CUE + ' First part: "hazel\\n483"; second part: beech725.'
    fragment_values = [r"hazel\n483", "beech725"]
    expected = source.replace(fragment_values[0], "[CREDENTIAL_1]").replace(
        fragment_values[1], "[CREDENTIAL_2]"
    )
    route = "/v1/responses" if api == "openai" else "/v1/messages"
    headers = {
        "thread-id" if api == "openai" else "x-claude-code-session-id": "fragments"
    }
    data = prepare_data_dir(tmp_path / "data")
    try:
        with (
            open_sessions(
                data,
                Settings(identity=False, note=False, secret_review=True),
                {},
                api=api,
            ) as sessions,
            Gateway(sessions, api=api, upstream=upstream.host, secure=False) as gateway,
        ):
            response, raw = call(
                gateway, "POST", route, body(api, source), headers=headers
            )
            assert response.status == 403
            assert not upstream.received
            assert all(value.encode() not in raw for value in fragment_values)
            review = gateway.reviews.report()["reviews"][0]
            assert [f["value"] for f in review["findings"]] == fragment_values
            for finding in review["findings"]:
                gateway.reviews.decide(review["id"], finding["index"], "CREDENTIAL")
            reply = (
                {
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": expected}],
                        }
                    ]
                }
                if api == "openai"
                else {"content": [{"type": "text", "text": expected}]}
            )
            upstream.replies.append(
                (200, "application/json", [json.dumps(reply).encode()])
            )
            response, restored = call(
                gateway, "POST", route, body(api, source), headers=headers
            )
            assert response.status == 200
            assert len(upstream.received) == 1
            sent = text(api, json.loads(upstream.received[0][3]))
            assert sent == expected
            assert all(value not in sent for value in fragment_values)
            decoded = json.loads(restored)
            assert (
                decoded["output"][0]["content"][0]["text"]
                if api == "openai"
                else decoded["content"][0]["text"]
            ) == source
            header_source = (
                'curl -H "X-Api-Key: fictional-alder-842" https://example.test/health'
            )
            header_expected = header_source.replace(
                "fictional-alder-842", "[API_KEY_1]"
            )
            if api == "openai":
                reply["output"][0]["content"][0]["text"] = header_expected
            else:
                reply["content"][0]["text"] = header_expected
            upstream.replies.append(
                (200, "application/json", [json.dumps(reply).encode()])
            )
            response, restored = call(
                gateway, "POST", route, body(api, header_source), headers=headers
            )
            assert response.status == 200
            assert len(upstream.received) == 2
            sent = text(api, json.loads(upstream.received[1][3]))
            assert sent == header_expected
            assert sessions.get("fragments").shield.restore(sent).text == header_source
            decoded = json.loads(restored)
            assert (
                decoded["output"][0]["content"][0]["text"]
                if api == "openai"
                else decoded["content"][0]["text"]
            ) == header_source
    finally:
        upstream.close()

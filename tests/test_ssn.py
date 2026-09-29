"""US SSN detection: explicit formats, lookalikes, and lossless restoration."""

import pytest

from veil import Shield
from veil.detectors import RegexDetector
from veil.gateway import Settings, open_sessions


@pytest.mark.parametrize(
    ("text", "number"),
    [
        ("SSN: 123-45-6789.", "123-45-6789"),
        ("(123-45-6789)", "123-45-6789"),
        ("申請123-45-6789です", "123-45-6789"),
        ("123\u201145\u20116789", "123\u201145\u20116789"),
        ("123\u201345\u20136789", "123\u201345\u20136789"),
        ("ssn=123456789", "123456789"),
        ("SSN # 123456789", "123456789"),
        ("SSN 123 45 6789", "123 45 6789"),
        ("Social Security number: 123 45 6789", "123 45 6789"),
        ("social security: 123456789", "123456789"),
        ("SSN:\t123\u00a045\u00a06789", "123\u00a045\u00a06789"),
        ("SSN: 123\u202f45\u202f6789", "123\u202f45\u202f6789"),
        ("SSN: 123\u200945\u20096789", "123\u200945\u20096789"),
        ("001-01-0001", "001-01-0001"),
        ("899-99-9999", "899-99-9999"),
    ],
)
def test_supported_formats_preserve_labels_offsets_and_spelling(text, number):
    [span] = RegexDetector().detect(text)
    assert span.entity_type == "SSN"
    assert span.value == number == text[span.start : span.end]
    shield = Shield()
    masked = shield.mask(text)
    assert masked.text == text.replace(number, "[SSN_1]")
    assert shield.restore(masked.text).text == text


@pytest.mark.parametrize(
    "text",
    [
        "000-12-3456",
        "666-12-3456",
        "900-12-3456",
        "999-12-3456",
        "123-00-6789",
        "123-45-0000",
        "SSN: 000123456",
        "SSN: 666123456",
        "SSN: 999 12 3456",
        "SSN: 123 00 6789",
        "SSN: 123450000",
        "Order 123456789",
        "Routing number: 123456789",
        "123 45 6789",
        "ISBN 123456789",
        "USSN: 123456789",
        "SSN123456789",
        "SSN:\n123456789",
        "2026-09-29",
        "Version 123.45.6789",
        "123-45-67890",
        "1123-45-6789",
        "12-123-45-6789",
        "123-45-6789-12",
        "v123-45-6789",
        "123-45-6789x",
        "123-45\u20136789",  # mixed separators
        "SSN: 123 45\u00a06789",
        "SSN: 1234567890",
        "SSN: 123456789a",
        "\u0661123-45-6789",  # embedded in a Unicode digit run
        "123-45-6789\u0661",
        "SSN: １２３４５６７８９",  # noqa: RUF001 - unsupported full-width digits
    ],
)
def test_lookalikes_and_invalid_groups_are_not_ssns(text):
    assert [s for s in RegexDetector().detect(text) if s.entity_type == "SSN"] == []


def test_phone_number_is_not_an_ssn():
    [span] = RegexDetector().detect("+123-45-6789")
    assert span.entity_type == "PHONE"


def test_gateway_ssns_and_literal_placeholder_restore_independently(tmp_path):
    with open_sessions(tmp_path, Settings(), {}) as sessions:
        shield = sessions.get("literal-test").shield
        source = "123-45-6789; SSN 123456789; template [SSN_1]"
        masked = shield.mask(source).text
        assert masked == "[SSN_1]; SSN [SSN_2]; template [LITERAL_1]"
        assert shield.restore(masked).text == source


def test_custom_ssn_replaces_both_builtin_rules_and_can_keep_demo_behavior():
    detector = RegexDetector(custom_patterns={"SSN": r"\b000-\d{2}-\d{4}\b"})
    spans = detector.detect("123-45-6789 SSN: 123456789 and 000-12-3456")
    assert [(s.entity_type, s.value) for s in spans] == [("SSN", "000-12-3456")]
    assert RegexDetector(include_builtins=False).detect("123-45-6789") == []


@pytest.mark.parametrize("api", ["anthropic", "openai"])
def test_gateway_masks_ssns_and_restores_from_persisted_session(tmp_path, api):
    source = "Jane Doe; 123-45-6789; jane@example.com"
    body = (
        {"input": [{"role": "user", "content": source}]}
        if api == "openai"
        else {"messages": [{"role": "user", "content": source}]}
    )
    settings = Settings(entities={"PERSON": ("Jane Doe",)}, identity=False, note=False)
    with open_sessions(tmp_path, settings, {}, api=api) as sessions:
        session = sessions.get("ssn-test")
        masked = session.masker.mask(body)
        key = "input" if api == "openai" else "messages"
        assert masked[key][0]["content"] == "[PERSON_1]; [SSN_1]; [EMAIL_1]"
    with open_sessions(tmp_path, settings, {}, api=api) as sessions:
        assert sessions.get("ssn-test").shield.restore("[SSN_1]").text == "123-45-6789"

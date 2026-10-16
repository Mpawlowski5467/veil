"""Local recognition offsets, integration, and failures without remote models."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from veil import Shield, cli
from veil.detectors.names import LocalNameDetector
from veil.detectors.regex import RegexDetector
from veil.gateway.config import Settings, SettingsError, parse_settings
from veil.gateway.store import shield_factory
from veil.names_cli import configure_names


class Pipeline:
    max_length = 1000

    def __init__(self):
        self.pipe_names = ["ner", "tagger"]
        self.calls = []
        self.disabled = []
        self.fail = False

    def disable_pipe(self, name):
        self.disabled.append(name)

    def __call__(self, text):
        self.calls.append(text)
        if self.fail:
            raise RuntimeError("private input must never appear in diagnostics")
        entities = []
        for value, label in (
            ("José Silva", "PER"),
            ("Acme", "ORG"),
            ("Paris", "GPE"),
            ("Friday", "DATE"),
        ):
            if value in text:
                start = text.index(value)
                entities.append(
                    SimpleNamespace(
                        start_char=start,
                        end_char=start + len(value),
                        text=value,
                        label_=label,
                    )
                )
        return SimpleNamespace(ents=entities)


@pytest.fixture
def local_model(tmp_path, monkeypatch):
    model = tmp_path / "model"
    model.mkdir()
    pipeline = Pipeline()
    loaded = []

    def load(path):
        assert Path(path).is_dir()
        loaded.append(path)
        return pipeline

    monkeypatch.setitem(sys.modules, "spacy", SimpleNamespace(load=load))
    return model, pipeline, loaded


def test_unicode_names_mask_and_restore_with_existing_rules(local_model):
    model, pipeline, _ = local_model
    shield = Shield(detectors=[RegexDetector(), LocalNameDetector(model)])
    text = "🙂 José Silva works at Acme in Paris on Friday; email j@example.com."
    masked = shield.mask(text)
    assert "José Silva" not in masked.text
    assert "Acme" not in masked.text
    assert "Paris" not in masked.text
    assert "j@example.com" not in masked.text
    assert "Friday" in masked.text
    assert {entity.entity_type for entity in masked.entities} == {
        "PERSON",
        "ORGANIZATION",
        "LOCATION",
        "EMAIL",
    }
    assert shield.restore(masked.text).text == text
    assert pipeline.disabled == ["tagger"]


def test_model_shared_across_sessions_but_mappings_are_isolated(local_model, tmp_path):
    model, _, loaded = local_model
    factory = shield_factory(
        Settings(local_names_model=str(model)), tmp_path / "vault.db", {}
    )
    first, second = factory("one"), factory("two")
    try:
        masked = first.mask("José Silva")
        assert second.vault.items() == []
        assert first.restore(masked.text).text == "José Silva"
        literal = second.mask("[PERSON_1]")
        assert literal.text != "[PERSON_1]"
        assert second.restore(literal.text).text == "[PERSON_1]"
        assert len(loaded) == 1
    finally:
        first.vault.close()
        second.vault.close()


def test_manual_registration_wins_equal_span_ties(local_model):
    model, _, _ = local_model
    shield = Shield(detectors=[LocalNameDetector(model)])
    shield.add_entity("Acme", "CUSTOM")
    assert shield.mask("Acme").text == "[CUSTOM_1]"


def test_missing_dependency_is_actionable(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "spacy", None)
    with pytest.raises(ValueError, match=r"veil\[names\]"):
        LocalNameDetector(tmp_path)


def test_invalid_model_does_not_replace_settings(local_model, tmp_path):
    model, _, _ = local_model
    data = tmp_path / "data"
    configure_names(data, model)
    original = (data / "config.json").read_bytes()
    with pytest.raises(SettingsError, match="local spaCy model"):
        configure_names(data, tmp_path / "missing")
    assert (data / "config.json").read_bytes() == original
    configure_names(data, None)
    assert json.loads((data / "config.json").read_text())["local_names_model"] is None


def test_model_without_ner_is_rejected(local_model):
    model, pipeline, _ = local_model
    pipeline.pipe_names = []
    with pytest.raises(ValueError, match="NER pipeline"):
        LocalNameDetector(model)


def test_inference_failure_and_oversized_input_fail_closed(local_model):
    model, pipeline, _ = local_model
    detector = LocalNameDetector(model)
    with pytest.raises(ValueError, match="no unmasked fallback"):
        detector.detect("x" * 1001)
    assert pipeline.calls == []
    pipeline.fail = True
    with pytest.raises(ValueError, match="no unmasked fallback") as error:
        detector.detect("José Silva")
    assert "José Silva" not in str(error.value)
    assert "private input" not in str(error.value)


def test_cli_failure_writes_no_source_text(local_model, tmp_path, monkeypatch, capsys):
    import io

    model, pipeline, _ = local_model
    data = tmp_path / "data"
    configure_names(data, model)
    pipeline.fail = True
    monkeypatch.setattr(sys, "stdin", io.StringIO("José Silva"))
    assert cli.main(["--data-dir", str(data), "mask", "--session", "test"]) == 2
    result = capsys.readouterr()
    assert result.out == ""
    assert "José Silva" not in result.err
    assert "no output written" in result.err


@pytest.mark.parametrize("value", [True, 1, [], {}, "", "relative-model"])
def test_settings_refuse_nonlocal_model_paths(value):
    with pytest.raises(SettingsError, match="local_names_model"):
        parse_settings(json.dumps({"local_names_model": value}), Path("config.json"))


def test_disabled_recognition_never_imports_spacy(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "spacy", None)
    shield = shield_factory(Settings(), tmp_path / "vault.db", {})("test")
    try:
        assert shield.mask("j@example.com").text == "[EMAIL_1]"
    finally:
        shield.vault.close()


def test_real_spacy_pipeline_round_trip_without_downloads(tmp_path, monkeypatch):
    """Exercise serialization and NER inference, not a detection-quality claim."""
    spacy = pytest.importorskip("spacy")
    from spacy.training import Example
    from spacy.util import fix_random_seed

    fix_random_seed(42)
    nlp = spacy.blank("en")
    ner = nlp.add_pipe("ner")
    ner.add_label("PERSON")
    text = "Hello Jane Example."
    examples = [
        Example.from_dict(nlp.make_doc(text), {"entities": [(6, 18, "PERSON")]})
    ]
    nlp.initialize(get_examples=lambda: examples)
    for _ in range(35):
        nlp.update(examples, drop=0)
    model = tmp_path / "model"
    nlp.to_disk(model)

    def refuse_network(*args, **kwargs):
        pytest.fail("local name recognition attempted network access")

    import socket

    monkeypatch.setattr(socket, "create_connection", refuse_network)
    shield = Shield(detectors=[LocalNameDetector(model)])
    masked = shield.mask(text)
    assert masked.text == "Hello [PERSON_1]."
    assert shield.restore(masked.text).text == text


@pytest.mark.parametrize("api", ["anthropic", "openai"])
def test_gateway_never_forwards_when_local_recognition_fails(
    local_model, tmp_path, api
):
    from test_gateway_server import FakeAPI, call
    from veil.gateway import Gateway, open_sessions
    from veil.gateway.config import prepare_data_dir

    model, pipeline, _ = local_model
    data = prepare_data_dir(tmp_path / "data")
    settings = Settings(identity=False, local_names_model=str(model))
    upstream = FakeAPI()
    try:
        with (
            open_sessions(data, settings, {}, api=api) as sessions,
            Gateway(sessions, api=api, upstream=upstream.host, secure=False) as gateway,
        ):
            pipeline.fail = True
            body = (
                {
                    "model": "claude-test",
                    "max_tokens": 32,
                    "messages": [{"role": "user", "content": "José Silva"}],
                }
                if api == "anthropic"
                else {
                    "model": "gpt-test",
                    "input": [
                        {
                            "role": "user",
                            "content": [{"type": "input_text", "text": "José Silva"}],
                        },
                    ],
                }
            )
            response, payload = call(
                gateway,
                path="/v1/messages" if api == "anthropic" else "/v1/responses",
                headers={"thread-id": "names-failure-test"} if api == "openai" else {},
                body=body,
            )
            assert response.status == 500
            assert upstream.received == []
            assert "José Silva" not in payload.decode()
            assert "private input" not in payload.decode()
    finally:
        upstream.close()

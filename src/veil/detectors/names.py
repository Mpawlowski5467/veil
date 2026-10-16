"""Optional name recognition with a locally installed spaCy pipeline."""

from __future__ import annotations

import importlib
import threading
from pathlib import Path
from typing import Any

from ..types import Span

NAME_TYPES = {"PERSON", "ORGANIZATION", "LOCATION"}
_LABELS = {
    "PERSON": "PERSON",
    "PER": "PERSON",
    "ORG": "ORGANIZATION",
    "GPE": "LOCATION",
    "LOC": "LOCATION",
}


class LocalNameDetector:
    """Recognize names locally; never download a model or use a remote service.

    Supply an absolute path to a trusted, installed spaCy pipeline directory.
    Models are executable software: only use pipelines from sources you trust.
    Recognition depends on the model and language and can miss or mislabel names.
    Inference failures raise an error instead of silently disabling detection.
    """

    def __init__(self, model: Path) -> None:
        """Load one local pipeline; only its NER dependencies are run."""
        if not model.is_absolute() or not model.is_dir():
            raise ValueError("select an absolute path to a local spaCy model folder")
        try:
            spacy = importlib.import_module("spacy")
        except ImportError:
            raise ValueError(
                "local name recognition needs the optional 'veil[names]' extra"
            ) from None
        try:
            self._nlp: Any = spacy.load(model)
            if "ner" not in self._nlp.pipe_names:
                raise ValueError
            # Keep dependencies (e.g. tok2vec/transformer) but omit unrelated work.
            for name in ("tagger", "parser", "lemmatizer", "attribute_ruler", "senter"):
                if name in self._nlp.pipe_names:
                    self._nlp.disable_pipe(name)
        except Exception:
            raise ValueError(
                "could not load the local NER pipeline; check its files and version"
            ) from None
        self._lock = threading.Lock()

    def detect(self, text: str) -> list[Span]:
        """Return recognized names, preserving exact Unicode character offsets."""
        if not text:
            return []
        try:
            with self._lock:
                # Refuse oversized input rather than truncating or creating
                # chunk boundaries that could silently split a private name.
                if len(text) > self._nlp.max_length:
                    raise ValueError
                document = self._nlp(text)
                found = []
                for entity in document.ents:
                    kind = _LABELS.get(entity.label_)
                    if kind is None:
                        continue
                    start, end = entity.start_char, entity.end_char
                    if not 0 <= start < end <= len(text):
                        raise ValueError
                    value = text[start:end]
                    if not value.strip() or value != entity.text:
                        raise ValueError
                    found.append(Span(start, end, value, kind, "local_names", 10))
                return found
        except Exception:
            raise ValueError(
                "local name recognition failed; no unmasked fallback is available"
            ) from None

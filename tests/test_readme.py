"""Run the documentation's Python examples so they cannot drift from the code."""

import doctest
import re
import warnings
from pathlib import Path

import pytest

README = Path(__file__).resolve().parent.parent / "README.md"
DOCUMENTS = [README, README.parent / "docs" / "python-guide.md"]
PYTHON_BLOCK_RE = re.compile(r"^```python\n(.*?)^```", re.DOTALL | re.MULTILINE)


def python_blocks(document):
    return PYTHON_BLOCK_RE.findall(document.read_text(encoding="utf-8"))


@pytest.mark.parametrize("document", DOCUMENTS, ids=lambda path: path.name)
def test_document_has_examples(document):
    minimum = 1 if document == README else 5
    assert len(python_blocks(document)) >= minimum


@pytest.mark.parametrize("document", DOCUMENTS, ids=lambda path: path.name)
def test_document_examples(document, capsys, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # examples may create files
    # Blocks share one namespace and run in order, like a reader following
    # along. Blocks with ">>>" are doctests (outputs are checked); the others
    # are scripts and must run without error.
    namespace: dict = {}
    parser = doctest.DocTestParser()
    runner = doctest.DocTestRunner(optionflags=doctest.ELLIPSIS)
    report: list[str] = []
    with warnings.catch_warnings():  # a block may change warning filters
        for number, block in enumerate(python_blocks(document), start=1):
            name = f"{document.name} python block {number}"
            if ">>>" in block:
                test = parser.get_doctest(block, namespace, name, str(document), 0)
                runner.run(test, out=report.append, clear_globs=False)
            else:
                exec(compile(block, name, "exec"), namespace)
    assert runner.failures == 0, "".join(report)
    if document == README:
        assert "Hi Jan Nowak, following up on the invoice..." in capsys.readouterr().out


def test_readme_names_the_tested_claude_code():
    from veil.gateway.compat import TESTED_CLAUDE_CODE

    assert f"tested with Claude Code {TESTED_CLAUDE_CODE}." in README.read_text(
        encoding="utf-8"
    )


def test_the_refusal_example_is_one_the_gateway_gives():
    # A newer Claude Code sends an opaque value under a field of a block that
    # the gateway has no rule for: the README shows the message it gets.
    from veil import MemoryVault, Shield
    from veil.gateway import (
        MemoryLedger,
        RequestMasker,
        UnsupportedRequestError,
        compat,
    )

    signed = {"signature": "QmFzZTY0IHNpZ25lZCBieSBhIG5ld2VyIENsYXVkZSBDb2Rl" * 2}
    block = {"type": "text", "text": "Notes", "attestation": signed}
    user = {"role": "user", "content": "hi"}
    assistant = {"role": "assistant", "content": "ok"}
    last = {"role": "user", "content": [{"type": "text", "text": "a"}, block]}
    body = {"messages": [user, assistant, user, assistant, last]}
    masker = RequestMasker(Shield(vault=MemoryVault()), MemoryLedger(), note=None)
    try:
        masker.mask(body)
    except UnsupportedRequestError as error:
        problems = error.problems
    else:
        raise AssertionError("not refused")
    newer = "2.1.290"
    assert compat.compare(newer) == 1, "pick a version newer than the tested one"
    expected = f"API Error: 400 {compat.refusal_message(problems, newer)}"
    assert expected in (README.parent / "docs/claude-code.md").read_text(
        encoding="utf-8"
    )

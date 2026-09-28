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
    assert len(python_blocks(document)) >= 5


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

"""Run the README's Python examples so the docs cannot drift from the code."""

import doctest
import re
import warnings
from pathlib import Path

README = Path(__file__).resolve().parent.parent / "README.md"
PYTHON_BLOCK_RE = re.compile(r"^```python\n(.*?)^```", re.DOTALL | re.MULTILINE)


def python_blocks():
    return PYTHON_BLOCK_RE.findall(README.read_text(encoding="utf-8"))


def test_readme_has_examples():
    assert len(python_blocks()) >= 5


def test_readme_examples(capsys, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # examples may create files
    # Blocks share one namespace and run in order, like a reader following
    # along. Blocks with ">>>" are doctests (outputs are checked); the others
    # are scripts and must run without error.
    namespace: dict = {}
    parser = doctest.DocTestParser()
    runner = doctest.DocTestRunner(optionflags=doctest.ELLIPSIS)
    report: list[str] = []
    with warnings.catch_warnings():  # a block may change warning filters
        for number, block in enumerate(python_blocks(), start=1):
            name = f"README.md python block {number}"
            if ">>>" in block:
                test = parser.get_doctest(block, namespace, name, str(README), 0)
                runner.run(test, out=report.append, clear_globs=False)
            else:
                exec(compile(block, name, "exec"), namespace)
    assert runner.failures == 0, "".join(report)
    assert "Hi Jan Nowak, following up on the invoice..." in capsys.readouterr().out


def test_readme_names_the_tested_claude_code():
    from veil.gateway.compat import TESTED_CLAUDE_CODE

    text = (Path(__file__).parent.parent / "README.md").read_text(encoding="utf-8")
    assert f"tested with Claude Code {TESTED_CLAUDE_CODE}." in text

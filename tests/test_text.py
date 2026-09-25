import pytest

from veil._text import contains_token, find_token, is_word_char


@pytest.mark.parametrize("ch", ["a", "Z", "ł", "ж", "7", "_", "́", "ा"])
def test_word_chars(ch):
    assert is_word_char(ch)


@pytest.mark.parametrize("ch", [" ", "-", "'", ".", "[", "\x00", "山", "さ", "김", "ก"])
def test_non_word_chars(ch):
    assert not is_word_char(ch)


def test_find_token_boundaries():
    assert list(find_token("Jan, January, Jan's (Jan)", "Jan")) == [0, 14, 21]


def test_find_token_only_guards_word_edges():
    assert list(find_token("xJan!", "Jan!")) == []
    assert list(find_token("Jan!!", "Jan!")) == [0]


def test_find_token_case_sensitive():
    assert list(find_token("jan", "Jan")) == []


def test_find_token_empty_value():
    assert list(find_token("anything", "")) == []


def test_contains_token():
    assert contains_token("hi Jan.", "Jan")
    assert not contains_token("January", "Jan")

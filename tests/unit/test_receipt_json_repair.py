"""Unit tests for repairing truncated or fenced JSON returned by vision models."""

from __future__ import annotations

import time

import pytest

from finance_ai.receipts.gateway import _last_complete_item_end, try_repair_json


def test_valid_json_object_is_returned_unchanged() -> None:
    assert try_repair_json('{"vendor": "Exito", "items": []}') == {
        "vendor": "Exito",
        "items": [],
    }


@pytest.mark.parametrize(
    "text",
    [
        '```json\n{"vendor": "Exito"}\n```',
        '```\n{"vendor": "Exito"}\n```',
        '  ```json   {"vendor": "Exito"}   ```  ',
        '```json\n{"vendor": "Exito"}',
    ],
)
def test_markdown_fences_are_stripped(text: str) -> None:
    assert try_repair_json(text) == {"vendor": "Exito"}


def test_truncated_items_list_keeps_only_complete_items() -> None:
    text = (
        '{"vendor": "Exito", "items": [{"name": "a", "price": 1}, '
        '{"name": "b", "price": 2}, {"name": "c", "pri'
    )

    assert try_repair_json(text) == {
        "vendor": "Exito",
        "items": [{"name": "a", "price": 1}, {"name": "b", "price": 2}],
    }


def test_truncated_items_list_with_newlines_between_items() -> None:
    text = '{"items": [\n{"name": "a"}\n,\n{"name": "b'

    assert try_repair_json(text) == {"items": [{"name": "a"}]}


def test_truncated_string_and_brackets_are_closed() -> None:
    assert try_repair_json('{"vendor": "Exi') == {"vendor": "Exi"}
    assert try_repair_json('{"vendor": "Exito", "items": [{"name": "a"},') == {
        "vendor": "Exito",
        "items": [{"name": "a"}],
    }


@pytest.mark.parametrize("text", ["[1, 2, 3]", '"just a string"', "[1, 2"])
def test_non_object_completion_is_rejected(text: str) -> None:
    with pytest.raises(ValueError):
        try_repair_json(text)


def test_unrecoverable_text_raises() -> None:
    with pytest.raises(ValueError):
        try_repair_json("not json at all")


@pytest.mark.parametrize(
    ("text", "expected_end"),
    [
        ('{"items": [{"a": 1}, {"b": 2', len('{"items": [{"a": 1}')),
        ('{"items":[{"a":1}  ,  {"b"', len('{"items":[{"a":1}')),
        ('{"items": [{"a": 1}]}', None),
        ('{"items": [{"a": 1},', None),
        ('{"vendor": "x"}, {"b": 2', None),
        ('{"items": [', None),
        ("", None),
    ],
)
def test_last_complete_item_end(text: str, expected_end: int | None) -> None:
    assert _last_complete_item_end(text) == expected_end


def test_pathological_inputs_do_not_backtrack() -> None:
    fenced = '```json\n{"a": 1}' + " " * 200_000 + "x" * 10
    many_braces = '{"items": [' + "{}," * 50_000 + '{"x"'

    started = time.perf_counter()
    with pytest.raises(ValueError):
        try_repair_json(fenced)
    assert try_repair_json(many_braces)["items"]
    assert time.perf_counter() - started < 2.0

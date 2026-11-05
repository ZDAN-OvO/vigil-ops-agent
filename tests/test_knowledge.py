"""Runbook 导入：字段类型不合格时拒绝，不产生半条知识。"""

import json

import pytest

from aiops.knowledge import load_runbooks

VALID = {
    "id": "rb-x",
    "version": 1,
    "title": "标题",
    "summary": "摘要",
    "applies_to": ["svc"],
    "symptoms": ["OOMKilled"],
    "steps": ["第一步"],
    "source": "tests",
}


def write(tmp_path, payload):
    path = tmp_path / "book.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_valid_entry_loads(tmp_path):
    [runbook] = load_runbooks(write(tmp_path, VALID))

    assert runbook.id == "rb-x" and runbook.steps == ("第一步",)


def test_steps_must_be_a_string_list(tmp_path):
    with pytest.raises(ValueError, match="steps"):
        load_runbooks(write(tmp_path, {**VALID, "steps": "DROP"}))


def test_version_must_be_positive_integer(tmp_path):
    with pytest.raises(ValueError, match="version"):
        load_runbooks(write(tmp_path, {**VALID, "version": 0}))


def test_missing_title_is_rejected(tmp_path):
    payload = {key: value for key, value in VALID.items() if key != "title"}

    with pytest.raises(ValueError, match="title"):
        load_runbooks(write(tmp_path, payload))

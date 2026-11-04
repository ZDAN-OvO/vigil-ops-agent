"""拒绝会污染证据或执行链路的歧义输入。"""

import pytest
from pydantic import ValidationError

from aiops.contracts import TimeRange, ToolResult


@pytest.mark.parametrize(
    ("start", "end"),
    [
        ("2026-11-02T10:00:00", "2026-11-02T11:00:00"),
        ("2026-11-02T10:00:00Z", "2026-11-02T10:00:00Z"),
        ("2026-11-02T11:00:00Z", "2026-11-02T10:00:00Z"),
    ],
)
def test_time_range_rejects_ambiguous_or_reversed_timestamps(start, end):
    with pytest.raises(ValidationError):
        TimeRange(start=start, end=end)


def tool_payload():
    return {
        "tool_call_id": "call-1",
        "source": "prometheus",
        "query": {"expression": "up"},
        "time_range": {"start": "2026-11-02T10:00:00Z", "end": "2026-11-02T11:00:00Z"},
        "collected_at": "2026-11-02T11:01:00Z",
    }


def test_empty_observation_is_distinct_from_failed_query():
    empty = ToolResult(**tool_payload(), data=[])
    failed = ToolResult(
        **tool_payload(), error={"code": "timeout", "message": "查询超时", "retryable": True}
    )
    assert empty.error is None
    assert empty.data == []
    assert failed.error.code == "timeout"
    assert failed.data is None


def test_query_error_cannot_be_disguised_as_successful_data():
    with pytest.raises(ValidationError):
        ToolResult(**tool_payload(), data=[], error={"code": "timeout", "message": "查询超时"})

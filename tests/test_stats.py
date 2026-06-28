"""Tests for the index.jsonl aggregator behind `python -m clawback.stats`."""

from __future__ import annotations

import json

from clawback.stats import format_summary, summarize_index


def _line(**fields) -> str:
    return json.dumps(fields)


def _sample_lines() -> list[str]:
    return [
        _line(seq=1, status=200, short_circuited="title-gen", bytes_unsent=1_000),
        _line(
            seq=2,
            status=200,
            transforms_applied=["reduce-main-system", "filter-tools"],
            bytes_removed=5_000,
            usage={"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 900},
        ),
        _line(
            seq=3,
            status=200,
            transforms_applied=["reduce-main-system"],
            bytes_removed=4_000,
            usage={"input_tokens": 7, "output_tokens": 3},
        ),
        _line(seq=4, status=502, error="boom"),
        "not json at all",
        "",
    ]


def test_summarize_counts_everything():
    s = summarize_index(_sample_lines())
    assert s["requests"] == 4  # junk and blank lines skipped
    assert s["statuses"] == {"200": 3, "502": 1}
    assert s["errors"] == 1
    assert s["shortcuts"] == {"title-gen": 1}
    assert s["transforms"] == {"reduce-main-system": 2, "filter-tools": 1}
    assert s["bytes_removed"] == 9_000
    assert s["bytes_unsent"] == 1_000
    assert s["usage_totals"] == {
        "input_tokens": 17,
        "output_tokens": 8,
        "cache_read_input_tokens": 900,
    }


def test_format_summary_renders_the_receipt():
    s = summarize_index(_sample_lines())
    out = format_summary(s, source="logs/run-x")
    assert "logs/run-x" in out
    assert "title-gen" in out
    assert "reduce-main-system" in out
    assert "9,000 bytes" in out  # removed total
    assert "1,000 bytes" in out  # unsent total
    assert "cache_read_input_tokens" in out
    assert "Drift check" in out


def test_empty_index_renders_without_crashing():
    out = format_summary(summarize_index([]))
    assert "Requests: 0" in out

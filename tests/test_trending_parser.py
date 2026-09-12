"""Trending 解析器与候选池合并的单元测试（基于真实页面 fixture）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.trending import (
    SourceSpec,
    TrendingEntry,
    TrendingParseError,
    merge_entries,
    parse_trending_html,
)

FIXTURES = Path(__file__).parent / "fixtures"
DAILY = FIXTURES / "trending_daily_2026-09-12.html"
WEEKLY = FIXTURES / "trending_weekly_2026-09-12.html"


def test_fixture_resolver_finds_dated_files():
    from src.trending import fixture_path

    assert fixture_path(FIXTURES, "daily") == DAILY
    assert fixture_path(FIXTURES, "weekly") == WEEKLY
    assert fixture_path(FIXTURES, "lang:kotlin") is None


def _daily_entries():
    return parse_trending_html(DAILY.read_text(encoding="utf-8"), since="daily")


def test_parse_daily_fixture_extracts_expected_fields():
    entries = _daily_entries()
    assert 5 <= len(entries) <= 60
    assert [e.rank for e in entries] == list(range(1, len(entries) + 1))

    names = [e.full_name for e in entries]
    assert all("/" in name and " " not in name for name in names), names
    assert len(set(names)) == len(names), "同一页面不应出现重复项目"

    coverage = sum(1 for e in entries if e.stars_today) / len(entries)
    assert coverage >= 0.8, f"stars today 覆盖率过低: {coverage:.0%}"

    first = entries[0]
    assert first.url == f"https://github.com/{first.full_name}"
    assert first.stars and first.stars > 0
    assert first.forks and first.forks >= 0
    assert first.rank == 1


def test_parse_weekly_uses_week_field():
    entries = parse_trending_html(WEEKLY.read_text(encoding="utf-8"), since="weekly")
    assert len(entries) >= 5
    weekly_rows = [e for e in entries if e.stars_this_week]
    assert weekly_rows, "weekly 页面应解析出 stars_this_week"
    # weekly 页面不应被误当成"今日增长"
    assert all(e.stars_today is None for e in weekly_rows)


def test_selector_break_is_detected_not_silently_empty():
    # 页面里还有数据容器，但结构变了导致解析不到任何项目 → 必须抛错，而不是产出空候选池
    broken = '<html><body><div class="Box-row"><h2></h2></div></body></html>'
    with pytest.raises(TrendingParseError):
        parse_trending_html(broken, since="daily")


def test_empty_page_is_not_an_error():
    assert parse_trending_html("<html><body><p>No trending repos</p></body></html>") == []


def test_merge_dedupes_and_takes_max_source_weight():
    daily = SourceSpec("daily", "https://github.com/trending?since=daily", "daily")
    weekly = SourceSpec("weekly", "https://github.com/trending?since=weekly", "weekly")
    weights = {"daily": 1.0, "weekly": 0.7, "language": 0.85, "search": 0.5}

    shared = TrendingEntry(full_name="Owner/Repo", url="https://github.com/Owner/Repo", rank=3, stars=1000, stars_today=200)
    same_weekly = TrendingEntry(full_name="owner/repo", url="https://github.com/owner/repo", rank=9, stars_this_week=900)
    other = TrendingEntry(full_name="a/b", url="https://github.com/a/b", rank=1, stars=5, stars_today=10)

    merged = merge_entries([(daily, shared), (weekly, same_weekly), (weekly, other)], weights)
    assert len(merged) == 2

    repo = next(c for c in merged if c.full_name == "Owner/Repo")
    assert sorted(repo.sources) == ["daily", "weekly"]
    assert repo.source_weight == 1.0, "来源权重必须取最大值，不能累加"
    assert repo.best_rank == 3
    assert repo.stars_today == 200
    assert repo.stars_this_week == 900

    # 排序按最佳名次
    assert [c.full_name for c in merged] == ["a/b", "Owner/Repo"]

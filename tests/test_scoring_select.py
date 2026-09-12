"""Trend Score 与多样性选择的单元测试。

重点验证方案里的两条硬承诺：
1. 打分使用固定参考值，与"当天其他项目"无关 → 跨天可比较。
2. 排序可复现；同类过多时自动收敛，且绝不会为了凑数放低标准。
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from src.config import load_config
from src.history import HistoryView
from src.models import Candidate
from src.scoring import TrendScorer
from src.select import BELOW_FLOOR, TARGET_REACHED, DiversitySelector

ROOT = Path(__file__).resolve().parents[1]
TODAY = date(2026, 9, 12)


@pytest.fixture(scope="module")
def cfg() -> dict:
    return load_config(ROOT / "config.yaml", env={}, strict_env=False)


@pytest.fixture(scope="module")
def scorer(cfg: dict) -> TrendScorer:
    return TrendScorer(cfg["scoring"])


@pytest.fixture(scope="module")
def selector(cfg: dict) -> DiversitySelector:
    return DiversitySelector(cfg["selection"])


def make(full_name: str, **kwargs) -> Candidate:
    owner, name = full_name.split("/")
    base = dict(
        full_name=full_name,
        url=f"https://github.com/{full_name}",
        owner=owner,
        name=name,
        source_weight=1.0,
        # 默认视为"已通过 API 核实"，以便测试多样性逻辑本身；
        # 未核实场景由 test_unassessed_penalty 单独覆盖。
        topics=["tool"],
        pushed_at="2026-09-10T00:00:00Z",
        software_status="keep",
        software_score=0.8,
    )
    base.update(kwargs)
    return Candidate(**base)


# --------------------------------------------------------------------- 打分
def test_score_is_independent_of_the_batch(scorer):
    """同一个项目，无论当天是否有更热的项目，分数必须相同（不是当天最大值归一化）。"""
    solo = make("a/solo", stars=5000, stars_today=300, best_rank=5)
    scorer.compute(solo, history=None, today=TODAY)

    hotter = make("b/hot", stars=100000, stars_today=5000, best_rank=1)
    scorer.compute(hotter, history=None, today=TODAY)
    together = make("a/solo", stars=5000, stars_today=300, best_rank=5)
    scorer.compute(together, history=None, today=TODAY)

    assert solo.trend_score == together.trend_score
    assert hotter.trend_score > solo.trend_score


def test_breakdown_contributions_sum_to_score(scorer):
    c = make("a/x", stars=10000, stars_today=500, best_rank=3)
    scorer.compute(c, history=None, today=TODAY)
    total = sum(part["contribution"] for part in c.score_breakdown.values())
    assert total == pytest.approx(c.trend_score, abs=0.05)


def test_velocity_source_priority_trending(scorer):
    c = make("a/x", stars=10000, stars_today=500, stars_this_week=900)
    scorer.compute(c, history=None, today=TODAY)
    assert c.velocity_source == "trending"
    assert c.velocity_value == 500


def test_velocity_source_history_delta(scorer):
    history = HistoryView(prev_stars={"a/x": (TODAY - timedelta(days=1), 9000)})
    c = make("a/x", stars=9500, stars_this_week=None)
    scorer.compute(c, history=history, today=TODAY, repeat_lookback_days=3)
    assert c.velocity_source == "history_delta"
    assert c.velocity_value == 500


def test_velocity_source_weekly_avg(scorer):
    c = make("a/x", stars=9500, stars_this_week=700)
    scorer.compute(c, history=None, today=TODAY)
    assert c.velocity_source == "weekly_avg"
    assert c.velocity_value == pytest.approx(100.0)


def test_velocity_unavailable_is_neutral_not_guessed(scorer):
    c = make("a/x", stars=9500)
    scorer.compute(c, history=None, today=TODAY)
    assert c.velocity_source == "unavailable"
    assert c.velocity_value is None
    assert c.score_breakdown["velocity"]["norm"] == pytest.approx(scorer.missing_velocity_norm)


def test_freshness_decays_with_history(scorer):
    history = HistoryView(seen_dates={"a/old": {TODAY - timedelta(days=6), TODAY}})
    old = make("a/old", stars=1000, stars_today=100, best_rank=10)
    scorer.compute(old, history=history, today=TODAY)
    assert old.days_since_first_seen == 6
    assert old.score_breakdown["freshness"]["norm"] == pytest.approx(0.4)

    fresh = make("a/new", stars=1000, stars_today=100, best_rank=10)
    scorer.compute(fresh, history=HistoryView(), today=TODAY)
    assert fresh.days_since_first_seen == 0
    assert fresh.score_breakdown["freshness"]["norm"] == pytest.approx(1.0)


def test_streak_days_from_history(scorer):
    days = {TODAY - timedelta(days=i) for i in range(4)}
    history = HistoryView(seen_dates={"a/streak": days})
    c = make("a/streak", stars=1000, stars_today=100, best_rank=10)
    scorer.compute(c, history=history, today=TODAY)
    assert c.trending_streak_days == 4


def test_recently_reported_flag(scorer):
    history = HistoryView(last_selected={"a/repeat": TODAY - timedelta(days=2)})
    c = make("a/repeat", stars=1000, stars_today=100, best_rank=10)
    scorer.compute(c, history=history, today=TODAY, repeat_lookback_days=3)
    assert c.recently_reported is True


# --------------------------------------------------------------------- 选择
def _ai_heavy_field() -> list[Candidate]:
    """方案 §4.3 的例子：AI 项目远比其他项目热，但不能让日报变成清一色 AI。"""
    specs = [
        ("a/agent-a", "ai-agent", "ai", "Python", 95),
        ("a/agent-b", "ai-agent", "ai", "Python", 93),
        ("a/agent-c", "ai-agent", "ai", "TypeScript", 91),
        ("a/coder-d", "ai-coding", "ai", "Python", 90),
        ("a/app-e", "ai-app", "ai", "TypeScript", 88),
        ("a/db-f", "database", "data", "Go", 80),
        ("a/cli-g", "cli", "dev-tools", "Rust", 78),
        ("a/sec-h", "security", "security", "Go", 75),
        ("a/desk-i", "mobile-desktop", "mobile-desktop", "Swift", 72),
        ("a/web-j", "web-frontend", "web", "TypeScript", 70),
    ]
    candidates = []
    for full_name, category, group, language, score in specs:
        c = make(full_name, category=category, group=group, language=language)
        c.trend_score = float(score)
        candidates.append(c)
    return candidates


def test_diversity_prevents_all_ai(selector):
    outcome = selector.select(_ai_heavy_field(), target=10, floor=35.0)

    assert outcome.count == 9
    assert outcome.stopped_reason == BELOW_FLOOR, "第 5 个 AI 项目被惩罚压到质量下限以下，应当停止而不是硬凑 10 个"

    groups = [c.group for c in outcome.selected]
    ai_count = groups.count("ai")
    assert ai_count == 4, f"AI 组应被压到 4 个，实际 {ai_count}"
    assert len(set(groups)) >= 5

    # 前两名仍然给最热的 AI 项目（热度优先，不是先到先得的分类配额）
    assert [c.category for c in outcome.selected[:2]] == ["ai-agent", "ai-agent"]
    # 第三名让位给真正热门的非 AI 项目（多样性惩罚生效）
    assert outcome.selected[2].category == "database"

    # 序号连续且可复现
    assert [c.rank for c in outcome.selected] == list(range(1, outcome.count + 1))
    again = selector.select(_ai_heavy_field(), target=10, floor=35.0)
    assert [c.full_name for c in again.selected] == [c.full_name for c in outcome.selected]


def test_monoculture_converges_instead_of_filling_ten(selector):
    candidates = []
    for index in range(20):
        c = make(f"a/same-{index:02d}", category="ai-agent", group="ai", language="Python")
        c.trend_score = 60.0
        candidates.append(c)
    outcome = selector.select(candidates, target=10, floor=35.0)
    # 第 3 个同类 = 60-14 = 46，第 4 个 = 60-33.3 = 26.7 < 35 → 停
    assert outcome.count == 3
    assert outcome.stopped_reason == BELOW_FLOOR


def test_below_floor_means_empty_report(selector):
    c = make("a/weak", category="cli", group="dev-tools")
    c.trend_score = 20.0
    outcome = selector.select([c], target=10, floor=35.0)
    assert outcome.count == 0
    assert outcome.stopped_reason == BELOW_FLOOR


def test_target_not_exceeded(selector):
    candidates = []
    for index in range(15):
        c = make(f"a/p{index:02d}", category=f"cat-{index}", group=f"group-{index}", language=f"L{index}")
        c.trend_score = 90.0 - index
        candidates.append(c)
    outcome = selector.select(candidates, target=10, floor=35.0)
    assert outcome.count == 10
    assert outcome.stopped_reason == TARGET_REACHED


def test_unassessed_penalty_prefers_verified_software(selector):
    """未能通过 API 核实（限流/失败）的项目应降权，让位给已核实的项目。"""
    unverified = make("a/unverified", category="ai-agent", group="ai", language="Go", topics=[], pushed_at=None)
    unverified.trend_score = 90.0
    verified = make("a/verified", category="database", group="data", language="Go")
    verified.trend_score = 84.0

    outcome = selector.select([unverified, verified], target=2, floor=35.0)
    assert outcome.selected[0].full_name == "a/verified"
    assert outcome.selected[1].penalties.get("unassessed") == 10.0


def test_gray_penalty_is_mild(selector):
    """灰区项目只轻微降权：热度明显更高时仍然能排在前面的位置。"""
    gray = make("a/gray", category="ai-app", group="ai", language="Go", software_status="gray", software_score=0.35)
    gray.trend_score = 85.0
    keep = make("a/keep", category="cli", group="dev-tools", language="Rust")
    keep.trend_score = 78.0

    outcome = selector.select([gray, keep], target=2, floor=35.0)
    assert [c.full_name for c in outcome.selected] == ["a/gray", "a/keep"]
    assert outcome.selected[0].penalties.get("gray") == 4.0


def test_repeat_penalty_lowers_but_does_not_ban(selector):
    reported = make("a/reported", category="cli", group="dev-tools", language="Go")
    reported.trend_score = 95.0
    reported.recently_reported = True

    fresh = make("a/fresh", category="database", group="data", language="Go")
    fresh.trend_score = 85.0

    outcome = selector.select([reported, fresh], target=2, floor=35.0)
    assert outcome.selected[0].full_name == "a/fresh", "已报告过的项目应被降权让位给新鲜项目"

    # 但真正极热的项目仍然能进来（不是一刀切淘汰）
    very_hot = make("a/very-hot", category="ai-app", group="ai", language="Go")
    very_hot.trend_score = 99.0
    very_hot.recently_reported = True
    outcome2 = selector.select([very_hot, fresh], target=2, floor=35.0)
    assert "a/very-hot" in [c.full_name for c in outcome2.selected]

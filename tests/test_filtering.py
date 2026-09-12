"""软件相关性过滤与分类的单元测试。"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from src.classify import Classifier
from src.config import load_config
from src.filtering import DROP, GRAY, KEEP, SoftwareFilter, compute_language_ratios
from src.models import Candidate

ROOT = Path(__file__).resolve().parents[1]
TODAY = date(2026, 9, 12)


@pytest.fixture(scope="module")
def cfg() -> dict:
    return load_config(ROOT / "config.yaml", env={}, strict_env=False)


@pytest.fixture(scope="module")
def flt(cfg: dict) -> SoftwareFilter:
    return SoftwareFilter(cfg["filter"])


@pytest.fixture(scope="module")
def clf(cfg: dict) -> Classifier:
    return Classifier(cfg["classify"])


def make(full_name: str, **kwargs) -> Candidate:
    owner, name = full_name.split("/")
    base = dict(
        full_name=full_name,
        url=f"https://github.com/{full_name}",
        owner=owner,
        name=name,
        pushed_at="2026-09-10T00:00:00Z",
    )
    base.update(kwargs)
    return Candidate(**base)


# --------------------------------------------------------------- 强排除
@pytest.mark.parametrize(
    "name",
    ["awesome-python", "python-awesome", "cheatsheet", "vscode-tutorial", "system-design-interview", "machine-learning-roadmap", "free-programming-books"],
)
def test_strong_name_patterns_drop(flt, name):
    result = flt.prefilter(make(f"someone/{name}"))
    assert result.status == DROP, f"{name} 应被强规则排除"


def test_strong_desc_pattern_drops_curated_list(flt):
    c = make("someone/cool-stuff", description="A curated list of the best tools for X")
    assert flt.prefilter(c).status == DROP


def test_chinese_tutorial_desc_drops(flt):
    c = make("someone/learn-x", description="这是一份学习资料，包含教程与面试题")
    assert flt.prefilter(c).status == DROP


# --------------------------------------------------------------- 弱信号不误杀
def test_weak_word_does_not_falsely_kill_bookstack(flt):
    """'bookstack' 含 book，但词边界匹配不应命中。"""
    c = make("bookstack-org/bookstack", description="A platform for organising and storing information")
    assert flt.prefilter(c).status == KEEP


def test_single_weak_signal_is_kept_as_gray_signal(flt):
    c = make("someone/nice-tool", description="A practical handbook for developers")
    result = flt.prefilter(c)
    assert result.status == KEEP
    assert result.weak_signals, "单一弱信号应被记录"


def test_two_weak_signals_drop(flt):
    c = make("someone/notes-list", description="My personal notes and a list of resources")
    assert flt.prefilter(c).status == DROP


def test_skill_bundle_with_two_signals_dropped(flt):
    """技能/提示词集合：描述含 skill + topics 含 claude-skills → 两个弱信号 → 排除。"""
    c = make(
        "someone/i-have-x",
        description="A skill to stop your coding agent from burying the answer",
        topics=["claude-skills", "claude-code-plugin"],
    )
    assert flt.prefilter(c).status == KEEP
    assert flt.finalize(c, today=TODAY).status == DROP


def test_single_skill_signal_only_weakens(flt):
    """只命中一个技能类弱信号时，不排除，仅轻微降权。"""
    c = make("someone/skills", description="Reusable agent tooling", language="Python", has_manifest=True)
    pre = flt.prefilter(c)          # 真实流程：先预过滤，弱信号会延续到终筛
    flt.apply(c, pre)
    result = flt.finalize(c, today=TODAY)
    assert pre.weak_signals, "名称命中弱词 skills 应被记录"
    assert result.status in (KEEP, GRAY)


# --------------------------------------------------------------- 阶段二
def test_real_tool_kept_with_high_score(flt):
    c = make(
        "acme/supercli",
        name="supercli",
        description="A fast command line tool for deploying services",
        language="Go",
        topics=["cli", "devops", "automation"],
        has_manifest=True,
        manifests_found=["go.mod"],
        has_release=True,
        license="MIT",
        archived=False,
        markup_ratio=0.05,
    )
    result = flt.finalize(c, today=TODAY)
    assert result.status == KEEP
    assert result.score >= 0.9
    assert any("依赖清单" in p for p in result.positives)


def test_markup_heavy_repo_dropped(flt):
    c = make("someone/guide", language="Markdown", markup_ratio=0.88, has_manifest=False)
    result = flt.finalize(c, today=TODAY)
    assert result.status == DROP
    assert any("标记语言" in r for r in result.reasons)


def test_excluded_topic_dropped(flt):
    c = make("someone/thing", topics=["awesome-list"], language="Python", has_manifest=True)
    assert flt.finalize(c, today=TODAY).status == DROP


def test_archived_dropped(flt):
    c = make("someone/old", archived=True, language="Python", has_manifest=True)
    assert flt.finalize(c, today=TODAY).status == DROP


def test_weak_topics_combined_drop(flt):
    c = make("someone/thing", topics=["tutorial", "learning"], language="Python")
    assert flt.finalize(c, today=TODAY).status == DROP


def test_empty_metadata_lands_in_drop_or_gray_not_keep(flt):
    c = make("someone/mystery")
    result = flt.finalize(c, today=TODAY)
    assert result.status in (DROP, GRAY)
    assert result.score < flt.keep_threshold


def test_language_ratios():
    ratios = compute_language_ratios({"Python": 8000, "Markdown": 2000}, {"markup_languages": ["Markdown"]})
    assert ratios["markup_ratio"] == pytest.approx(0.2)
    assert compute_language_ratios({}, {})["markup_ratio"] is None


# --------------------------------------------------------------- 分类
def test_classify_by_topic(clf):
    c = make("a/b", topics=["ai-agent", "llm"])
    clf.apply(c)
    assert c.category == "ai-agent" and c.group == "ai"


def test_classify_by_description_keyword(clf):
    c = make("a/some-db", name="some-db", description="A distributed SQL database engine", language="Rust")
    clf.apply(c)
    assert c.category == "database" and c.group == "data"


def test_classify_language_fallback(clf):
    c = make("a/plainlib", language="Python")
    clf.apply(c)
    assert c.category == "library-sdk" and c.group == "dev-tools"


def test_classify_unknown_falls_back_to_other(clf):
    c = make("a/mystery")
    clf.apply(c)
    assert c.category == "other" and c.group == "other"

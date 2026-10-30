"""技能/提示词类仓库的判定机制。

背景：2026-10-30 有 9 个项目因名字含 skill(s) 在 prefilter 就被杀掉，
其中 obra/superpowers（30 万 star）、addyosmani/agent-skills 是可执行软件。
现在改为：prefilter 放行 soft_words 类信号，finalize 用"有无依赖清单/Release"判定。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from src.config import load_config
from src.filtering import DROP, KEEP, SoftwareFilter
from src.models import Candidate

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def flt() -> SoftwareFilter:
    cfg = load_config(ROOT / "config.yaml", env={}, strict_env=False)
    return SoftwareFilter(cfg["filter"])


def make(name: str, desc: str, **kwargs) -> Candidate:
    return Candidate(
        full_name=f"owner/{name}", url="", owner="owner", name=name, description=desc,
        language=kwargs.pop("language", "JavaScript"), topics=kwargs.pop("topics", []),
        stars=1000, stars_today=100, forks=10, best_rank=1, sources=["daily"], source_weight=1.0,
        **kwargs,
    )


def test_skill_repo_survives_prefilter(flt):
    """名字/描述含 skill 的仓库不再在 prefilter 被杀（阶段一没有 API 数据，判不了）。"""
    result = flt.prefilter(make("agent-skills", "Skills that make agents useful"))
    assert result.status == KEEP, result.reasons


def test_prompt_repo_survives_prefilter(flt):
    result = flt.prefilter(make("prompt-pack", "A pack of prompts for coding"))
    assert result.status == KEEP, result.reasons


def test_skill_repo_with_manifest_is_software(flt):
    """有依赖清单 → 当软件保留，即使弱信号命中。"""
    c = make("agent-skills", "Skills for agents", topics=["skills", "ai"],
             has_manifest=True, has_release=True, pushed_at="2026-10-29T00:00:00Z", license="MIT")
    c.weak_signals = ["topics 含「skills」", "描述含「skill」"]
    result = flt.finalize(c, today=date(2026, 10, 30))
    assert result.status == KEEP, result.reasons


def test_skill_repo_without_manifest_is_dropped(flt):
    """纯 Markdown 的技能集合没有清单也没有 Release → 仍然淘汰。"""
    c = make("agent-skills", "Skills for agents", topics=["skills", "ai"],
             has_manifest=False, has_release=False, pushed_at="2026-10-29T00:00:00Z", license="MIT")
    c.weak_signals = ["topics 含「skills」", "描述含「skill」"]
    result = flt.finalize(c, today=date(2026, 10, 30))
    assert result.status == DROP, result.reasons


def test_non_soft_weak_signals_still_drop(flt):
    """非 soft 词（template/tutorial）不受放行逻辑影响，达到阈值照旧淘汰。"""
    result = flt.prefilter(make("web-template", "A template and a tutorial for apps"))
    assert result.status == DROP, result.reasons


def test_mixed_signals_still_drop(flt):
    """soft 词 + 非 soft 词混合时不放行：只要有一个非 soft 信号就照常淘汰。"""
    result = flt.prefilter(make("prompt-template", "A prompt template collection"))
    assert result.status == DROP, result.reasons

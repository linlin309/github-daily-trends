"""分类特异性：宽泛 topic 不能压过具体 topic。

背景：2026-10-30 OpenMausBot 的 topics 是 ai-agents + docker + electron，
却因为 devops-cloud 的精确集里有 docker 而被判成 devops-cloud。
现在按"专指 1.0 / 泛化 0.25"打分，取总分最高的类别。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.classify import Classifier
from src.config import load_config
from src.models import Candidate

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def clf() -> Classifier:
    cfg = load_config(ROOT / "config.yaml", env={}, strict_env=False)
    return Classifier(cfg["classify"])


def make(name: str, topics: list[str], language: str = "TypeScript") -> Candidate:
    return Candidate(
        full_name=f"owner/{name}", url="", owner="owner", name=name, description="",
        language=language, topics=topics, stars=1000, stars_today=100, forks=10,
        best_rank=1, sources=["daily"], source_weight=1.0,
    )


def test_ai_agent_beats_generic_docker(clf):
    """ai-agents 是专指 topic，docker 是泛化 topic → 应判为 AI 类，而不是 devops-cloud。"""
    category, group = clf.classify(
        make("OpenMausBot", ["ai-agents", "claude-code", "docker", "electron", "grok"])
    )
    assert group == "ai", f"got {category}/{group}"


def test_specific_ai_topics(clf):
    category, group = clf.classify(make("hindsight", ["agentic-ai", "agents", "ai-memory"]))
    assert (category, group) == ("ai-agent", "ai")


def test_security_still_wins_with_specific_topics(clf):
    category, group = clf.classify(
        make("ghidra", ["disassembler", "reverse-engineering", "software-analysis"], "Java")
    )
    assert group == "security", f"got {category}/{group}"


def test_language_fallback_when_no_topics(clf):
    category, group = clf.classify(make("php-src", [], "C"))
    assert (category, group) == ("systems-lang", "systems")

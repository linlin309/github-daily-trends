"""渲染层测试：转义安全、数量措辞、JSON 契约、链接来源。"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from src.analyze import AnalysisResult
from src.config import load_config
from src.models import Candidate
from src.render import ReportRenderer

ROOT = Path(__file__).resolve().parents[1]
TODAY = date(2026, 9, 12)


@pytest.fixture(scope="module")
def cfg() -> dict:
    return load_config(ROOT / "config.yaml", env={}, strict_env=False)


def make(full_name: str, **kwargs) -> Candidate:
    owner, name = full_name.split("/")
    base = dict(
        full_name=full_name,
        url=f"https://github.com/{full_name}",
        owner=owner,
        name=name,
        stars=42181,
        stars_today=3463,
        forks=2386,
        language="Python",
        topics=["cli", "ai-agent"],
        pushed_at="2026-09-11T07:04:20Z",
        sources=["daily", "weekly"],
        best_rank=1,
        trend_score=96.6,
        category="ai-agent",
        group="ai",
        trending_streak_days=3,
        rank=1,
    )
    base.update(kwargs)
    return Candidate(**base)


def build_bundle(cfg, selected, *, analysis=None, meta=None):
    analysis = analysis or AnalysisResult(
        ok=True,
        provider="zhipu",
        model="glm-4.7-flash",
        calls=1,
        summary={"headline": "今天 AI 与开发工具活跃", "trends": ["AI Agent 持续升温"], "highlights": ["某项目日增 3463"]},
    )
    for candidate in selected:
        if candidate.ai:
            continue  # 测试已自行注入 AI 文本时不要覆盖
        candidate.ai = {
            "summary": "一个命令行工具",
            "what_it_is": "用来做某事的工具",
            "why_worth_attention": "增长很快",
            "why_is_speculation": True,
            "use_case": "日常开发",
            "target_user": "开发者",
        }
    return ReportRenderer(cfg, project_root=ROOT).render(
        report_date=TODAY,
        selected=selected,
        candidates=selected,
        analysis=analysis,
        meta=meta or {"mode": "trending", "generated_at": "2026-09-12T12:07:00+08:00"},
    )


def test_html_escapes_untrusted_content(cfg):
    """外部描述与模型输出中的 HTML/脚本必须被转义，绝不能进入邮件正文结构。"""
    evil = make("evil/xss", description="<script>alert('x')</script>", topics=["<img src=x onerror=alert(1)>"])
    evil.ai = {
        "summary": "<script>alert('from-ai')</script>",
        "what_it_is": "<iframe src=//evil.example></iframe>",
        "why_worth_attention": "<a href='https://evil.example'>click</a>",
        "why_is_speculation": True,
        "use_case": "",
        "target_user": "",
    }
    evil.tags = ["今日最值得关注"]
    bundle = build_bundle(cfg, [evil])

    assert "<script>" not in bundle.html
    assert "<iframe" not in bundle.html
    assert "<img" not in bundle.html
    assert "&lt;script&gt;" in bundle.html
    # 唯一的链接必须来自程序生成的 GitHub 地址（模型编造的域名只作为转义后的纯文本存在）
    import re

    hrefs = re.findall(r'href="([^"]+)"', bundle.html)
    assert hrefs, "邮件里应当包含 GitHub 链接"
    assert all(url.startswith("https://github.com/") for url in hrefs), hrefs
    assert not any("evil.example" in url for url in hrefs), "模型提供的链接绝不能变成可点击的 href"


def test_shortage_wording_when_fewer_than_target(cfg):
    selected = [make("a/one", rank=1)]
    bundle = build_bundle(cfg, selected, meta={"mode": "trending", "stopped_reason": "below_floor"})
    assert "今日精选 **1 个**" in bundle.markdown
    assert "宁缺毋滥" in bundle.markdown or "质量下限" in bundle.markdown
    assert "今日精选 1 个" in bundle.plain_text


def test_speculation_marker_rendered(cfg):
    selected = [make("a/x")]
    bundle = build_bundle(cfg, selected)
    assert "（推测）" in bundle.markdown
    assert "（推测）" in bundle.plain_text


def test_json_payload_contract(cfg):
    selected = [make("a/x", rank=1)]
    bundle = build_bundle(cfg, selected, meta={"mode": "trending+search", "kept_count": 12, "floor": 35.0})
    payload = bundle.json_payload

    assert payload["date"] == "2026-09-12"
    assert payload["mode"] == "trending+search"
    assert payload["counts"] == {"candidates": 1, "after_filter": 12, "selected": 1, "target": 10}
    assert payload["llm"]["provider"] == "zhipu" and payload["llm"]["calls"] == 1
    assert payload["summary"]["headline"]
    selected_item = payload["selected"][0]
    for key in ("full_name", "stars", "stars_today", "trend_score", "score_breakdown", "category", "ai", "tags"):
        assert key in selected_item
    compact = payload["candidates"][0]
    assert "ai" not in compact, "候选池历史记录里不应包含 AI 文本（控制体积）"
    # 必须可 JSON 序列化（会被写入仓库）
    json.dumps(payload, ensure_ascii=False)


def test_tag_rows_and_mode_label(cfg):
    selected = [make("a/x", tags=["今日最值得关注"], rank=1)]
    bundle = build_bundle(cfg, selected, meta={"mode": "search-only"})
    assert "🔥" in bundle.html
    assert "Search 兜底" in bundle.html


def test_degraded_notes_surface_in_report(cfg):
    selected = [make("a/x")]
    bundle = build_bundle(cfg, selected, meta={"mode": "trending", "degraded": ["AI 分析不可用（超时），本次为纯数据日报"]})
    assert "AI 分析不可用" in bundle.markdown
    assert "AI 分析不可用" in bundle.plain_text
    assert "AI 分析不可用" in bundle.html

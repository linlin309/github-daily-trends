"""报告渲染：Markdown（人读）/ JSON（机器读）/ 纯文本 + HTML（邮件）。

- HTML 由 Jinja2 渲染并开启自动转义：外部内容与模型输出中的任何标签都会被转义，
  不存在把第三方 HTML/脚本注入邮件的可能。
- 链接一律由程序根据 full_name 生成，绝不使用模型返回的链接。
- 数量不足 10 个时，明确写出"今日精选 N 个"，并在正文说明原因。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .analyze import AnalysisResult
from .models import Candidate
from .util import fmt_int, fmt_signed

TAG_EMOJI = {
    "今日最值得关注": "🔥",
    "今日最实用": "🛠",
    "今日潜力项目": "🚀",
}

TARGET_REACHED = "target_reached"
BELOW_FLOOR = "below_floor"


@dataclass
class ReportBundle:
    markdown: str
    plain_text: str
    html: str
    json_payload: dict[str, Any]


class ReportRenderer:
    def __init__(self, cfg: dict, *, project_root: Path | None = None) -> None:
        self.cfg = cfg
        self.root = project_root or Path(__file__).resolve().parents[1]
        self.title_prefix = cfg.get("report", {}).get("title_prefix", "【GitHub 每日软件趋势】")
        self.target_count = int(cfg.get("report", {}).get("target_count", 10))
        self.env = Environment(
            loader=FileSystemLoader(str(self.root / "templates")),
            autoescape=select_autoescape(enabled_extensions=("html", "xml", "j2"), default=True),
            trim_blocks=True,
            lstrip_blocks=True,
        )

    # ------------------------------------------------------------------ 入口
    def render(
        self,
        *,
        report_date: date,
        selected: list[Candidate],
        candidates: list[Candidate],
        analysis: AnalysisResult,
        meta: dict[str, Any],
    ) -> ReportBundle:
        generated_at = meta.get("generated_at") or datetime.now().isoformat(timespec="seconds")
        context = {
            "date": report_date.isoformat(),
            "title_prefix": self.title_prefix,
            "generated_at": generated_at,
            "selected": selected,
            "selected_count": len(selected),
            "target_count": self.target_count,
            "candidates_count": meta.get("candidates_count") or len(candidates),
            "summary": analysis.summary,
            "tag_emoji": TAG_EMOJI,
            "tag_rows": _tag_rows(selected),
            "degraded": meta.get("degraded") or [],
            "mode_label": _mode_label(meta.get("mode")),
            "mode": meta.get("mode"),
            "sources_ok": meta.get("sources_ok") or [],
            "sources_failed": meta.get("sources_failed") or [],
            "llm": analysis.to_dict(),
            "ai_available": analysis.ok,
            "stopped_reason": meta.get("stopped_reason"),
            "shortage_note": _shortage_note(len(selected), self.target_count, meta.get("stopped_reason")),
            "fmt_int": fmt_int,
            "fmt_signed": fmt_signed,
            "short_date": lambda value: (value or "")[:10] or "—",
        }
        markdown = self._render_markdown(context)
        plain_text = self._render_plain(context)
        html = self._render_html(context)
        json_payload = self._build_json(report_date, generated_at, selected, candidates, analysis, meta)
        return ReportBundle(markdown=markdown, plain_text=plain_text, html=html, json_payload=json_payload)

    # ------------------------------------------------------------------ Markdown
    def _render_markdown(self, ctx: dict[str, Any]) -> str:
        lines: list[str] = []
        lines.append(f"# {ctx['title_prefix']}{ctx['date']}")
        lines.append("")
        lines.append(
            f"> 今日精选 **{ctx['selected_count']} 个**软件项目 · "
            f"候选池 {ctx['candidates_count']} 个 · 模式：{ctx['mode_label']} · "
            f"生成时间 {ctx['generated_at']}"
        )
        if ctx["stopped_reason"] == BELOW_FLOOR:
            lines.append(">")
            lines.append("> 说明：达到质量下限后停止选取，因此数量可能少于 10 个 —— 宁缺毋滥。")
        lines.append("")
        lines.append("## 今日趋势概览")
        lines.append("")
        lines.append(ctx["summary"].get("headline") or "（AI 未生成概览）")
        lines.append("")
        for trend in ctx["summary"].get("trends") or []:
            lines.append(f"- {trend}")
        for highlight in ctx["summary"].get("highlights") or []:
            lines.append(f"- {highlight}")
        lines.append("")

        if ctx["tag_rows"]:
            lines.append("| 标签 | 项目 |")
            lines.append("| --- | --- |")
            for tag, names in ctx["tag_rows"]:
                lines.append(f"| {TAG_EMOJI.get(tag, '')} {tag} | {', '.join(names)} |")
            lines.append("")

        lines.append("---")
        lines.append("")
        for candidate in ctx["selected"]:
            ai = candidate.ai or {}
            lines.append(f"## {candidate.rank:02d}. {candidate.full_name}")
            lines.append("")
            if candidate.tags:
                chips = " ".join(f"{TAG_EMOJI.get(t, '')} {t}" for t in candidate.tags)
                lines.append(f"{chips}")
                lines.append("")
            lines.append("| 指标 | 值 |")
            lines.append("| --- | --- |")
            lines.append(f"| Stars | {fmt_int(candidate.stars)}（今日 {fmt_signed(candidate.stars_today)}） |")
            lines.append(f"| Forks | {fmt_int(candidate.forks)} |")
            lines.append(f"| Language | {candidate.language or '—'} |")
            lines.append(f"| Topics | {', '.join(candidate.topics[:8]) or '—'} |")
            lines.append(f"| 最近更新 | {(candidate.pushed_at or '')[:10] or '—'} |")
            lines.append(f"| 来源 | {', '.join(sorted(candidate.sources)) or '—'} |")
            streak = f" · 连续上榜 {candidate.trending_streak_days} 天" if candidate.trending_streak_days > 1 else ""
            lines.append(
                f"| Trend Score | {candidate.trend_score:.1f}（{candidate.category}{streak}） |"
            )
            lines.append(f"| GitHub | {candidate.url} |")
            lines.append("")
            if ai.get("summary"):
                lines.append(f"**一句话**：{ai['summary']}")
                lines.append("")
            if ai.get("what_it_is"):
                lines.append(f"**它是什么**：{ai['what_it_is']}")
                lines.append("")
            if ai.get("why_worth_attention"):
                marker = "（推测）" if ai.get("why_is_speculation") else ""
                lines.append(f"**为什么值得关注**{marker}：{ai['why_worth_attention']}")
                lines.append("")
            if ai.get("use_case"):
                lines.append(f"**典型场景**：{ai['use_case']}")
                lines.append("")
            if ai.get("target_user"):
                lines.append(f"**适合谁**：{ai['target_user']}")
                lines.append("")
            if ai.get("unavailable"):
                lines.append("_（AI 分析不可用，本条为纯数据展示）_")
                lines.append("")

        if ctx["degraded"]:
            lines.append("---")
            lines.append("")
            lines.append("## 本次运行提示")
            lines.append("")
            for item in ctx["degraded"]:
                lines.append(f"- {item}")
            lines.append("")

        lines.append("---")
        lines.append("")
        lines.append("*Generated automatically by GitHub Actions*")
        lines.append("")
        return "\n".join(lines)

    # ------------------------------------------------------------------ 纯文本
    def _render_plain(self, ctx: dict[str, Any]) -> str:
        lines: list[str] = []
        lines.append(f"{ctx['title_prefix']}{ctx['date']}")
        lines.append(f"今日精选 {ctx['selected_count']} 个软件项目（候选池 {ctx['candidates_count']} 个）")
        lines.append("")
        lines.append("【今日趋势概览】")
        lines.append(ctx["summary"].get("headline") or "（AI 未生成概览）")
        for trend in ctx["summary"].get("trends") or []:
            lines.append(f"  · {trend}")
        for highlight in ctx["summary"].get("highlights") or []:
            lines.append(f"  · {highlight}")
        lines.append("")
        lines.append("=" * 46)
        for candidate in ctx["selected"]:
            ai = candidate.ai or {}
            lines.append("")
            lines.append(f"{candidate.rank:02d}. {candidate.full_name}")
            if candidate.tags:
                lines.append("    标签：" + "、".join(candidate.tags))
            lines.append(
                f"    Stars：{fmt_int(candidate.stars)}（今日 {fmt_signed(candidate.stars_today)}）"
                f"  Forks：{fmt_int(candidate.forks)}  语言：{candidate.language or '—'}"
            )
            lines.append(f"    Topics：{', '.join(candidate.topics[:8]) or '—'}")
            lines.append(f"    地址：{candidate.url}")
            if ai.get("summary"):
                lines.append(f"    一句话：{ai['summary']}")
            if ai.get("what_it_is"):
                lines.append(f"    它是什么：{ai['what_it_is']}")
            if ai.get("why_worth_attention"):
                marker = "（推测）" if ai.get("why_is_speculation") else ""
                lines.append(f"    为什么值得关注{marker}：{ai['why_worth_attention']}")
            if ai.get("use_case"):
                lines.append(f"    典型场景：{ai['use_case']}")
            if ai.get("target_user"):
                lines.append(f"    适合谁：{ai['target_user']}")
            if ai.get("unavailable"):
                lines.append("    （AI 分析不可用，本条为纯数据展示）")
        if ctx["degraded"]:
            lines.append("")
            lines.append("【本次运行提示】")
            for item in ctx["degraded"]:
                lines.append(f"  · {item}")
        lines.append("")
        lines.append("-" * 46)
        lines.append("Generated automatically by GitHub Actions")
        return "\n".join(lines)

    # ------------------------------------------------------------------ HTML
    def _render_html(self, ctx: dict[str, Any]) -> str:
        template = self.env.get_template("email.html.j2")
        return template.render(**ctx)

    # ------------------------------------------------------------------ JSON
    def _build_json(
        self,
        report_date: date,
        generated_at: str,
        selected: list[Candidate],
        candidates: list[Candidate],
        analysis: AnalysisResult,
        meta: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "date": report_date.isoformat(),
            "generated_at": generated_at,
            "mode": meta.get("mode"),
            "weights_version": meta.get("weights_version"),
            "counts": {
                "candidates": len(candidates),
                "after_filter": meta.get("kept_count"),
                "selected": len(selected),
                "target": self.target_count,
            },
            "selection": {
                "stopped_reason": meta.get("stopped_reason"),
                "floor": meta.get("floor"),
            },
            "sources": {
                "ok": meta.get("sources_ok") or [],
                "failed": meta.get("sources_failed") or [],
            },
            "llm": analysis.to_dict(),
            "degraded": meta.get("degraded") or [],
            "summary": analysis.summary,
            "selected": [candidate.to_dict() for candidate in selected],
            "candidates": [_compact(candidate) for candidate in candidates],
        }


def _compact(candidate: Candidate) -> dict[str, Any]:
    """历史分析用的精简记录（不含 AI 文本，控制文件体积）。"""
    return {
        "full_name": candidate.full_name,
        "stars": candidate.stars,
        "stars_today": candidate.stars_today,
        "stars_this_week": candidate.stars_this_week,
        "forks": candidate.forks,
        "language": candidate.language,
        "group": candidate.group,
        "category": candidate.category,
        "software_status": candidate.software_status,
        "software_score": round(candidate.software_score, 3),
        "trend_score": round(candidate.trend_score, 2),
        "best_rank": candidate.best_rank,
        "sources": sorted(candidate.sources),
        "reasons": (candidate.filter_reasons or [])[:3],
    }


def _tag_rows(selected: list[Candidate]) -> list[tuple[str, list[str]]]:
    rows: list[tuple[str, list[str]]] = []
    for tag in TAG_EMOJI:
        names = [c.full_name for c in selected if tag in (c.tags or [])]
        if names:
            rows.append((tag, names))
    return rows


def _mode_label(mode: str | None) -> str:
    return {
        "trending": "Trending 正常",
        "trending+search": "Trending + Search 兜底",
        "search-only": "Search 兜底（降级）",
        None: "未知",
    }.get(mode, str(mode))


def _shortage_note(selected_count: int, target: int, stopped_reason: str | None) -> str:
    if selected_count >= target:
        return ""
    if selected_count == 0:
        return "今日没有符合标准的软件项目。"
    if stopped_reason == BELOW_FLOOR:
        return f"今日精选 {selected_count} 个：达到质量下限后停止选取，不以低价值项目凑满 {target} 个。"
    return f"今日精选 {selected_count} 个：符合标准的项目不足 {target} 个。"


def dump_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)

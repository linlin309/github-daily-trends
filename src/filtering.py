"""软件相关性规则过滤（第一轮，规则优先，AI 不参与相关性判定）。

两个阶段：
- prefilter：只用 Trending 就能拿到的信息（name / description），把明显的
  教程、Awesome List、书籍、数据集等直接排掉，避免为它们浪费 GitHub API 配额。
- finalize：拿到 /languages、依赖清单、release、license 之后，计算 software_score
  并按阈值分成 keep / gray / drop 三档。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from .models import Candidate

KEEP = "keep"
GRAY = "gray"
DROP = "drop"


@dataclass
class FilterResult:
    status: str
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    weak_signals: list[str] = field(default_factory=list)
    positives: list[str] = field(default_factory=list)


def compute_language_ratios(languages_bytes: dict[str, int], cfg: dict) -> dict[str, float | None]:
    """由 /languages 的字节数计算标记语言 / 笔记本 / 静态页占比。"""
    total = sum(int(v) for v in (languages_bytes or {}).values())
    if total <= 0:
        return {"markup_ratio": None, "notebook_ratio": None, "html_ratio": None}

    markup_set = set(cfg.get("markup_languages") or [])
    markup = sum(int(v) for k, v in languages_bytes.items() if k in markup_set)
    notebook = int(languages_bytes.get(cfg.get("notebook_language", "Jupyter Notebook"), 0))
    html = int(languages_bytes.get(cfg.get("html_language", "HTML"), 0))
    return {
        "markup_ratio": round(markup / total, 4),
        "notebook_ratio": round(notebook / total, 4),
        "html_ratio": round(html / total, 4),
    }


class SoftwareFilter:
    def __init__(self, cfg: dict, logger: logging.Logger | None = None) -> None:
        self.cfg = cfg
        self.log = logger or logging.getLogger("filter")
        strong = cfg.get("strong") or {}
        weak = cfg.get("weak") or {}

        self.strong_name = [re.compile(p) for p in (strong.get("name_regex") or [])]
        self.strong_desc = [re.compile(p) for p in (strong.get("desc_regex") or [])]
        self.strong_topics = {t.lower() for t in (strong.get("topics") or [])}

        self.weak_name = self._compile_words(weak.get("name_words") or [])
        self.weak_desc = self._compile_words(weak.get("desc_words") or [])
        self.weak_topics = {t.lower() for t in (weak.get("topics") or [])}
        self.weak_min_hits = int(weak.get("min_hits", 2))

        self.positive_weights = dict(cfg.get("positive_weights") or {})
        self.weak_penalty = float(cfg.get("weak_signal_penalty", 0.10))
        self.gray_penalty = float(cfg.get("gray_penalty", 0.15))
        thresholds = cfg.get("thresholds") or {}
        self.keep_threshold = float(thresholds.get("keep", 0.45))
        self.gray_threshold = float(thresholds.get("gray", 0.30))
        self.software_topics = {t.lower() for t in (cfg.get("software_topics") or [])}
        self.manifests = set(cfg.get("manifests") or [])
        self.markup_languages = set(cfg.get("markup_languages") or [])
        self.notebook_language = cfg.get("notebook_language", "Jupyter Notebook")
        self.markup_ratio_max = float(cfg.get("markup_ratio_max", 0.60))
        self.notebook_ratio_gray = float(cfg.get("notebook_ratio_gray", 0.70))
        self.html_ratio_gray = float(cfg.get("html_ratio_gray", 0.80))

    # ------------------------------------------------------------------ 阶段一
    def prefilter(self, candidate: Candidate) -> FilterResult:
        reasons: list[str] = []
        name = candidate.name or ""
        description = candidate.description or ""

        for pattern in self.strong_name:
            if pattern.search(name):
                return FilterResult(DROP, 0.0, [f"名称命中强排除规则: {pattern.pattern}"])

        for pattern in self.strong_desc:
            if description and pattern.search(description):
                return FilterResult(DROP, 0.0, [f"描述命中强排除规则: {pattern.pattern}"])

        weak_hits: list[str] = []
        for word, regex in self.weak_name:
            if regex.search(name):
                weak_hits.append(f"名称含「{word}」")
        for word, regex in self.weak_desc:
            if description and regex.search(description):
                weak_hits.append(f"描述含「{word}」")

        if len(weak_hits) >= self.weak_min_hits:
            return FilterResult(DROP, 0.0, [f"弱信号命中 {len(weak_hits)} 个（阈值 {self.weak_min_hits}）: " + "; ".join(weak_hits)])

        return FilterResult(KEEP, 0.0, [], weak_hits)

    # ------------------------------------------------------------------ 阶段二
    def finalize(self, candidate: Candidate, *, today: date | None = None) -> FilterResult:
        today = today or date.today()
        reasons: list[str] = []
        positives: list[str] = []
        weak_signals = list(candidate.weak_signals)

        # 硬性排除
        if self.cfg.get("archived_drop", True) and candidate.archived:
            return FilterResult(DROP, 0.0, ["仓库已归档（archived）"], weak_signals)
        if any("HTTP 404" in err for err in candidate.errors):
            return FilterResult(DROP, 0.0, ["仓库不存在或已删除（API 返回 404）"], weak_signals)

        # 缺少 API 元数据（被限流 / 调用失败 / 离线模式）时不做"无数据即淘汰"的判断，
        # 按灰区保留并如实标注 —— 单个项目或整批补全失败都不应让日报失败。
        if not candidate.is_enriched:
            score = self.positive_weights.get("code_language", 0.0) if candidate.language else 0.0
            return FilterResult(
                GRAY,
                score,
                ["缺少 GitHub API 元数据（限流/调用失败/离线模式），未能完整评估，按灰区保留"],
                weak_signals,
            )

        topics_lower = {t.lower() for t in candidate.topics}
        hit_excluded_topics = sorted(topics_lower & self.strong_topics)
        if hit_excluded_topics:
            return FilterResult(DROP, 0.0, ["topics 命中排除列表: " + ", ".join(hit_excluded_topics)], weak_signals)

        if candidate.markup_ratio is not None and candidate.markup_ratio > self.markup_ratio_max:
            return FilterResult(
                DROP,
                0.0,
                [f"标记语言（Markdown 等）占比 {candidate.markup_ratio:.0%} > {self.markup_ratio_max:.0%}，判定为文档/书籍类"],
                weak_signals,
            )

        weak_signals.extend(f"topics 含「{t}」" for t in sorted(topics_lower & self.weak_topics))
        if len(weak_signals) >= self.weak_min_hits:
            return FilterResult(DROP, 0.0, [f"弱信号合计 {len(weak_signals)} 个: " + "; ".join(weak_signals)], weak_signals)

        # 正向信号
        score = 0.0
        if candidate.has_manifest:
            found = ", ".join(candidate.manifests_found[:3]) or "依赖清单"
            score += self.positive_weights.get("manifest", 0.0)
            positives.append(f"含依赖清单({found})")
        if candidate.has_release:
            score += self.positive_weights.get("release", 0.0)
            positives.append("有 Release")
        matched_topics = sorted(topics_lower & self.software_topics)
        if matched_topics:
            score += self.positive_weights.get("topics", 0.0)
            positives.append("topics 命中软件白名单: " + ", ".join(matched_topics[:4]))
        if candidate.language and candidate.language not in self.markup_languages:
            score += self.positive_weights.get("code_language", 0.0)
            positives.append(f"主语言为代码语言({candidate.language})")
        elif not candidate.language:
            reasons.append("缺少主语言信息")
        if candidate.license and not candidate.archived:
            score += self.positive_weights.get("license", 0.0)
            positives.append(f"有 License({candidate.license})")

        pushed = _parse_date(candidate.pushed_at)
        if pushed and (today - pushed) <= timedelta(days=30):
            score += self.positive_weights.get("active", 0.0)
            positives.append(f"近 30 天有更新({pushed.isoformat()})")

        # 灰区扣分
        if weak_signals:
            score -= self.weak_penalty
            reasons.append(f"弱信号 {len(weak_signals)} 个，扣 {self.weak_penalty:.2f}")
        if (
            candidate.notebook_ratio is not None
            and candidate.notebook_ratio >= self.notebook_ratio_gray
            and candidate.has_manifest is False
        ):
            score -= self.gray_penalty
            reasons.append(f"笔记本占比 {candidate.notebook_ratio:.0%} 且无依赖清单，扣 {self.gray_penalty:.2f}")
        if (
            candidate.html_ratio is not None
            and candidate.html_ratio >= self.html_ratio_gray
            and candidate.has_manifest is False
            and candidate.has_release is False
        ):
            score -= self.gray_penalty
            reasons.append(f"HTML 占比 {candidate.html_ratio:.0%} 且无清单/发布，扣 {self.gray_penalty:.2f}")

        score = max(0.0, min(1.0, score))

        if score >= self.keep_threshold:
            status = KEEP
        elif score >= self.gray_threshold:
            status = GRAY
            reasons.append(f"software_score {score:.2f} 落在灰区（{self.gray_threshold:.2f}-{self.keep_threshold:.2f}）")
        else:
            status = DROP
            reasons.append(f"software_score {score:.2f} < {self.gray_threshold:.2f}")

        return FilterResult(status, score, reasons, weak_signals, positives)

    # ------------------------------------------------------------------ 写回
    @staticmethod
    def apply(candidate: Candidate, result: FilterResult) -> None:
        candidate.software_status = result.status
        candidate.software_score = result.score
        candidate.filter_reasons = result.reasons
        candidate.weak_signals = result.weak_signals

    @staticmethod
    def _compile_words(words: list[str]) -> list[tuple[str, re.Pattern[str]]]:
        compiled: list[tuple[str, re.Pattern[str]]] = []
        for word in words:
            # 词边界匹配：避免 book 命中 notebook、notes 命中 noteshare
            pattern = re.compile(rf"(?<![a-z0-9]){re.escape(word.lower())}(?![a-z0-9])")
            compiled.append((word, pattern))
        return compiled


def _parse_date(value: str | None) -> date | None:
    from .util import parse_iso_date

    return parse_iso_date(value)

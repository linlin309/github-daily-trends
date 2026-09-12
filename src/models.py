"""数据模型：整个管道里流动的唯一对象 Candidate。

字段分四段：
1) 采集阶段（Trending / Search 提供）
2) 补全阶段（GitHub API 提供）
3) 计算阶段（规则过滤、分类、打分、选择）
4) 历史阶段（由历史 JSON 推导）
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Any


@dataclass
class Candidate:
    # ---- 1) 采集 ----
    full_name: str
    url: str
    owner: str = ""
    name: str = ""
    sources: list[str] = field(default_factory=list)
    source_ranks: dict[str, int] = field(default_factory=dict)
    source_weight: float = 0.5
    best_rank: int | None = None
    description: str | None = None
    language: str | None = None
    stars: int | None = None
    stars_today: int | None = None
    stars_this_week: int | None = None
    forks: int | None = None

    # ---- 2) 补全（GitHub API）----
    topics: list[str] = field(default_factory=list)
    license: str | None = None
    archived: bool | None = None
    created_at: str | None = None
    pushed_at: str | None = None
    updated_at: str | None = None
    size_kb: int | None = None
    open_issues: int | None = None
    has_manifest: bool | None = None
    manifests_found: list[str] = field(default_factory=list)
    has_release: bool | None = None
    latest_release_at: str | None = None
    languages_bytes: dict[str, int] = field(default_factory=dict)
    markup_ratio: float | None = None
    notebook_ratio: float | None = None
    html_ratio: float | None = None

    # ---- 3) 计算 ----
    software_score: float = 0.0
    software_status: str = "unknown"  # keep / gray / drop
    filter_reasons: list[str] = field(default_factory=list)
    weak_signals: list[str] = field(default_factory=list)
    injection_suspected: bool = False
    group: str = "other"
    category: str = "other"
    trend_score: float = 0.0
    score_breakdown: dict[str, Any] = field(default_factory=dict)
    velocity_value: float | None = None   # 归一化前使用的"当日增长"数值
    velocity_source: str | None = None    # trending / history_delta / weekly_avg / unavailable
    adjusted_score: float | None = None
    penalties: dict[str, float] = field(default_factory=dict)

    # ---- 4) 历史 ----
    first_seen: str | None = None
    days_since_first_seen: int | None = None
    trending_streak_days: int = 0
    recently_reported: bool = False

    # ---- AI ----
    ai: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    rank: int | None = None  # 最终报告中的序号

    # ---- 运维 ----
    errors: list[str] = field(default_factory=list)

    @property
    def is_enriched(self) -> bool:
        return bool(self.topics) or self.languages_bytes or self.pushed_at is not None

    def add_error(self, message: str) -> None:
        if message not in self.errors:
            self.errors.append(message)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Candidate":
        """从历史 JSON 还原（用于 --send-only 重新发送邮件）。"""
        payload = dict(data or {})
        if "stars_today_source" in payload:
            payload.setdefault("velocity_source", payload.pop("stars_today_source"))
        payload.pop("stars_today_source", None)
        valid = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in payload.items() if k in valid})

    def to_dict(self) -> dict[str, Any]:
        return {
            "rank": self.rank,
            "full_name": self.full_name,
            "url": self.url,
            "description": self.description,
            "stars": self.stars,
            "stars_today": self.stars_today,
            "stars_this_week": self.stars_this_week,
            "stars_today_source": self.velocity_source,
            "velocity_value": None if self.velocity_value is None else round(self.velocity_value, 1),
            "forks": self.forks,
            "language": self.language,
            "topics": self.topics,
            "created_at": self.created_at,
            "pushed_at": self.pushed_at,
            "license": self.license,
            "archived": self.archived,
            "open_issues": self.open_issues,
            "has_manifest": self.has_manifest,
            "manifests_found": self.manifests_found,
            "has_release": self.has_release,
            "latest_release_at": self.latest_release_at,
            "markup_ratio": self.markup_ratio,
            "group": self.group,
            "category": self.category,
            "software_score": round(self.software_score, 3),
            "software_status": self.software_status,
            "trend_score": round(self.trend_score, 2),
            "score_breakdown": self.score_breakdown,
            "adjusted_score": None if self.adjusted_score is None else round(self.adjusted_score, 2),
            "penalties": self.penalties,
            "sources": sorted(self.sources),
            "source_ranks": self.source_ranks,
            "best_rank": self.best_rank,
            "first_seen": self.first_seen,
            "days_since_first_seen": self.days_since_first_seen,
            "trending_streak_days": self.trending_streak_days,
            "recently_reported": self.recently_reported,
            "injection_suspected": self.injection_suspected,
            "tags": self.tags,
            "ai": self.ai,
            "errors": self.errors,
        }

    def to_llm_payload(self, max_description_chars: int) -> dict[str, Any]:
        """交给 LLM 的最小结构化字段（不含 README、不含任何指令性内容）。"""
        from .util import sanitize_text

        return {
            "full_name": self.full_name,
            "description": sanitize_text(self.description, max_description_chars),
            "language": self.language or "unknown",
            "topics": self.topics[:20],
            "stars": self.stars,
            "stars_today": self.stars_today,
            "stars_this_week": self.stars_this_week,
            "stars_today_source": self.velocity_source,
            "forks": self.forks,
            "created_at": self.created_at,
            "pushed_at": self.pushed_at,
            "has_release": self.has_release,
            "latest_release_at": self.latest_release_at,
            "first_seen": self.first_seen,
            "trending_streak_days": self.trending_streak_days,
            "category": self.category,
            "sources": sorted(self.sources),
        }

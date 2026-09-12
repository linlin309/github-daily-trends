"""Trend Score：程序计算的客观热度分。

原则：
- 全部使用固定参考值归一化（velocity_cap / scale_cap / rank_ref），
  不使用"当天最大值"，否则分数无法跨天比较。
- 每个分项都写进 score_breakdown，可复盘"为什么它排第 3"。
- 缺失数据不猜测：无当日增长时用中性值并如实标注 velocity_source。
"""

from __future__ import annotations

import math
from datetime import date

from .history import HistoryView
from .models import Candidate


class TrendScorer:
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        weights = cfg.get("weights") or {}
        self.w_velocity = float(weights.get("velocity", 0.40))
        self.w_scale = float(weights.get("scale", 0.20))
        self.w_rank = float(weights.get("rank", 0.15))
        self.w_freshness = float(weights.get("freshness", 0.15))
        self.w_source = float(weights.get("source", 0.10))
        self.velocity_cap = float(cfg.get("velocity_cap", 3000))
        self.scale_cap = float(cfg.get("scale_cap", 200000))
        self.rank_ref = float(cfg.get("rank_ref", 25))
        self.missing_velocity_norm = float(cfg.get("missing_velocity_norm", 0.35))
        self.missing_rank_norm = float(cfg.get("missing_rank_norm", 0.5))
        fresh = cfg.get("freshness") or {}
        self.fresh_first = float(fresh.get("first_seen", 1.0))
        self.fresh_2_3 = float(fresh.get("seen_2_3_days", 0.6))
        self.fresh_4_plus = float(fresh.get("seen_4_plus_days", 0.4))

    # ------------------------------------------------------------------ 工具
    @staticmethod
    def _norm_log1p(value: float | None, cap: float) -> float:
        if value is None or value <= 0 or cap <= 0:
            return 0.0
        return min(1.0, math.log1p(value) / math.log1p(cap))

    def _norm_scale(self, stars: int | None) -> float:
        if not stars or stars <= 0:
            return 0.0
        return min(1.0, math.log10(stars) / math.log10(self.scale_cap))

    def _norm_rank(self, rank: int | None) -> float:
        if rank is None:
            return self.missing_rank_norm
        return max(0.0, min(1.0, 1.0 - (rank - 1) / self.rank_ref))

    def _freshness(self, days_since_first_seen: int) -> float:
        if days_since_first_seen <= 1:
            return self.fresh_first
        if days_since_first_seen <= 3:
            return self.fresh_2_3
        return self.fresh_4_plus

    # ------------------------------------------------------------------ 主流程
    def compute(
        self,
        candidate: Candidate,
        *,
        history: HistoryView | None,
        today: date,
        repeat_lookback_days: int = 3,
    ) -> None:
        # 1) 当日增长的三个来源，优先级：Trending → 历史差值 → 本周均值
        velocity = None
        if candidate.stars_today is not None:
            velocity, candidate.velocity_source = float(candidate.stars_today), "trending"
        elif history is not None and candidate.stars:
            previous = history.previous_stars(candidate.full_name, today)
            if previous is not None:
                velocity = float(max(0, candidate.stars - previous))
                candidate.velocity_source = "history_delta"
        if velocity is None and candidate.stars_this_week is not None:
            velocity = candidate.stars_this_week / 7.0
            candidate.velocity_source = "weekly_avg"
        if velocity is None:
            candidate.velocity_source = "unavailable"
        candidate.velocity_value = velocity

        velocity_norm = (
            self._norm_log1p(velocity, self.velocity_cap) if velocity is not None else self.missing_velocity_norm
        )
        scale_norm = self._norm_scale(candidate.stars)
        rank_norm = self._norm_rank(candidate.best_rank)

        # 2) 历史：首次上榜 / 连续上榜
        if history is not None:
            first_seen = history.first_seen(candidate.full_name)
            candidate.first_seen = first_seen.isoformat() if first_seen else today.isoformat()
            days_since = (today - first_seen).days if first_seen else 0
            candidate.days_since_first_seen = max(0, days_since)
            candidate.trending_streak_days = max(1, history.streak_days(candidate.full_name, today))
            candidate.recently_reported = history.was_selected_recently(
                candidate.full_name, today, repeat_lookback_days
            )
        else:
            candidate.first_seen = today.isoformat()
            candidate.days_since_first_seen = 0
            candidate.trending_streak_days = 1
            candidate.recently_reported = False

        freshness_norm = self._freshness(candidate.days_since_first_seen)
        source_norm = max(0.0, min(1.0, float(candidate.source_weight)))

        total = (
            self.w_velocity * velocity_norm
            + self.w_scale * scale_norm
            + self.w_rank * rank_norm
            + self.w_freshness * freshness_norm
            + self.w_source * source_norm
        )
        candidate.trend_score = round(100.0 * total, 2)
        candidate.score_breakdown = {
            "velocity": {
                "value": None if velocity is None else round(velocity, 1),
                "source": candidate.velocity_source,
                "norm": round(velocity_norm, 4),
                "weight": self.w_velocity,
                "contribution": round(100 * self.w_velocity * velocity_norm, 2),
            },
            "scale": {
                "value": candidate.stars,
                "norm": round(scale_norm, 4),
                "weight": self.w_scale,
                "contribution": round(100 * self.w_scale * scale_norm, 2),
            },
            "rank": {
                "value": candidate.best_rank,
                "norm": round(rank_norm, 4),
                "weight": self.w_rank,
                "contribution": round(100 * self.w_rank * rank_norm, 2),
            },
            "freshness": {
                "days_since_first_seen": candidate.days_since_first_seen,
                "norm": round(freshness_norm, 4),
                "weight": self.w_freshness,
                "contribution": round(100 * self.w_freshness * freshness_norm, 2),
            },
            "source": {
                "value": sorted(candidate.sources),
                "weight": self.w_source,
                "norm": round(source_norm, 4),
                "contribution": round(100 * self.w_source * source_norm, 2),
            },
        }

"""多样性选择：热度优先 + 两级软惩罚 + 质量下限。

不是硬配额：同类项目越多，后续同类项目的"调整后分数"越低（粗类按幂次增长），
因此不会出现"10 个全是 AI"，也不会为了凑类别而把冷门项目抬进来。
低于质量下限（floor）就停止，宁可少写。

同一输入必然产生同一输出（可复现）：排序键是 (-trend_score, full_name)。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import Candidate

TARGET_REACHED = "target_reached"
BELOW_FLOOR = "below_floor"
EXHAUSTED = "exhausted"


@dataclass
class SelectionOutcome:
    selected: list[Candidate] = field(default_factory=list)
    stopped_reason: str = EXHAUSTED
    considered: int = 0
    below_floor_skipped: int = 0

    @property
    def count(self) -> int:
        return len(self.selected)


class DiversitySelector:
    def __init__(self, cfg: dict) -> None:
        self.group_penalty = float(cfg.get("group_penalty", 10.0))
        self.group_exponent = float(cfg.get("group_penalty_exponent", 1.2))
        self.category_penalty = float(cfg.get("category_penalty", 4.0))
        self.language_penalty = float(cfg.get("language_penalty", 3.0))
        self.repeat_penalty = float(cfg.get("repeat_penalty", 12.0))
        self.unassessed_penalty = float(cfg.get("unassessed_penalty", 10.0))
        self.gray_penalty = float(cfg.get("gray_penalty", 4.0))
        self.hard_group_cap = cfg.get("hard_group_cap")

    def penalties_for(self, candidate: Candidate, selected: list[Candidate]) -> dict[str, float]:
        """惩罚形状：同类第 2 个免费，第 3 个开始付出代价，越堆越贵。

        - 粗类（group）：base × (n-1)^1.25 —— 第 3 个同类 ≈ -14，第 4 个 ≈ -33，第 5 个 ≈ -55
        - 细类（category）：base × (n-1) —— 完全相同的类别才累积
        - 同语言：base × (n-1) —— 前两个同语言项目不受影响
        - 近 days 天已报告过：一次性 -12（提高新鲜度，但热度足够高仍能入选）
        """
        group_count = sum(1 for s in selected if s.group == candidate.group)
        category_count = sum(1 for s in selected if s.category == candidate.category)
        language_count = sum(
            1 for s in selected if candidate.language and s.language == candidate.language
        )
        penalties = {
            "group": self.group_penalty * max(0, group_count - 1) ** self.group_exponent,
            "category": self.category_penalty * max(0, category_count - 1),
            "language": self.language_penalty * max(0, language_count - 1),
            "repeat": self.repeat_penalty if candidate.recently_reported else 0.0,
        }
        # 未核实（缺少 API 元数据）的项目优先让位给已核实的软件；
        # 灰区项目只是轻微降权，不会因为"不够确定"就被排除。
        if not candidate.is_enriched:
            penalties["unassessed"] = self.unassessed_penalty
        elif candidate.software_status == "gray":
            penalties["gray"] = self.gray_penalty
        if self.hard_group_cap is not None and group_count >= int(self.hard_group_cap):
            penalties["group_hard_cap"] = 1_000_000.0
        return {k: round(v, 2) for k, v in penalties.items() if v}

    def select(self, candidates: list[Candidate], *, target: int, floor: float) -> SelectionOutcome:
        outcome = SelectionOutcome(considered=len(candidates))
        remaining = sorted(candidates, key=lambda c: (-c.trend_score, c.full_name))

        while remaining and len(outcome.selected) < target:
            best: Candidate | None = None
            best_adjusted: float | None = None
            best_penalties: dict[str, float] = {}

            for candidate in remaining:
                penalties = self.penalties_for(candidate, outcome.selected)
                adjusted = candidate.trend_score - sum(penalties.values())
                if best_adjusted is None or adjusted > best_adjusted:
                    best, best_adjusted, best_penalties = candidate, adjusted, penalties

            assert best is not None and best_adjusted is not None
            if best_adjusted < floor:
                outcome.stopped_reason = BELOW_FLOOR
                outcome.below_floor_skipped = len(remaining)
                break

            best.adjusted_score = round(best_adjusted, 2)
            best.penalties = best_penalties
            outcome.selected.append(best)
            remaining.remove(best)
        else:
            outcome.stopped_reason = TARGET_REACHED if len(outcome.selected) >= target else EXHAUSTED

        for index, candidate in enumerate(outcome.selected, start=1):
            candidate.rank = index
        return outcome

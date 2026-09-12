"""项目分类：粗类 group（用于多样性主惩罚）+ 细类 category（报告展示与次惩罚）。

规则配置化（config.yaml 的 classify 段），匹配顺序：
  1. topics 精确命中
  2. topics 关键词命中
  3. name + description 关键词命中
  4. 主语言兜底
  5. other
"""

from __future__ import annotations

from .models import Candidate


class Classifier:
    def __init__(self, cfg: dict) -> None:
        self.categories: list[dict] = list(cfg.get("categories") or [])
        self.fallback_category: str = cfg.get("fallback_category", "other")
        self.fallback_group: str = cfg.get("fallback_group", "other")
        self.language_fallback: dict[str, str] = dict(cfg.get("language_fallback") or {})
        self._group_of: dict[str, str] = {cat["name"]: cat.get("group", self.fallback_group) for cat in self.categories}
        self._topic_sets: list[tuple[str, str, set[str]]] = [
            (cat["name"], cat.get("group", self.fallback_group), {t.lower() for t in (cat.get("topics") or [])})
            for cat in self.categories
        ]
        self._keyword_sets: list[tuple[str, str, list[str]]] = [
            (cat["name"], cat.get("group", self.fallback_group), [k.lower() for k in (cat.get("keywords") or [])])
            for cat in self.categories
        ]

    def classify(self, candidate: Candidate) -> tuple[str, str]:
        topics = {t.lower() for t in candidate.topics}

        # 1) topics 精确命中
        for name, group, topic_set in self._topic_sets:
            if topics & topic_set:
                return name, group

        # 2) topics 关键词命中
        topic_text = " ".join(sorted(topics))
        if topic_text:
            for name, group, keywords in self._keyword_sets:
                if any(word in topic_text for word in keywords):
                    return name, group

        # 3) name + description 关键词命中
        text = f"{candidate.name} {candidate.description or ''}".lower()
        for name, group, keywords in self._keyword_sets:
            if any(word in text for word in keywords):
                return name, group

        # 4) 主语言兜底
        if candidate.language:
            mapped = self.language_fallback.get(candidate.language)
            if mapped:
                return mapped, self._group_of.get(mapped, self.fallback_group)

        return self.fallback_category, self.fallback_group

    def apply(self, candidate: Candidate) -> None:
        candidate.category, candidate.group = self.classify(candidate)

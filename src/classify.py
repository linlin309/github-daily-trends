"""项目分类：粗类 group（用于多样性主惩罚）+ 细类 category（报告展示与次惩罚）。

规则配置化（config.yaml 的 classify 段），匹配顺序：
  1. topics 精确命中 —— 命中多个类别时按"特异性"取最高，而不是取配置里第一个。
     因为 docker/linux/windows/cloud 这类泛化 topic 会出现在大量无关项目上，
     谁先写在配置里谁就赢，会把 ai-agents 项目判成 devops-cloud。
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
        # 泛化 topic 权重低于专指 topic：命中 ai-agents 比命中 docker 更能说明项目类别
        self._generic_topics = {t.lower() for t in (cfg.get("generic_topics") or [])}
        self._generic_weight = float(cfg.get("generic_topic_weight", 0.25))
        self._keyword_sets: list[tuple[str, str, list[str]]] = [
            (cat["name"], cat.get("group", self.fallback_group), [k.lower() for k in (cat.get("keywords") or [])])
            for cat in self.categories
        ]

    def _topic_score(self, hits: set[str]) -> float:
        """专指 topic 记 1 分，泛化 topic 记很低的分。

        一个项目常常同时命中多个类别（ai-agents + docker + electron），
        取"总分最高"而不是"配置里第一个"，否则宽泛词会压过具体词。
        """
        return sum(self._generic_weight if t in self._generic_topics else 1.0 for t in hits)

    def classify(self, candidate: Candidate) -> tuple[str, str]:
        topics = {t.lower() for t in candidate.topics}

        # 1) topics 精确命中：按特异性打分取最高，同分时保留配置顺序（先写的优先）
        best: tuple[float, str, str] | None = None
        for name, group, topic_set in self._topic_sets:
            hits = topics & topic_set
            if not hits:
                continue
            score = self._topic_score(hits)
            if best is None or score > best[0]:
                best = (score, name, group)
        if best is not None:
            return best[1], best[2]

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

"""Trending 采集与候选池合并。

- parse_trending_html 是纯函数，可用 fixture 做单元测试（第一步就要能测）。
- parse health check：行数区间 + 字段覆盖率，任一不达标就抛错交给上层兜底，
  绝不产出"看起来正常但全是空"的候选池。
- merge_entries：以 full_name 为唯一键合并多来源，来源权重取最大值（不累加）。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Iterable

from bs4 import BeautifulSoup

from .models import Candidate
from .net import HttpClient, HttpError
from .util import sanitize_text

_WS = re.compile(r"\s+")
_INT = re.compile(r"[\d,]+")

MIN_ROWS = 5
MAX_ROWS = 60
MIN_FIELD_COVERAGE = 0.8


class TrendingParseError(RuntimeError):
    """页面结构变化或健康检查未通过。"""


@dataclass
class TrendingEntry:
    full_name: str
    url: str
    rank: int
    description: str | None = None
    language: str | None = None
    stars: int | None = None
    forks: int | None = None
    stars_today: int | None = None
    stars_this_week: int | None = None


@dataclass
class SourceSpec:
    label: str
    url: str
    weight_key: str
    descriptions: list[str] = field(default_factory=list)


def _parse_int(text: str | None) -> int | None:
    if not text:
        return None
    match = _INT.search(text.replace(",", "").replace(" ", ""))
    if not match:
        return None
    try:
        return int(match.group(0))
    except ValueError:
        return None


def _normalize_repo_name(text: str | None) -> str | None:
    """'owner / repo' + 换行 → 'owner/repo'。"""
    if not text:
        return None
    collapsed = _WS.sub("", text)
    if collapsed.count("/") != 1:
        return None
    owner, name = collapsed.split("/")
    if not owner or not name:
        return None
    return f"{owner}/{name}"


def parse_trending_html(html: str, *, since: str = "daily") -> list[TrendingEntry]:
    """把 Trending 页面解析成结构化条目。纯函数，便于 fixture 测试。"""
    soup = BeautifulSoup(html, "html.parser")
    entries: list[TrendingEntry] = []

    for index, row in enumerate(soup.select("article.Box-row"), start=1):
        link = row.select_one("h2 a")
        full_name = _normalize_repo_name(link.get_text() if link else None)
        if not full_name:
            continue

        description_el = row.select_one("p.col-9") or row.select_one("p")
        description = description_el.get_text(" ", strip=True) if description_el else None

        language_el = row.select_one('[itemprop="programmingLanguage"]')
        language = language_el.get_text(strip=True) if language_el else None

        stars_el = row.select_one('a[href$="/stargazers"]')
        forks_el = row.select_one('a[href$="/forks"]')
        today_el = row.select_one("span.float-sm-right")

        stars_today = stars_this_week = None
        if today_el:
            today_text = today_el.get_text(" ", strip=True)
            value = _parse_int(today_text)
            if value is not None:
                lowered = today_text.lower()
                if "week" in lowered:
                    stars_this_week = value
                else:
                    stars_today = value

        entries.append(
            TrendingEntry(
                full_name=full_name,
                url=f"https://github.com/{full_name}",
                rank=index,
                description=description,
                language=language,
                stars=_parse_int(stars_el.get_text(strip=True) if stars_el else None),
                forks=_parse_int(forks_el.get_text(strip=True) if forks_el else None),
                stars_today=stars_today,
                stars_this_week=stars_this_week,
            )
        )

    assert_parse_health(entries, html, since=since)
    return entries


def assert_parse_health(entries: list[TrendingEntry], html: str, *, since: str = "daily") -> None:
    if not entries:
        # 页面本身可能有 0 个项目（GitHub 偶发），但若容器还在而选择器失效，必须报错
        if "Box-row" in html:
            raise TrendingParseError(f"Trending({since}) 页面含数据容器但解析结果为 0，选择器可能已失效")
        return

    if not (MIN_ROWS <= len(entries) <= MAX_ROWS):
        raise TrendingParseError(f"Trending({since}) 解析出行数异常: {len(entries)}（期望 {MIN_ROWS}-{MAX_ROWS}）")

    covered = sum(1 for e in entries if e.full_name and (e.stars_today or e.stars_this_week or e.stars))
    coverage = covered / len(entries)
    if coverage < MIN_FIELD_COVERAGE:
        raise TrendingParseError(f"Trending({since}) 字段覆盖率过低: {coverage:.0%}")


def merge_entries(scored_entries: Iterable[tuple[SourceSpec, TrendingEntry]], weights: dict[str, float]) -> list[Candidate]:
    """以 full_name 为唯一键合并多来源。来源权重取最大值，不累加。"""
    merged: dict[str, Candidate] = {}

    for spec, entry in scored_entries:
        key = entry.full_name.lower()
        candidate = merged.get(key)
        if candidate is None:
            candidate = Candidate(
                full_name=entry.full_name,
                url=entry.url,
                owner=entry.full_name.split("/")[0],
                name=entry.full_name.split("/")[1],
                description=entry.description,
                language=entry.language,
                stars=entry.stars,
                forks=entry.forks,
            )
            merged[key] = candidate

        if spec.label not in candidate.sources:
            candidate.sources.append(spec.label)
        candidate.source_ranks[spec.label] = entry.rank
        candidate.source_weight = max(candidate.source_weight, float(weights.get(spec.weight_key, 0.5)))
        candidate.best_rank = entry.rank if candidate.best_rank is None else min(candidate.best_rank, entry.rank)

        # 每日来源优先提供 stars_today；weekly 提供 stars_this_week
        if entry.stars_today is not None and candidate.stars_today is None:
            candidate.stars_today = entry.stars_today
        if entry.stars_this_week is not None and candidate.stars_this_week is None:
            candidate.stars_this_week = entry.stars_this_week
        if candidate.description is None and entry.description:
            candidate.description = entry.description
        if candidate.language is None and entry.language:
            candidate.language = entry.language
        if candidate.stars is None and entry.stars is not None:
            candidate.stars = entry.stars
        if candidate.forks is None and entry.forks is not None:
            candidate.forks = entry.forks

    return sorted(merged.values(), key=lambda c: c.best_rank if c.best_rank is not None else 999)


def fixture_path(fixtures_dir, label: str):
    """按 label 找稳定的 fixture 文件（支持带日期后缀，取最新一份）。"""
    from pathlib import Path

    base = Path(fixtures_dir)
    key = label.replace(":", "_")
    exact = base / f"trending_{key}.html"
    if exact.is_file():
        return exact
    matches = sorted(base.glob(f"trending_{key}_*.html"))
    return matches[-1] if matches else None


def collect_from_fixtures(fixtures_dir, sources_cfg: dict, logger: logging.Logger, *, max_description_chars: int = 300):
    """离线模式：只用本地 fixture 跑通全流程（供 dry-run 与测试使用，不访问网络）。"""
    weights = sources_cfg.get("weights") or {}
    pairs: list[tuple[SourceSpec, TrendingEntry]] = []
    ok: list[str] = []
    failed: list[str] = []

    for spec in TrendingCollector.build_specs_static(sources_cfg):
        path = fixture_path(fixtures_dir, spec.label)
        if path is None:
            failed.append(f"{spec.label}: 缺少 fixture")
            continue
        since = "weekly" if "weekly" in spec.url else "daily"
        entries = parse_trending_html(path.read_text(encoding="utf-8"), since=since)
        for entry in entries:
            if entry.description:
                entry.description = sanitize_text(entry.description, max_description_chars)
        pairs.extend((spec, entry) for entry in entries)
        ok.append(spec.label)

    if not pairs:
        return [], ok, failed
    return merge_entries(pairs, weights), ok, failed


class TrendingCollector:
    """按配置抓取 daily / weekly / 语言页（可选中文区），全部失败时抛错由上层兜底。"""

    def __init__(self, http: HttpClient, sources_cfg: dict, logger: logging.Logger, *, max_description_chars: int = 300) -> None:
        self.http = http
        self.cfg = sources_cfg
        self.log = logger
        self.max_description_chars = max_description_chars

    @staticmethod
    def build_specs_static(sources_cfg: dict) -> list[SourceSpec]:
        specs: list[SourceSpec] = [
            SourceSpec("daily", sources_cfg["daily_url"], "daily"),
            SourceSpec("weekly", sources_cfg["weekly_url"], "weekly"),
        ]
        template = sources_cfg.get("language_url_template")
        for language in sources_cfg.get("languages") or []:
            if template:
                specs.append(SourceSpec(f"lang:{language}", template.format(language=language), "language"))
        spoken = (sources_cfg.get("spoken_language_url") or "").strip()
        if spoken:
            specs.append(SourceSpec("spoken", spoken, "spoken"))
        return specs

    def build_specs(self) -> list[SourceSpec]:
        return self.build_specs_static(self.cfg)

    def collect(self) -> tuple[list[Candidate], list[str], list[str]]:
        """返回 (候选列表, 成功来源, 失败来源)。"""
        weights = self.cfg.get("weights") or {}
        pairs: list[tuple[SourceSpec, TrendingEntry]] = []
        ok: list[str] = []
        failed: list[str] = []

        for spec in self.build_specs():
            since = "weekly" if "weekly" in spec.url else "daily"
            try:
                html = self.http.get_text(spec.url)
                entries = parse_trending_html(html, since=since)
            except (HttpError, TrendingParseError) as exc:
                self.log.error("来源 %s 采集失败: %s", spec.label, exc)
                failed.append(f"{spec.label}: {exc}")
                continue

            for entry in entries:
                if entry.description:
                    entry.description = sanitize_text(entry.description, self.max_description_chars)
            pairs.extend((spec, entry) for entry in entries)
            ok.append(spec.label)
            self.log.info("来源 %s 解析到 %d 个项目", spec.label, len(entries))

        if not pairs:
            return [], ok, failed

        candidates = merge_entries(pairs, weights)
        self.log.info("去重后候选池: %d 个项目（成功来源 %d 个）", len(candidates), len(ok))
        return candidates, ok, failed

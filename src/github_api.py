"""GitHub REST API：数据补全 + Search 兜底。

设计要点：
- 使用 Actions 自动注入的 GITHUB_TOKEN（1000 请求/小时/仓库），无需额外 PAT。
- 每个项目独立容错：单个项目失败只记录 errors，绝不中断整份日报。
- 调用前检查 /rate_limit（该接口不消耗额度），额度不足时优雅降级。
- Search API 只作兜底，且每天最多 2 次（配置可调），不做 Trending 的等价替代。
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from .filtering import compute_language_ratios
from .models import Candidate
from .net import HttpClient, HttpError
from .util import parse_iso_date, sanitize_text


class GithubClient:
    def __init__(
        self,
        http: HttpClient,
        cfg: dict,
        *,
        token: str | None,
        logger: logging.Logger | None = None,
        max_description_chars: int = 300,
    ) -> None:
        self.http = http
        self.cfg = cfg
        self.token = (token or "").strip() or None
        self.log = logger or logging.getLogger("github")
        self.api_base = cfg.get("api_base", "https://api.github.com").rstrip("/")
        self.max_description_chars = max_description_chars
        self.unauth = self.token is None
        self.remaining: int | None = None
        self.search_remaining: int | None = None

    # ------------------------------------------------------------------ 基础
    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github.mercy-preview+json, application/vnd.github+json",
            "X-GitHub-Api-Version": self.cfg.get("api_version", "2022-11-28"),
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def check_rate_limit(self) -> None:
        try:
            payload, _ = self.http.get_json_with_headers(f"{self.api_base}/rate_limit", headers=self._headers())
        except HttpError as exc:
            self.log.warning("读取 rate_limit 失败: %s", exc)
            return
        resources = (payload or {}).get("resources") or {}
        core = resources.get("core") or {}
        search = resources.get("search") or {}
        self.remaining = core.get("remaining")
        self.search_remaining = search.get("remaining")
        self.log.info(
            "GitHub 限额：core 剩余 %s（认证=%s），search 剩余 %s",
            self.remaining,
            "是" if not self.unauth else "否",
            self.search_remaining,
        )

    def enrich_plan(self, candidates: list[Candidate]) -> int:
        """决定本次最多补全多少个项目。"""
        cap = int(self.cfg.get("max_enrich_repos", 60))
        if self.unauth:
            cap = min(cap, int(self.cfg.get("max_unauth_enrich", 8)))
            self.log.warning("未提供 GITHUB_TOKEN，本次最多补全 %d 个项目（未认证限额仅 60/小时）", cap)
        if self.remaining is not None:
            floor = int(self.cfg.get("min_rate_remaining", 30))
            affordable = max(0, (self.remaining - floor) // 4)
            if affordable < cap:
                self.log.warning("限额剩余 %s，补全数量从 %d 下调到 %d", self.remaining, cap, affordable)
                cap = affordable
        return cap

    # ------------------------------------------------------------------ 单项目补全
    def enrich(self, candidate: Candidate, *, filter_cfg: dict, skip_manifests: bool = False) -> None:
        repo = self._fetch_repo(candidate)
        if repo is not None:
            self._apply_repo(candidate, repo)

        languages = self._try(lambda: self._fetch_languages(candidate), candidate, "languages")
        if languages is not None:
            candidate.languages_bytes = {k: int(v) for k, v in languages.items()}
            ratios = compute_language_ratios(candidate.languages_bytes, filter_cfg)
            candidate.markup_ratio = ratios["markup_ratio"]
            candidate.notebook_ratio = ratios["notebook_ratio"]
            candidate.html_ratio = ratios["html_ratio"]

        # 未认证或额度紧张时，省掉 contents / releases 两次调用（缺失不会导致误判：
        # 过滤逻辑只在明确拿到 False 时才扣分）
        if self.unauth or skip_manifests:
            return

        names = self._try(lambda: self._fetch_root_contents(candidate), candidate, "contents")
        if names is not None:
            found = [n for n in names if n in set(filter_cfg.get("manifests") or [])]
            candidate.has_manifest = bool(found)
            candidate.manifests_found = found

        release = self._try(lambda: self._fetch_latest_release(candidate), candidate, "releases")
        if release is not None:
            candidate.has_release = release[0]
            candidate.latest_release_at = release[1]

    def _fetch_repo(self, candidate: Candidate) -> dict | None:
        try:
            payload, headers = self.http.get_json_with_headers(
                f"{self.api_base}/repos/{candidate.full_name}", headers=self._headers()
            )
        except HttpError as exc:
            candidate.add_error(f"github_repo: {exc}")
            return None
        self._track(headers)
        return payload

    def _fetch_languages(self, candidate: Candidate) -> dict:
        payload, headers = self.http.get_json_with_headers(
            f"{self.api_base}/repos/{candidate.full_name}/languages", headers=self._headers()
        )
        self._track(headers)
        return payload or {}

    def _fetch_root_contents(self, candidate: Candidate) -> list[str]:
        payload, headers = self.http.get_json_with_headers(
            f"{self.api_base}/repos/{candidate.full_name}/contents/", headers=self._headers()
        )
        self._track(headers)
        if not isinstance(payload, list):
            return []
        return [str(item.get("name", "")) for item in payload if isinstance(item, dict)]

    def _fetch_latest_release(self, candidate: Candidate) -> tuple[bool, str | None]:
        try:
            payload, headers = self.http.get_json_with_headers(
                f"{self.api_base}/repos/{candidate.full_name}/releases",
                headers=self._headers(),
                params={"per_page": 1},
            )
        except HttpError as exc:
            # 404 表示没有 release，属于正常情况
            if "404" in str(exc):
                return False, None
            raise
        self._track(headers)
        if isinstance(payload, list) and payload:
            return True, payload[0].get("published_at") or payload[0].get("created_at")
        return False, None

    def _apply_repo(self, candidate: Candidate, repo: dict) -> None:
        candidate.stars = repo.get("stargazers_count", candidate.stars)
        candidate.forks = repo.get("forks_count", candidate.forks)
        candidate.open_issues = repo.get("open_issues_count")
        candidate.language = repo.get("language") or candidate.language
        candidate.topics = [str(t) for t in (repo.get("topics") or [])]
        candidate.archived = bool(repo.get("archived"))
        candidate.created_at = repo.get("created_at")
        candidate.pushed_at = repo.get("pushed_at")
        candidate.updated_at = repo.get("updated_at")
        candidate.size_kb = repo.get("size")
        license_info = repo.get("license") or {}
        candidate.license = license_info.get("spdx_id") if isinstance(license_info, dict) else None
        if candidate.license in (None, "NOASSERTION"):
            candidate.license = None
        if not candidate.description:
            candidate.description = sanitize_text(repo.get("description"), self.max_description_chars)

    def _try(self, func, candidate: Candidate, label: str):
        try:
            return func()
        except HttpError as exc:
            candidate.add_error(f"github_{label}: {exc}")
            self.log.warning("%s 的 %s 补全失败: %s", candidate.full_name, label, exc)
            return None

    def _track(self, headers: dict[str, str]) -> None:
        raw = headers.get("X-RateLimit-Remaining") or headers.get("x-ratelimit-remaining")
        if raw is not None:
            try:
                self.remaining = int(raw)
            except ValueError:
                pass

    # ------------------------------------------------------------------ Search 兜底
    def search_repositories(
        self,
        query: str,
        *,
        sort: str = "stars",
        per_page: int = 30,
        weights: dict[str, float] | None = None,
        max_description_chars: int = 300,
    ) -> list[Candidate]:
        if self.search_remaining is not None and self.search_remaining < 1:
            self.log.warning("Search 限额不足，跳过兜底查询")
            return []
        try:
            payload, headers = self.http.get_json_with_headers(
                f"{self.api_base}/search/repositories",
                headers=self._headers(),
                params={"q": query, "sort": sort, "order": "desc", "per_page": per_page},
            )
        except HttpError as exc:
            self.log.error("Search API 调用失败(%s): %s", query, exc)
            return []
        self._track(headers)

        weight = float((weights or {}).get("search", 0.5))
        results: list[Candidate] = []
        for index, item in enumerate((payload or {}).get("items") or [], start=1):
            full_name = item.get("full_name")
            if not full_name:
                continue
            owner, _, name = full_name.partition("/")
            results.append(
                Candidate(
                    full_name=full_name,
                    url=item.get("html_url") or f"https://github.com/{full_name}",
                    owner=owner,
                    name=name,
                    description=sanitize_text(item.get("description"), max_description_chars),
                    language=item.get("language"),
                    stars=item.get("stargazers_count"),
                    forks=item.get("forks_count"),
                    sources=["search"],
                    source_ranks={"search": index},
                    source_weight=weight,
                    best_rank=index,
                )
            )
        self.log.info("Search 兜底(%s)返回 %d 个项目", query, len(results))
        return results


def build_search_queries(cfg: dict, today: date) -> list[tuple[str, str]]:
    """把配置里的 query 模板展开成 (name, q)。"""
    search_cfg = cfg.get("search") or {}
    if not search_cfg.get("enabled", True):
        return []
    substitutions = {
        "date_minus_7": (today - timedelta(days=7)).isoformat(),
        "date_minus_3": (today - timedelta(days=3)).isoformat(),
        "min_new_stars": search_cfg.get("min_new_stars", 100),
        "min_active_stars": search_cfg.get("min_active_stars", 2000),
    }
    queries: list[tuple[str, str]] = []
    for item in search_cfg.get("queries") or []:
        template = item.get("q")
        if not template:
            continue
        try:
            queries.append((item.get("name", "search"), template.format(**substitutions)))
        except KeyError as exc:
            raise ValueError(f"search query 模板包含未知占位符: {exc}") from exc
    return queries[: int(search_cfg.get("max_queries_per_run", 2))]


def needs_search(
    *,
    candidate_count: int,
    kept_count: int,
    trending_failed: bool,
    search_cfg: dict,
) -> tuple[bool, str]:
    """Search 兜底的三个触发条件（对应方案约定）。"""
    if not search_cfg.get("enabled", True):
        return False, "disabled"
    if trending_failed:
        return True, "trending_failed"
    if candidate_count < int(search_cfg.get("trigger_min_candidates", 40)):
        return True, f"candidates_below_{search_cfg.get('trigger_min_candidates', 40)}"
    if kept_count < int(search_cfg.get("trigger_min_kept", 5)):
        return True, f"kept_below_{search_cfg.get('trigger_min_kept', 5)}"
    return False, "enough"


def dedupe_into(pool: list[Candidate], extra: list[Candidate], weights: dict[str, float]) -> int:
    """把 Search 结果合并进已有候选池，返回新增数量。"""
    from .trending import SourceSpec, TrendingEntry, merge_entries

    seen = {c.full_name.lower() for c in pool}
    new_items = [c for c in extra if c.full_name.lower() not in seen]
    if not new_items:
        return 0
    spec = SourceSpec("search", "", "search")
    entries = [
        TrendingEntry(
            full_name=c.full_name,
            url=c.url,
            rank=c.best_rank or 999,
            description=c.description,
            language=c.language,
            stars=c.stars,
            forks=c.forks,
            stars_today=None,
            stars_this_week=None,
        )
        for c in new_items
    ]
    merged = merge_entries([(spec, e) for e in entries], weights)
    pool.extend(merged)
    return len(merged)


def parse_pushed_within(candidate: Candidate, days: int, today: date) -> bool:
    pushed = parse_iso_date(candidate.pushed_at)
    return bool(pushed and (today - pushed) <= timedelta(days=days))


def as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

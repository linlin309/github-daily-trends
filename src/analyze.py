"""LLM 分析：每天只做 1 次主调用。

硬约束：
- 一次请求提交当天全部入选项目（不是每个项目一次调用）。
- 最多 2 次调用：1 次主调用 + 1 次（JSON 修复 或 切换备用 Provider）。
- 输入只有结构化元数据（不抓 README），统一包在 <untrusted_data> 中。
- 输出必须是严格 JSON：校验 full_name、字段长度、标签枚举；模型不能输出 URL/HTML。
- 任何失败都不中断日报：降级为"纯数据日报"。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .models import Candidate
from .net import HttpClient, HttpError
from .util import detect_injection, parse_iso_date, sanitize_text

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)

PROJECT_FIELDS = ("summary", "what_it_is", "why_worth_attention", "use_case", "target_user")


@dataclass
class AnalysisResult:
    ok: bool = False
    provider: str | None = None
    model: str | None = None
    calls: int = 0
    degraded_reason: str | None = None
    summary: dict[str, Any] = field(default_factory=dict)
    projects: dict[str, dict[str, Any]] = field(default_factory=dict)
    tokens: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    demo: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "provider": self.provider,
            "model": self.model,
            "calls": self.calls,
            "degraded_reason": self.degraded_reason,
            "tokens": self.tokens,
            "demo": self.demo,
            "notes": self.notes,
        }


class LLMAnalyzer:
    def __init__(
        self,
        cfg: dict,
        http: HttpClient,
        *,
        logger: logging.Logger | None = None,
        project_root: Path | None = None,
    ) -> None:
        self.cfg = cfg
        self.http = http
        self.log = logger or logging.getLogger("llm")
        self.root = project_root or Path(__file__).resolve().parents[1]
        self.temperature = float(cfg.get("temperature", 0.3))
        self.max_output_tokens = int(cfg.get("max_output_tokens", 3000))
        self.timeout = float(cfg.get("timeout_seconds", 120))
        self.max_retries = int(cfg.get("max_retries_per_provider", 2))
        self.max_calls_total = int(cfg.get("max_calls_total", 2))
        self.allowed_tags = list(cfg.get("allowed_tags") or [])
        self.max_field_chars = int(cfg.get("max_field_chars", 80))
        self.max_one_liner_chars = int(cfg.get("max_one_liner_chars", 40))
        self.injection_markers = list(cfg.get("injection_markers") or [])
        self.prompt_file = self.root / str(cfg.get("prompt_file", "prompts/analyze.md"))
        self._env = Environment(
            loader=FileSystemLoader(str(self.root)),
            autoescape=select_autoescape(enabled_extensions=("html", "xml"), default=False),
            trim_blocks=False,
            lstrip_blocks=False,
        )
        self.calls = 0
        self.tokens_total: dict[str, int] = {"in": 0, "out": 0}

    # ------------------------------------------------------------------ 对外入口
    def analyze(self, projects: list[Candidate], *, today: date, demo: bool = False) -> AnalysisResult:
        if not projects:
            return AnalysisResult(ok=False, degraded_reason="没有入选项目，跳过 AI 分析")
        if demo:
            return self._demo_result(projects, today=today)

        projects_json = json.dumps(
            [self._payload_for(p, today) for p in projects], ensure_ascii=False, indent=1
        )
        prompt = self._render_prompt(projects_json, len(projects), today)

        result = AnalysisResult()
        last_error = ""
        for provider in self.cfg.get("providers") or []:
            if self.calls >= self.max_calls_total:
                break
            result.provider, result.model = provider.get("name"), provider.get("model")
            payload, error = self._call_provider(provider, prompt, projects_json)
            if payload is None:
                last_error = error
                self.log.warning("Provider %s 调用失败: %s", provider.get("name"), error)
                continue

            summary, by_name, notes = self._validate(payload, projects, today=today)
            result.ok = True
            result.summary = summary
            result.projects = by_name
            result.notes = notes
            result.degraded_reason = None
            result.calls = self.calls
            result.tokens = dict(self.tokens_total)
            self._apply(projects, by_name)
            return result

        result.ok = False
        result.degraded_reason = last_error or "所有 LLM Provider 均不可用"
        result.calls = self.calls
        result.tokens = dict(self.tokens_total)
        self._apply_fallback(projects)
        return result

    # ------------------------------------------------------------------ 调用
    def _call_provider(
        self, provider: dict, prompt: str, projects_json: str
    ) -> tuple[dict[str, Any] | None, str]:
        base_url = str(provider.get("base_url") or "").rstrip("/")
        url = f"{base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {provider.get('api_key')}",
            "Content-Type": "application/json",
        }
        messages = [{"role": "user", "content": prompt}]
        error = ""

        for attempt in range(1, self.max_retries + 1):
            if self.calls >= self.max_calls_total:
                return None, f"已达到调用次数上限({self.max_calls_total})"
            body = {
                "model": provider.get("model"),
                "messages": messages,
                "temperature": self.temperature,
                "max_tokens": self.max_output_tokens,
            }
            self.calls += 1
            try:
                response = self.http.request(
                    "POST",
                    url,
                    headers=headers,
                    json_body=body,
                    allow_status=(200,),
                    max_sleep_seconds=20.0,
                )
            except HttpError as exc:
                error = f"HTTP 调用失败: {exc}"
                continue
            except Exception as exc:  # noqa: BLE001 - 网络层以外的异常也要降级，不能拖垮日报
                error = f"调用异常: {exc}"
                continue

            try:
                payload = response.json()
            except ValueError:
                error = "响应不是 JSON"
                continue

            usage = payload.get("usage") or {}
            self.tokens_total["in"] += int(usage.get("prompt_tokens") or 0)
            self.tokens_total["out"] += int(usage.get("completion_tokens") or 0)

            content = self._content_of(payload)
            if not content:
                error = "响应中没有文本内容"
                continue

            parsed = _extract_json(content)
            if parsed is not None:
                return parsed, ""

            # JSON 修复：只允许一次
            error = "输出不是合法 JSON"
            self.log.warning("输出非 JSON，尝试修复（第 %d 次）", attempt)
            messages = [
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": content[:4000]},
                {"role": "user", "content": "上面的输出不是合法 JSON。请只输出符合要求的 JSON 对象，不要任何解释或代码块围栏。"},
            ]
        return None, error

    @staticmethod
    def _content_of(payload: dict) -> str:
        choices = payload.get("choices") or []
        if not choices:
            return ""
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, list):  # 某些 OpenAI 兼容实现返回分段内容
            return "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
        return str(content or "")

    # ------------------------------------------------------------------ 校验
    def _validate(
        self, payload: dict[str, Any], projects: list[Candidate], *, today: date
    ) -> tuple[dict[str, Any], dict[str, dict[str, Any]], list[str]]:
        names = [p.full_name for p in projects]
        name_set = set(names)
        notes: list[str] = []

        raw_summary = payload.get("summary") or {}
        summary = {
            "headline": sanitize_text(raw_summary.get("headline"), self.max_one_liner_chars),
            "trends": [sanitize_text(t, 60) for t in _as_list(raw_summary.get("trends"))][:3],
            "highlights": [sanitize_text(t, 60) for t in _as_list(raw_summary.get("highlights"))][:3],
        }

        by_name: dict[str, dict[str, Any]] = {}
        dropped: list[str] = []
        for item in payload.get("projects") or []:
            if not isinstance(item, dict):
                continue
            full_name = str(item.get("full_name") or "").strip()
            if full_name not in name_set:
                dropped.append(full_name or "(空)")
                continue
            candidate = next(p for p in projects if p.full_name == full_name)
            grounded = _is_grounded(candidate, today)
            record: dict[str, Any] = {}
            for field_name in PROJECT_FIELDS:
                limit = self.max_one_liner_chars if field_name == "summary" else self.max_field_chars
                record[field_name] = sanitize_text(item.get(field_name), limit)
            tags = [t for t in _as_list(item.get("tags")) if t in self.allowed_tags][:2]
            record["tags"] = tags
            spec = bool(item.get("why_is_speculation", True))
            record["why_is_speculation"] = False if grounded and not spec else True
            by_name[full_name] = record

        if dropped:
            notes.append(f"丢弃了 {len(dropped)} 个不在输入中的项目（防止模型编造）: {', '.join(dropped[:5])}")

        missing = [name for name in names if name not in by_name]
        if missing:
            notes.append(f"{len(missing)} 个项目未获得 AI 分析，已用占位文案: {', '.join(missing[:5])}")
            for name in missing:
                by_name[name] = {field_name: "" for field_name in PROJECT_FIELDS}
                by_name[name]["tags"] = []
                by_name[name]["why_is_speculation"] = True
                by_name[name]["unavailable"] = True

        return summary, by_name, notes

    @staticmethod
    def _apply(projects: list[Candidate], by_name: dict[str, dict[str, Any]]) -> None:
        for candidate in projects:
            record = by_name.get(candidate.full_name) or {}
            candidate.ai = {k: v for k, v in record.items() if k != "tags"}
            candidate.tags = list(record.get("tags") or [])

    @staticmethod
    def _apply_fallback(projects: list[Candidate]) -> None:
        for candidate in projects:
            candidate.ai = {field_name: "" for field_name in PROJECT_FIELDS}
            candidate.ai["unavailable"] = True
            candidate.tags = []

    # ------------------------------------------------------------------ 提示词
    def _render_prompt(self, projects_json: str, count: int, today: date) -> str:
        template = self._env.get_template(str(self.cfg.get("prompt_file", "prompts/analyze.md")))
        return template.render(
            today=today.isoformat(),
            project_count=count,
            projects_json=projects_json,
            allowed_tags=", ".join(self.allowed_tags),
        )

    def _payload_for(self, candidate: Candidate, today: date) -> dict[str, Any]:
        payload = candidate.to_llm_payload(int(self.cfg.get("max_description_chars", 300)))
        if _is_grounded(candidate, today):
            payload["grounded_hint"] = (
                "该项目今天首次出现在 Trending，或最近 7 天内发布过新版本，可以基于这些数据作确定性描述。"
            )
        traces = detect_injection(str(payload.get("description") or ""), self.injection_markers)
        if traces:
            candidate.injection_suspected = True
            payload["injection_suspected"] = True
            self.log.warning("项目 %s 的描述中出现可疑注入标记: %s", candidate.full_name, traces)
        return payload

    # ------------------------------------------------------------------ 演示模式
    def _demo_result(self, projects: list[Candidate], *, today: date) -> AnalysisResult:
        """不调用任何 API，用元数据生成占位文案，供本地 dry-run 预览排版效果。"""
        result = AnalysisResult(ok=True, provider="demo", model="demo", calls=0, demo=True)
        result.summary = {
            "headline": f"本地演示：今日精选 {len(projects)} 个项目（AI 文本为占位内容）",
            "trends": ["此处为演示文本，正式运行时由智谱免费模型生成。"],
            "highlights": ["dry-run 已跑通采集、过滤、打分、选择、渲染与邮件构造全流程。"],
        }
        for candidate in projects:
            candidate.ai = {
                "summary": f"{candidate.name}（演示）",
                "what_it_is": "演示文本：正式运行时，这里由 AI 根据项目 description 与 topics 生成一句话说明。",
                "why_worth_attention": "演示文本：正式运行时这里会给出为什么值得关注，并标注是否为推测。",
                "why_is_speculation": True,
                "use_case": "演示文本：典型使用场景。",
                "target_user": "演示文本：适合人群。",
            }
            candidate.tags = ["今日最实用"] if candidate.rank == 1 else []
            result.projects[candidate.full_name] = dict(candidate.ai, tags=list(candidate.tags))
        return result


# ---------------------------------------------------------------------- 工具
def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if isinstance(item, (str, int, float)) and str(item).strip()]
    return []


def _extract_json(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    cleaned = _FENCE.sub("", text.strip()).strip()
    try:
        payload = json.loads(cleaned)
        return payload if isinstance(payload, dict) else None
    except ValueError:
        pass
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        payload = json.loads(cleaned[start : end + 1])
        return payload if isinstance(payload, dict) else None
    except ValueError:
        return None


def _is_grounded(candidate: Candidate, today: date) -> bool:
    """是否有硬数据支撑"为什么火"：今天首次上榜，或最近 7 天有发布。"""
    if candidate.first_seen == today.isoformat():
        return True
    released = parse_iso_date(candidate.latest_release_at)
    return bool(released and (today - released) <= timedelta(days=7))

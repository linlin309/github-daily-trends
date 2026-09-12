"""通用小工具：日期、数字格式化、外部文本清洗。

这里的 sanitize_text 是安全边界的一部分：所有来自 GitHub 的外部文本
（description / topics）在进入 LLM 之前都必须经过它。
"""

from __future__ import annotations

import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WHITESPACE = re.compile(r"\s+")
_DELIMITER = re.compile(r"</?untrusted_data>", re.IGNORECASE)


def local_today(tz_name: str) -> date:
    """按 IANA 时区计算业务日期。绝不用 utcnow().date()。"""
    return datetime.now(ZoneInfo(tz_name)).date()


def local_now(tz_name: str) -> datetime:
    return datetime.now(ZoneInfo(tz_name))


def parse_iso_date(value: str | None) -> date | None:
    """解析 '2026-09-11T07:04:20Z' / '2026-09-11' 这类时间戳。"""
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None


def days_between(earlier: date, later: date) -> int:
    return (later - earlier).days


def fmt_int(value: int | None) -> str:
    """12345 -> '12,345'；None -> '—'"""
    if value is None:
        return "—"
    return f"{value:,}"


def fmt_signed(value: int | None) -> str:
    if value is None:
        return "—"
    return f"+{value:,}" if value >= 0 else f"{value:,}"


def sanitize_text(value: str | None, max_chars: int) -> str:
    """清洗外部文本：去掉控制字符、压缩空白、截断、中和数据分隔符。

    返回的文本一律视为不可信数据，只能作为 LLM 的分析对象。
    """
    if not value:
        return ""
    text = _CONTROL_CHARS.sub(" ", str(value))
    text = _WHITESPACE.sub(" ", text).strip()
    # 防止外部内容伪造/闭合我们的数据边界标记
    text = _DELIMITER.sub("[filtered]", text)
    if len(text) > max_chars:
        text = text[: max_chars - 1].rstrip() + "…"
    return text


def detect_injection(text: str, markers: list[str]) -> list[str]:
    """检测可疑的提示注入标记，仅用于记录与告警，不改变事实内容。"""
    lowered = text.lower()
    hits: list[str] = []
    for marker in markers:
        if marker.lower() in lowered:
            hits.append(marker)
    return hits


def redact_url(url: str) -> str:
    """日志用：隐藏可能出现在 query 里的密钥类参数。"""
    return re.sub(r"(?i)([?&](?:key|api[_-]?key|token|access_token|password)=)[^&]+", r"\1***", url)


def round2(value: float | None) -> float | None:
    return None if value is None else round(float(value), 2)

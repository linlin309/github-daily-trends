"""幂等判断：是否"今天已经发过信"取决于 JSON 里的发信记录，而不是报告文件是否存在。

背景：报告可能被凌晨误触或手动补跑先生成，若只看 reports/DATE.md 是否存在，
当天就再也发不出邮件（2026-10-26 实际发生过）。
"""

from __future__ import annotations

import json
from pathlib import Path

from src.main import _email_already_sent


def write_json(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_sent_true(tmp_path: Path) -> None:
    p = write_json(tmp_path / "2026-10-26.json", {"date": "2026-10-26", "email": {"ok": True, "attempted_at": "x"}})
    assert _email_already_sent(p) is True


def test_generated_but_not_sent(tmp_path: Path) -> None:
    """报告生成过但没发信 —— 这正是需要补发的场景，必须返回 False。"""
    p = write_json(tmp_path / "2026-10-26.json", {"date": "2026-10-26", "email": {"ok": False, "attempted_at": None}})
    assert _email_already_sent(p) is False


def test_legacy_json_without_email_field(tmp_path: Path) -> None:
    """老报告没有 email 字段：当作没发过，宁可多发一次也不静默漏发。"""
    p = write_json(tmp_path / "2026-09-12.json", {"date": "2026-09-12"})
    assert _email_already_sent(p) is False


def test_missing_or_broken_file(tmp_path: Path) -> None:
    assert _email_already_sent(tmp_path / "nope.json") is False
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert _email_already_sent(bad) is False

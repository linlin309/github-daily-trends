"""邮件构造测试（不联网）：编码、双版本、收件人、主题。"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.config import load_config
from src.mailer import Mailer

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def mail_cfg() -> dict:
    cfg = load_config(ROOT / "config.yaml", env={}, strict_env=False)
    cfg["mail"] = {
        "smtp_host": "smtp.163.com",
        "smtp_port": 465,
        "use_ssl": True,
        "username": "sender@163.com",
        "password": "dummy-auth-code",
        "from_name": "GitHub 每日软件趋势",
        "to": ["sender@163.com", "other@example.com"],
        "timeout_seconds": 30,
        "max_retries": 1,
    }
    return cfg["mail"]


def test_build_message_has_both_versions(mail_cfg):
    mailer = Mailer(mail_cfg)
    message = mailer._build(
        subject="【GitHub 每日软件趋势】2026-09-12",
        plain_text="纯文本版本",
        html="<html><body><h1>HTML 版本</h1></body></html>",
    )

    assert message["To"] == "sender@163.com, other@example.com"
    assert "sender@163.com" in message["From"]
    assert "GitHub 每日软件趋势" in str(message["From"])
    assert message["Date"] and message["Message-ID"]
    assert message.is_multipart()

    types = {part.get_content_type() for part in message.walk()}
    assert "text/plain" in types and "text/html" in types, "必须是 multipart/alternative 双版本"


def test_subject_with_chinese_is_encoded(mail_cfg):
    mailer = Mailer(mail_cfg)
    message = mailer._build(subject="【GitHub 每日软件趋势】2026-09-12", plain_text="x", html=None)
    raw = message.as_bytes()
    # 中文主题必须按 RFC2047 编码，避免部分邮件客户端乱码
    assert b"=?utf-8?" in raw.lower()
    assert not message.is_multipart(), "没有 HTML 时不应构造 multipart"


def test_no_recipients_raises(mail_cfg):
    mail_cfg = dict(mail_cfg, to=[])
    with pytest.raises(ValueError):
        Mailer(mail_cfg)._build(subject="s", plain_text="x", html=None)


def test_alert_message_is_plain_only(mail_cfg):
    mailer = Mailer(mail_cfg)
    message = mailer._build(subject="【GitHub 每日软件趋势】采集失败", plain_text="今天没有生成报告", html=None)
    assert message.get_content_type() == "text/plain"

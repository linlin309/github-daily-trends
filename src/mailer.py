"""邮件发送：Python smtplib + EmailMessage，不依赖任何第三方邮件 Action。

163 邮箱要点（已核实）：
- smtp.163.com；GitHub Actions 出站 25 端口被封，必须用 465 + SSL（SMTP_SSL）。
- 密码是网页端生成的「客户端授权码」，不是登录密码。
- From 地址必须与登录账号一致，否则会被拒（553）。
- 同时提供纯文本与 HTML 两个版本（multipart/alternative），这是降低被判垃圾邮件概率的重要特征。
"""

from __future__ import annotations

import logging
import smtplib
import time
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid


@dataclass
class MailResult:
    ok: bool
    attempts: int = 0
    error: str | None = None


class Mailer:
    def __init__(self, cfg: dict, *, logger: logging.Logger | None = None) -> None:
        self.cfg = cfg
        self.log = logger or logging.getLogger("mail")
        self.host = cfg.get("smtp_host")
        self.port = int(cfg.get("smtp_port") or 465)
        self.use_ssl = bool(cfg.get("use_ssl", True))
        self.username = cfg.get("username")
        self.password = cfg.get("password")
        self.from_name = cfg.get("from_name") or "GitHub 每日软件趋势"
        self.recipients = list(cfg.get("to") or [])
        self.timeout = float(cfg.get("timeout_seconds", 30))
        self.max_retries = int(cfg.get("max_retries", 2))

    # ------------------------------------------------------------------ 对外
    def send_report(self, *, subject: str, html: str, plain_text: str) -> MailResult:
        message = self._build(subject=subject, plain_text=plain_text, html=html)
        return self._send(message)

    def send_alert(self, *, subject: str, text: str) -> MailResult:
        message = self._build(subject=subject, plain_text=text, html=None)
        return self._send(message)

    def describe_target(self) -> str:
        return f"{self.host}:{self.port} (SSL={self.use_ssl}) → {', '.join(self.recipients)}"

    # ------------------------------------------------------------------ 构造
    def _build(self, *, subject: str, plain_text: str, html: str | None) -> EmailMessage:
        if not self.recipients:
            raise ValueError("收件人列表为空")
        message = EmailMessage()
        # EmailMessage 的表头必须是字符串：非 ASCII 由 EmailPolicy 在序列化时自动做 RFC2047 编码
        message["Subject"] = subject
        # 地址必须与登录账号一致（163 会校验）；显示名由 formataddr 负责编码
        message["From"] = formataddr((self.from_name, self.username))
        message["To"] = ", ".join(self.recipients)
        message["Date"] = formatdate(localtime=True)
        domain = (self.username or "").split("@")[-1] or None
        message["Message-ID"] = make_msgid(domain=domain)
        message.set_content(plain_text)
        if html:
            message.add_alternative(html, subtype="html")
        return message

    # ------------------------------------------------------------------ 发送
    def _send(self, message: EmailMessage) -> MailResult:
        last_error: str | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                if self.use_ssl:
                    with smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout) as server:
                        server.login(self.username, self.password)
                        server.send_message(message)
                else:
                    with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as server:
                        server.starttls()
                        server.login(self.username, self.password)
                        server.send_message(message)
                self.log.info("邮件已发送（第 %d 次尝试）：%s", attempt, message["Subject"])
                return MailResult(ok=True, attempts=attempt)
            except smtplib.SMTPAuthenticationError as exc:
                # 授权码错误，重试无意义
                last_error = f"SMTP 认证失败（{exc.smtp_code}）：请检查是否使用 163 客户端授权码，且 MAIL_USERNAME 与授权码匹配"
                self.log.error(last_error)
                return MailResult(ok=False, attempts=attempt, error=last_error)
            except (smtplib.SMTPException, OSError) as exc:
                code = getattr(exc, "smtp_code", None)
                last_error = f"{type(exc).__name__}({code}): {exc}"
                self.log.warning("邮件发送失败（第 %d 次）: %s", attempt, last_error)
                if attempt < self.max_retries:
                    time.sleep(2.0 * attempt)
        return MailResult(ok=False, attempts=self.max_retries, error=last_error)

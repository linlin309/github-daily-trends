"""HTTP 客户端与 URL 安全校验。

安全约束（每次发请求前强制执行）：
- 只允许 http/https；默认强制 https。
- 解析主机名，拒绝 localhost、环回、私有、链路本地、保留、多播地址。
- 重定向后的最终地址同样要校验。
- 日志中的 URL 会做密钥参数脱敏。
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import time
from typing import Any
from urllib.parse import urlparse

import requests

from .util import redact_url

ALLOWED_SCHEMES = {"http", "https"}
_BLOCKED_HOST_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa", ".lan")


class UnsafeUrlError(ValueError):
    """目标地址不满足安全策略。"""


class HttpError(RuntimeError):
    """重试耗尽后仍然失败。"""


def validate_public_url(url: str, *, require_https: bool = True) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise UnsafeUrlError(f"只允许 http/https 协议: {parsed.scheme!r}")
    if require_https and parsed.scheme != "https":
        raise UnsafeUrlError(f"只允许 https: {redact_url(url)}")
    host = (parsed.hostname or "").lower()
    if not host:
        raise UnsafeUrlError("URL 缺少主机名")
    if host == "localhost" or host.endswith(_BLOCKED_HOST_SUFFIXES):
        raise UnsafeUrlError(f"拒绝访问本机/内网主机名: {host}")

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UnsafeUrlError(f"主机名无法解析: {host}") from exc

    for info in infos:
        raw_ip = info[4][0]
        try:
            ip = ipaddress.ip_address(raw_ip)
        except ValueError:
            raise UnsafeUrlError(f"无法识别的地址: {raw_ip}") from None
        if ip.is_multicast or not ip.is_global:
            raise UnsafeUrlError(f"拒绝访问环回/私有/保留地址: {host} -> {ip}")


class HttpClient:
    """带重试、超时、限流感知的极简 HTTP 客户端。"""

    def __init__(
        self,
        user_agent: str,
        *,
        timeout_connect: float = 10,
        timeout_read: float = 30,
        max_retries: int = 3,
        backoff_seconds: float = 2.0,
        logger: logging.Logger | None = None,
    ) -> None:
        self.log = logger or logging.getLogger("http")
        self.timeout = (timeout_connect, timeout_read)
        self.max_retries = max(1, max_retries)
        self.backoff_seconds = backoff_seconds
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"})

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        require_https: bool = True,
        allow_status: tuple[int, ...] = (200,),
        max_sleep_seconds: float = 60.0,
    ) -> requests.Response:
        validate_public_url(url, require_https=require_https)
        last_error: Exception | None = None

        for attempt in range(1, self.max_retries + 1):
            try:
                response = self.session.request(
                    method,
                    url,
                    headers=headers,
                    params=params,
                    json=json_body,
                    timeout=self.timeout,
                    allow_redirects=True,
                )
            except requests.RequestException as exc:
                last_error = exc
                self.log.warning("请求失败(%s/%s) %s: %s", attempt, self.max_retries, redact_url(url), exc)
                self._sleep(self.backoff_seconds * attempt)
                continue

            # 重定向后的最终地址也要校验，避免外部把我们引到内网
            validate_public_url(response.url, require_https=require_https)

            if response.status_code in allow_status:
                return response

            if response.status_code in (403, 429, 500, 502, 503, 504):
                wait = self._retry_after_seconds(response, default=self.backoff_seconds * attempt)
                self.log.warning(
                    "HTTP %s(%s/%s) %s，等待 %.1fs 后重试",
                    response.status_code,
                    attempt,
                    self.max_retries,
                    redact_url(url),
                    wait,
                )
                last_error = HttpError(f"HTTP {response.status_code}")
                self._sleep(min(wait, max_sleep_seconds))
                continue

            raise HttpError(f"HTTP {response.status_code} for {redact_url(url)}")

        raise HttpError(f"重试 {self.max_retries} 次后仍失败: {redact_url(url)}") from last_error

    def get_json(self, url: str, *, headers=None, params=None, require_https=True) -> Any:
        payload, _ = self.get_json_with_headers(url, headers=headers, params=params, require_https=require_https)
        return payload

    def get_json_with_headers(
        self, url: str, *, headers=None, params=None, require_https=True
    ) -> tuple[Any, dict[str, str]]:
        """返回 (JSON, 响应头)。GitHub 的限额信息就在响应头里。"""
        response = self.request("GET", url, headers=headers, params=params, require_https=require_https)
        try:
            payload = response.json()
        except ValueError as exc:
            raise HttpError(f"响应不是合法 JSON: {redact_url(url)}") from exc
        return payload, dict(response.headers)

    def get_text(self, url: str, *, headers=None, params=None, require_https=True) -> str:
        response = self.request("GET", url, headers=headers, params=params, require_https=require_https)
        response.encoding = response.encoding or "utf-8"
        return response.text

    @staticmethod
    def _retry_after_seconds(response: requests.Response, default: float) -> float:
        raw = response.headers.get("Retry-After")
        if raw:
            try:
                return float(raw)
            except ValueError:
                pass
        reset = response.headers.get("X-RateLimit-Reset")
        if reset and response.status_code in (403, 429):
            try:
                delta = float(reset) - time.time()
                if delta > 0:
                    return min(delta, 120.0)
            except ValueError:
                pass
        return default

    @staticmethod
    def _sleep(seconds: float) -> None:
        if seconds > 0:
            time.sleep(min(seconds, 120.0))

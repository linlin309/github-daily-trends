"""配置加载：读取 config.yaml 并把 ${VAR} 占位符替换为环境变量。

规则：
- 只从环境变量读取凭据；源码/配置/测试中不允许出现可用的凭据字面量。
- 必需变量缺失 → 直接抛 ConfigError（fail fast，而不是带着空密码继续跑）。
- 可选的备用 LLM Provider 缺失 → 丢弃该 provider 并告警，不影响主流程。
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Mapping

import yaml

_PLACEHOLDER = re.compile(r"^\$\{([A-Z0-9_]+)\}$")

REQUIRED_ENV: tuple[str, ...] = (
    "LLM_BASE_URL",
    "LLM_MODEL",
    "LLM_API_KEY",
    "MAIL_SMTP_HOST",
    "MAIL_SMTP_PORT",
    "MAIL_USERNAME",
    "MAIL_PASSWORD",
    "MAIL_TO",
)

OPTIONAL_ENV: tuple[str, ...] = (
    "LLM_FALLBACK_BASE_URL",
    "LLM_FALLBACK_MODEL",
    "LLM_FALLBACK_API_KEY",
)


class ConfigError(RuntimeError):
    """配置缺失或不合法。"""


def _resolve(node: Any, env: Mapping[str, str], missing: set[str]) -> Any:
    if isinstance(node, dict):
        return {key: _resolve(value, env, missing) for key, value in node.items()}
    if isinstance(node, list):
        return [_resolve(item, env, missing) for item in node]
    if isinstance(node, str):
        match = _PLACEHOLDER.match(node.strip())
        if match:
            name = match.group(1)
            value = (env.get(name) or "").strip()
            if not value:
                missing.add(name)
                return None
            return value
        return node
    return node


def _require_int(config: dict, path: tuple[str, ...]) -> None:
    """把配置项强制转成 int（仅限最末一级，绝不覆盖中间层）。"""
    node: Any = config
    for key in path[:-1]:
        node = node[key]
    leaf = path[-1]
    value = node[leaf]
    if value is None:
        # 缺值由 REQUIRED_ENV 检查负责（测试模式下允许缺省）
        return
    try:
        node[leaf] = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"配置项 {'.'.join(path)} 需要是整数，实际为 {value!r}") from exc


def load_config(
    path: str | Path,
    *,
    env: Mapping[str, str] | None = None,
    logger: logging.Logger | None = None,
    strict_env: bool = True,
) -> dict:
    """加载配置。

    strict_env=True（默认，线上使用）：必需环境变量缺失立即报错。
    strict_env=False（仅测试使用）：允许缺凭据，用于验证规则/打分/选择等纯逻辑。
    """
    log = logger or logging.getLogger("config")
    env_map: Mapping[str, str] = env if env is not None else os.environ
    file_path = Path(path)
    if not file_path.is_file():
        raise ConfigError(f"找不到配置文件: {file_path}")

    with file_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    if not isinstance(raw, dict):
        raise ConfigError("配置文件顶层必须是映射结构")

    missing: set[str] = set()
    config = _resolve(raw, env_map, missing)

    missing_required = sorted(set(REQUIRED_ENV) & missing)
    if missing_required and strict_env:
        raise ConfigError(
            "缺少必需的环境变量: " + ", ".join(missing_required)
            + "（请配置到 GitHub Secrets / Variables，或本地导出后再运行）"
        )
    if missing_required:
        log.warning("（测试模式）缺少环境变量: %s", ", ".join(missing_required))

    # 备用 provider 缺失时静默降级为"只有一个 provider"
    providers = [p for p in (config.get("llm", {}).get("providers") or []) if p.get("base_url") and p.get("model") and p.get("api_key")]
    if not providers and strict_env:
        raise ConfigError("llm.providers 为空：至少需要一个可用的 LLM provider（检查 LLM_BASE_URL / LLM_MODEL / LLM_API_KEY）")
    config.setdefault("llm", {})["providers"] = providers
    config.setdefault("_meta", {})["missing_env"] = sorted(missing)

    mail_to = config.get("mail", {}).get("to") or []
    # 支持用逗号分隔多个收件人（MAIL_TO="a@x.com,b@y.com"）
    config["mail"]["to"] = [
        address.strip()
        for entry in mail_to
        if entry
        for address in str(entry).split(",")
        if address.strip()
    ]
    if not config["mail"]["to"] and strict_env:
        raise ConfigError("mail.to 为空：请配置 MAIL_TO")

    _require_int(config, ("mail", "smtp_port"))
    _require_int(config, ("report", "target_count"))
    _require_int(config, ("sources", "search", "per_page"))
    _require_int(config, ("llm", "max_output_tokens"))
    _require_int(config, ("llm", "max_calls_total"))

    config.setdefault("_meta", {})["config_path"] = str(file_path)
    return config

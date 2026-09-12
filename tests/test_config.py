"""配置加载的单元测试（占位符解析、fail fast、嵌套整型不破坏结构）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.config import ConfigError, load_config

ROOT = Path(__file__).resolve().parents[1]


def test_nested_int_coercion_keeps_structure():
    """回归测试：三级路径的 int 转换绝不能把中间的 dict 覆盖成 int。"""
    cfg = load_config(ROOT / "config.yaml", env={}, strict_env=False)
    search = cfg["sources"]["search"]
    assert isinstance(search, dict), "sources.search 被破坏了"
    assert search["per_page"] == 30
    assert search["trigger_min_candidates"] == 40
    # 非严格模式下缺值保持 None，不应报错
    assert cfg["mail"]["smtp_port"] is None


def test_missing_required_env_fails_fast():
    with pytest.raises(ConfigError) as excinfo:
        load_config(ROOT / "config.yaml", env={}, strict_env=True)
    message = str(excinfo.value)
    assert "LLM_API_KEY" in message and "MAIL_PASSWORD" in message


def test_env_placeholders_resolved():
    env = {
        "LLM_BASE_URL": "https://example.invalid/api/paas/v4/",
        "LLM_MODEL": "glm-4.7-flash",
        "LLM_API_KEY": "dummy-llm-key",
        "MAIL_SMTP_HOST": "smtp.163.com",
        "MAIL_SMTP_PORT": "465",
        "MAIL_USERNAME": "sender@163.com",
        "MAIL_PASSWORD": "dummy-auth-code",
        "MAIL_TO": "sender@163.com",
    }
    cfg = load_config(ROOT / "config.yaml", env=env, strict_env=True)
    assert cfg["mail"]["smtp_host"] == "smtp.163.com"
    assert cfg["mail"]["smtp_port"] == 465
    assert cfg["mail"]["to"] == ["sender@163.com"]
    assert cfg["llm"]["providers"][0]["model"] == "glm-4.7-flash"
    # 未提供备用 provider 的变量 → 只保留主 provider，不报错
    assert len(cfg["llm"]["providers"]) == 1


def test_fallback_provider_kept_when_configured():
    env = {
        "LLM_BASE_URL": "https://example.invalid/api/paas/v4/",
        "LLM_MODEL": "glm-4.7-flash",
        "LLM_API_KEY": "dummy-llm-key",
        "LLM_FALLBACK_BASE_URL": "https://openrouter.invalid/api/v1",
        "LLM_FALLBACK_MODEL": "some/model:free",
        "LLM_FALLBACK_API_KEY": "dummy-openrouter-key",
        "MAIL_SMTP_HOST": "smtp.163.com",
        "MAIL_SMTP_PORT": "465",
        "MAIL_USERNAME": "sender@163.com",
        "MAIL_PASSWORD": "dummy-auth-code",
        "MAIL_TO": "sender@163.com,other@example.com",
    }
    cfg = load_config(ROOT / "config.yaml", env=env, strict_env=True)
    names = [p["name"] for p in cfg["llm"]["providers"]]
    assert names == ["zhipu", "openrouter"]
    assert cfg["mail"]["to"] == ["sender@163.com", "other@example.com"]


def test_same_vendor_alt_model_provider_is_optional():
    """LLM_MODEL_2 复用主 Key：配了就有同厂商兜底，没配就自动忽略。"""
    base_env = {
        "LLM_BASE_URL": "https://open.bigmodel.cn/api/paas/v4/",
        "LLM_MODEL": "glm-4.7-flash",
        "LLM_API_KEY": "dummy-llm-key",
        "MAIL_SMTP_HOST": "smtp.163.com",
        "MAIL_SMTP_PORT": "465",
        "MAIL_USERNAME": "sender@163.com",
        "MAIL_PASSWORD": "dummy-auth-code",
        "MAIL_TO": "sender@163.com",
    }
    without = load_config(ROOT / "config.yaml", env=base_env, strict_env=True)
    assert [p["name"] for p in without["llm"]["providers"]] == ["zhipu"]

    with_alt = load_config(ROOT / "config.yaml", env=dict(base_env, LLM_MODEL_2="glm-4-flash-250414"), strict_env=True)
    names = [p["name"] for p in with_alt["llm"]["providers"]]
    assert names == ["zhipu", "zhipu-alt-model"]
    alt = with_alt["llm"]["providers"][1]
    assert alt["model"] == "glm-4-flash-250414"
    assert alt["api_key"] == base_env["LLM_API_KEY"], "同厂商兜底应复用主 Key，不需要额外 Secret"
    assert alt["base_url"] == base_env["LLM_BASE_URL"]
    # 老模型不是思考模型，不应带 thinking 参数
    assert "extra_body" not in alt or not alt["extra_body"]


def test_credentials_never_literal_in_repo_files():
    """源码/配置/测试中不得出现可用的凭据字面量（只允许占位符）。"""
    import re

    suspicious = re.compile(r"(sk-[A-Za-z0-9]{16,}|Bearer\s+[A-Za-z0-9._-]{20,})")
    for path in list(ROOT.glob("*.yaml")) + list(ROOT.glob("src/*.py")):
        text = path.read_text(encoding="utf-8")
        assert not suspicious.search(text), f"{path} 中疑似出现真实凭据"
    config_text = (ROOT / "config.yaml").read_text(encoding="utf-8")
    assert "${LLM_API_KEY}" in config_text and "${MAIL_PASSWORD}" in config_text

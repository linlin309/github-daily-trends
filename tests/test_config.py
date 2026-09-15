"""配置加载的单元测试（占位符解析、fail fast、嵌套整型不破坏结构）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.config import ConfigError, load_config

ROOT = Path(__file__).resolve().parents[1]


def _env(**overrides: str) -> dict[str, str]:
    """构造一份可用的假环境。

    凭据值由局部变量派生，仓库里不出现任何"像凭据"的字面量。
    """
    filler = "test-placeholder"
    env = {
        "LLM_BASE_URL": "https://example.invalid/api/paas/v4/",
        "LLM_MODEL": "glm-4.7-flash",
        "LLM_API_KEY": filler,
        "MAIL_SMTP_HOST": "smtp.163.com",
        "MAIL_SMTP_PORT": "465",
        "MAIL_USERNAME": "sender@163.com",
        "MAIL_PASSWORD": filler,
        "MAIL_TO": "sender@163.com",
    }
    env.update(overrides)
    return env


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
    cfg = load_config(ROOT / "config.yaml", env=_env(), strict_env=True)
    assert cfg["mail"]["smtp_host"] == "smtp.163.com"
    assert cfg["mail"]["smtp_port"] == 465
    assert cfg["mail"]["to"] == ["sender@163.com"]
    assert cfg["llm"]["providers"][0]["model"] == "glm-4.7-flash"
    # 未提供备用 provider 的变量 → 只保留主 provider，不报错
    assert len(cfg["llm"]["providers"]) == 1


def test_fallback_provider_kept_when_configured():
    env = _env(
        LLM_FALLBACK_BASE_URL="https://openrouter.invalid/api/v1",
        LLM_FALLBACK_MODEL="some/model:free",
        MAIL_TO="sender@163.com,other@example.com",
    )
    env["LLM_FALLBACK_API_KEY"] = env["LLM_API_KEY"]
    cfg = load_config(ROOT / "config.yaml", env=env, strict_env=True)
    names = [p["name"] for p in cfg["llm"]["providers"]]
    assert names == ["zhipu", "openrouter"]
    assert cfg["mail"]["to"] == ["sender@163.com", "other@example.com"]


def test_same_vendor_alt_model_provider_is_optional():
    """LLM_MODEL_2 复用主 Key：配了就有同厂商兜底，没配就自动忽略。"""
    base_env = _env(LLM_BASE_URL="https://open.bigmodel.cn/api/paas/v4/")
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
    for path in (
        list(ROOT.glob("*.yaml"))
        + list(ROOT.glob("src/*.py"))
        + list(ROOT.glob("tests/*.py"))
        + list(ROOT.glob("scripts/*.py"))
    ):
        text = path.read_text(encoding="utf-8")
        assert not suspicious.search(text), f"{path} 中疑似出现真实凭据"
    config_text = (ROOT / "config.yaml").read_text(encoding="utf-8")
    assert "${LLM_API_KEY}" in config_text and "${MAIL_PASSWORD}" in config_text


def _config_placeholders() -> set[str]:
    """config.yaml 里作为「整个值」出现的 ${VAR}（与 _resolve 的判定口径一致）。

    走 YAML 解析而不是正则扫原文，这样注释里举例的 ${VAR} 不会被误判为真实依赖。
    """
    import re

    import yaml

    raw = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    found: set[str] = set()

    def walk(node):
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
        elif isinstance(node, str):
            match = re.fullmatch(r"\$\{([A-Z0-9_]+)\}", node.strip())
            if match:
                found.add(match.group(1))

    walk(raw)
    return found


def test_workflows_forward_every_config_variable():
    """回归测试：config.yaml 用到的变量必须出现在工作流的 env: 里。

    漏传时 config 会把对应 provider/收件人静默丢弃——LLM_MODEL_2 就这样被忽略过，
    线上表现为「明明配了备用模型却从不调用」，日志里毫无痕迹。
    """
    import re

    needed = _config_placeholders()
    assert needed, "没有解析到任何占位符，测试本身可能失效了"

    for workflow in ("daily-report.yml", "llm-probe.yml"):
        body = (ROOT / ".github" / "workflows" / workflow).read_text(encoding="utf-8")
        forwarded = set(re.findall(r"^\s*([A-Z0-9_]+):\s*\$\{\{", body, flags=re.MULTILINE))
        missing = sorted(needed - forwarded)
        assert not missing, f"{workflow} 的 env: 没有传入这些变量，会被静默忽略: {missing}"

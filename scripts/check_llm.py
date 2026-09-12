"""LLM 连通性与限制探针：不跑采集、不碰 GitHub API、不发邮件。

用途：真实 dry-run 出现"AI 分析失败"时，先用它把问题缩小到 LLM 这一层。

用法：
    # 1) 最小请求（最省，验证 key / base_url / 模型名 / 账号额度）
    python scripts/check_llm.py

    # 2) 用真实提示词 + 真实项目数据，测我们真实 workload 的耗时与 token
    python scripts/check_llm.py --full                 # 取 preview/*.json 里的前 3 个项目
    python scripts/check_llm.py --full --count 5

需要的环境变量（与线上一致）：LLM_BASE_URL / LLM_MODEL / LLM_API_KEY
（可选备用：LLM_FALLBACK_BASE_URL / LLM_FALLBACK_MODEL / LLM_FALLBACK_API_KEY）
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.analyze import LLMAnalyzer  # noqa: E402
from src.config import ConfigError, load_config  # noqa: E402
from src.models import Candidate  # noqa: E402
from src.net import HttpClient, HttpError, UnsafeUrlError  # noqa: E402


def mask(secret: str | None) -> str:
    if not secret:
        return "(未设置)"
    if len(secret) <= 8:
        return "***"
    return f"{secret[:4]}***{secret[-4:]}"


def build_projects(count: int, date_hint: str) -> list[Candidate]:
    """优先用 preview/*.json 里的真实项目；没有就造几条等价结构的假数据。"""
    preview_dir = ROOT / "preview"
    for path in sorted(preview_dir.glob("*.json"), reverse=True):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        items = payload.get("selected") or []
        if items:
            projects = [Candidate.from_dict(item) for item in items[:count]]
            print(f"（使用 {path.relative_to(ROOT)} 里的 {len(projects)} 个真实项目）")
            return projects

    print("（没有 preview/*.json，使用内置样例项目）")
    samples = [
        ("acme/fastcli", "A blazing-fast CLI for deploying containers", "Go", ["cli", "devops", "containers"], 12000, 480, 1),
        ("acme/agent-kit", "A toolkit for building coding agents", "Python", ["ai-agent", "llm", "tools"], 8400, 260, 2),
        ("acme/vector-db", "Embedded vector database for local RAG", "Rust", ["database", "rag", "embeddings"], 3100, 95, 4),
    ]
    return [
        Candidate(
            full_name=name,
            url=f"https://github.com/{name}",
            owner=name.split("/")[0],
            name=name.split("/")[1],
            description=desc,
            language=lang,
            topics=topics,
            stars=stars,
            stars_today=today,
            forks=stars // 10,
            best_rank=rank,
            sources=["daily"],
            source_weight=1.0,
            category="cli",
            group="dev-tools",
            first_seen=date_hint,
            trending_streak_days=1,
            rank=rank,
        )
        for name, desc, lang, topics, stars, today, rank in samples
    ][:count]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LLM 探针")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--count", type=int, default=3, help="--full 模式下提交的项目数")
    parser.add_argument("--full", action="store_true", help="用真实提示词跑完整分析（消耗 token 更多）")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        cfg = load_config(ROOT / args.config, logger=logging.getLogger("probe"))
    except ConfigError as exc:
        print(f"\n[配置错误] {exc}")
        print("先把 LLM_BASE_URL / LLM_MODEL / LLM_API_KEY（以及邮件变量）准备好再跑。")
        return 2

    llm_cfg = cfg["llm"]
    print("=" * 62)
    print("LLM 配置")
    print("=" * 62)
    for provider in llm_cfg["providers"]:
        print(f"  provider      : {provider.get('name')}")
        print(f"  base_url      : {provider.get('base_url')}")
        print(f"  model         : {provider.get('model')}")
        print(f"  api_key       : {mask(str(provider.get('api_key')))}")
        print(f"  extra_body    : {json.dumps(provider.get('extra_body') or {}, ensure_ascii=False)}")
    print(f"  read_timeout  : {llm_cfg.get('timeout_seconds')}s（连接 {llm_cfg.get('connect_timeout_seconds')}s）")
    print(f"  max_tokens    : {llm_cfg.get('max_output_tokens')}")
    print(f"  max_calls     : {llm_cfg.get('max_calls_total')}")
    print()

    http = HttpClient(
        cfg["http"]["user_agent"],
        timeout_connect=float(llm_cfg.get("connect_timeout_seconds", 15)),
        timeout_read=float(llm_cfg.get("timeout_seconds", 240)),
        max_retries=1,          # 探针只打一发，方便看清原始错误
        logger=logging.getLogger("probe"),
    )
    analyzer = LLMAnalyzer(llm_cfg, http, project_root=ROOT)

    if not args.full:
        # 最小请求：直接打服务端，验证鉴权 / 模型名 / 额度 / 网络
        provider = llm_cfg["providers"][0]
        url = str(provider["base_url"]).rstrip("/") + "/chat/completions"
        body = {
            "model": provider["model"],
            "messages": [{"role": "user", "content": "只回复两个字符：OK"}],
            "max_tokens": 16,
            "temperature": 0.1,
        }
        body.update(provider.get("extra_body") or {})
        print("发送最小请求（max_tokens=16）…")
        started = time.monotonic()
        try:
            response = http.request(
                "POST",
                url,
                headers={"Authorization": f"Bearer {provider['api_key']}", "Content-Type": "application/json"},
                json_body=body,
                allow_status=(200, 400, 401, 403, 404, 422, 429, 500, 502, 503, 504),
                timeout=(float(llm_cfg.get("connect_timeout_seconds", 15)), float(llm_cfg.get("timeout_seconds", 240))),
            )
        except UnsafeUrlError as exc:
            print(f"\n[失败] LLM_BASE_URL 不合法：{exc}")
            print("要求：必须是 https，且主机名可解析（本项目的安全策略会拒绝内网/环回地址）。")
            print("智谱国内站正确写法：https://open.bigmodel.cn/api/paas/v4/")
            return 1
        except HttpError as exc:
            print(f"\n[失败] 网络/HTTP 层：{exc}")
            print("排查：base_url 是否正确、是否能出网（Actions 里正常运行；本地可能被网络环境限制）")
            return 1
        elapsed = time.monotonic() - started
        print(f"HTTP {response.status_code}，耗时 {elapsed:.1f}s")
        if response.status_code == 200:
            payload = response.json()
            print(f"返回内容：{analyzer._content_of(payload)[:80]!r}")
            print(f"用量：{payload.get('usage')}")
            print("\n[成功] 鉴权、base_url、模型名、账号额度都正常，可以继续跑完整 dry-run。")
            return 0
        print(f"错误详情：{analyzer._describe_error(response)}")
        print("\n判读建议：")
        print("  401/403 → LLM_API_KEY 错误或未开通")
        print("  404    → LLM_MODEL 名称错误")
        print("  429    → 看错误码：1302/1303 并发或频率超限；1305 过载（稍后重试）；1308/1310 额度用尽；1113 余额不足（该模型可能并非免费）")
        print("  400/422→ 请求参数问题（例如该模型不支持 thinking 字段）")
        return 1

    date_hint = time.strftime("%Y-%m-%d")
    projects = build_projects(args.count, date_hint)
    print(f"用真实提示词分析 {len(projects)} 个项目 …")
    started = time.monotonic()
    result = analyzer.analyze(projects, today=__import__("datetime").date.fromisoformat(date_hint))
    elapsed = time.monotonic() - started

    print()
    print("=" * 62)
    print(f"结果：ok={result.ok} provider={result.provider} model={result.model} calls={result.calls}")
    print(f"耗时：{elapsed:.1f}s   tokens in/out={result.tokens.get('in')}/{result.tokens.get('out')}")
    if result.degraded_reason:
        print(f"失败原因：{result.degraded_reason}")
    if result.notes:
        for note in result.notes:
            print(f"提示：{note}")
    if result.ok:
        print(f"概览：{result.summary.get('headline')}")
        for name, record in list(result.projects.items())[:2]:
            print(f"  - {name}: {record.get('what_it_is')}")
        print("\n[成功] 提示词、字段长度、JSON 校验都通过了。")
        return 0
    print("\n[失败] 建议：先跑 python scripts/check_llm.py（不带 --full）缩小问题范围。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

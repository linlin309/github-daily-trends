"""下载真实的 Trending 页面作为测试 fixture。

用途：
- tests/ 里的解析单元测试需要一份真实 HTML 样本；
- `python -m src.main --offline` 可以在不访问网络的情况下跑通全流程。

用法：
    python scripts/fetch_fixture.py            # 抓 daily + weekly
    python scripts/fetch_fixture.py python     # 额外抓语言页
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
UA = {"User-Agent": "github-daily-trends/1.0 (+https://github.com/linlin309/github-daily-trends)"}

TARGETS = {
    "daily": "https://github.com/trending?since=daily",
    "weekly": "https://github.com/trending?since=weekly",
}


def fetch(label: str, url: str, today: str) -> Path:
    response = requests.get(url, headers=UA, timeout=30)
    response.raise_for_status()
    safe_label = label.replace(":", "_")
    path = FIXTURES / f"trending_{safe_label}_{today}.html"
    path.write_text(response.text, encoding="utf-8")
    print(f"{label:14s} {len(response.text):>8,} bytes -> {path.name}")
    return path


def main(argv: list[str]) -> int:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    targets = dict(TARGETS)
    for language in argv:
        targets[f"lang:{language}"] = f"https://github.com/trending/{language}?since=daily"
    for label, url in targets.items():
        fetch(label, url, today)
    print("\n提示：fixture 只用于测试与离线预览，正式运行仍然实时抓取。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

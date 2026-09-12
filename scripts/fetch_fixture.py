"""下载真实的 Trending 页面作为测试 fixture。

用途：
- tests/ 里的解析单元测试需要一份真实 HTML 样本；
- `python -m src.main --offline` 可以在不访问网络的情况下跑通全流程。

用法：
    python scripts/fetch_fixture.py            # 抓 daily + weekly
    python scripts/fetch_fixture.py python     # 额外抓语言页
"""

from __future__ import annotations

import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.net import HttpClient  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
UA = "github-daily-trends/1.0 (+https://github.com/linlin309/github-daily-trends)"

TARGETS = {
    "daily": "https://github.com/trending?since=daily",
    "weekly": "https://github.com/trending?since=weekly",
}

# 语言来自命令行参数，限定字符集后才允许拼进 URL
SAFE_LANGUAGE = re.compile(r"^[a-z0-9][a-z0-9+#.-]{0,30}$")


def fetch(client: HttpClient, label: str, url: str, today: str) -> Path:
    # 统一走 HttpClient：内部会校验目标地址（仅 https + 公网主机）并带重试
    html = client.get_text(url)
    safe_label = label.replace(":", "_")
    path = FIXTURES / f"trending_{safe_label}_{today}.html"
    path.write_text(html, encoding="utf-8")
    print(f"{label:14s} {len(html):>8,} bytes -> {path.name}")
    return path


def main(argv: list[str]) -> int:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    client = HttpClient(UA, timeout_read=30, max_retries=3)
    targets = dict(TARGETS)
    for language in argv:
        if not SAFE_LANGUAGE.match(language):
            print(f"跳过非法语言参数：{language!r}")
            continue
        targets[f"lang:{language}"] = f"https://github.com/trending/{language}?since=daily"
    for label, url in targets.items():
        fetch(client, label, url, today)
    print("\n提示：fixture 只用于测试与离线预览，正式运行仍然实时抓取。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

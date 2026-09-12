"""线上解析自检（parser canary）。

抓取真实 Trending 页面并执行与线上完全相同的解析健康检查：
- 行数区间 5~60
- 字段覆盖率 ≥ 80%
- 至少能取到 full_name / stars

页面结构一旦变化，这个脚本会以非 0 退出，让 Actions 变红（每周运行一次）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import load_config  # noqa: E402
from src.net import HttpClient, HttpError  # noqa: E402
from src.trending import TrendingParseError, parse_trending_html  # noqa: E402

TARGETS = {
    "daily": "https://github.com/trending?since=daily",
    "weekly": "https://github.com/trending?since=weekly",
    "lang:python": "https://github.com/trending/python?since=daily",
}


def main() -> int:
    cfg = load_config(ROOT / "config.yaml", env={}, strict_env=False)
    http = HttpClient(cfg["http"]["user_agent"], max_retries=2)
    failures: list[str] = []

    for label, url in TARGETS.items():
        since = "weekly" if "weekly" in url else "daily"
        try:
            html = http.get_text(url)
            entries = parse_trending_html(html, since=since)
        except (HttpError, TrendingParseError) as exc:
            failures.append(f"{label}: {exc}")
            print(f"[FAIL] {label}: {exc}")
            continue

        if not entries:
            print(f"[WARN] {label}: 页面当前没有任何项目（GitHub 偶发）")
            continue

        today_ratio = sum(1 for e in entries if e.stars_today) / len(entries)
        print(
            f"[OK]   {label}: {len(entries)} 行，"
            f"stars today 覆盖 {today_ratio:.0%}，示例 {entries[0].full_name}"
        )

    if failures:
        print("\n解析自检未通过，Trending 页面结构可能已经变化：")
        for item in failures:
            print(f"  - {item}")
        print("\n处理建议：用 scripts/fetch_fixture.py 保存最新页面，然后更新 src/trending.py 的选择器与 tests/fixtures。")
        return 1
    print("\n解析自检全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

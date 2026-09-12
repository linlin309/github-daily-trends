"""历史数据读取：连续上榜、首次上榜、日增推算、近期是否已报告。

历史文件就是 reports/YYYY-MM-DD.json，没有数据库。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

_DATE_PREFIX = 10  # YYYY-MM-DD


@dataclass
class HistoryView:
    seen_dates: dict[str, set[date]] = field(default_factory=dict)
    prev_stars: dict[str, tuple[date, int]] = field(default_factory=dict)
    last_selected: dict[str, date] = field(default_factory=dict)

    def first_seen(self, full_name: str) -> date | None:
        dates = self.seen_dates.get(full_name)
        return min(dates) if dates else None

    def days_seen(self, full_name: str) -> int:
        return len(self.seen_dates.get(full_name) or ())

    def streak_days(self, full_name: str, today: date) -> int:
        """截止今天（含今天）的连续上榜天数。今天尚未记录时从昨天往前数。"""
        dates = self.seen_dates.get(full_name)
        if not dates:
            return 0
        streak = 0
        cursor = today if today in dates else today - timedelta(days=1)
        while cursor in dates:
            streak += 1
            cursor -= timedelta(days=1)
        return streak

    def previous_stars(self, full_name: str, today: date) -> int | None:
        entry = self.prev_stars.get(full_name)
        if not entry:
            return None
        seen_on, stars = entry
        if seen_on >= today:
            return None
        return stars

    def was_selected_recently(self, full_name: str, today: date, lookback_days: int) -> bool:
        last = self.last_selected.get(full_name)
        if last is None:
            return False
        return timedelta(0) <= (today - last) <= timedelta(days=max(0, lookback_days))


class HistoryStore:
    def __init__(self, reports_dir: Path, logger: logging.Logger | None = None) -> None:
        self.reports_dir = Path(reports_dir)
        self.log = logger or logging.getLogger("history")

    def load(self, today: date, *, lookback_days: int = 60) -> HistoryView:
        view = HistoryView()
        if not self.reports_dir.is_dir():
            return view

        earliest = today - timedelta(days=lookback_days)
        files: list[tuple[date, Path]] = []
        for path in sorted(self.reports_dir.glob("*.json")):
            file_date = _date_from_name(path.name)
            if file_date is None or not (earliest <= file_date < today):
                continue
            files.append((file_date, path))

        for file_date, path in sorted(files):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                self.log.warning("读取历史文件失败 %s: %s", path.name, exc)
                continue

            for item in payload.get("candidates") or []:
                self._record(view, item, file_date)
            for item in payload.get("selected") or []:
                self._record(view, item, file_date)
                name = item.get("full_name")
                if name:
                    previous = view.last_selected.get(name)
                    if previous is None or file_date > previous:
                        view.last_selected[name] = file_date

        self.log.info("历史记录：%d 天，覆盖 %d 个项目", len(files), len(view.seen_dates))
        return view

    @staticmethod
    def _record(view: HistoryView, item: dict, file_date: date) -> None:
        name = item.get("full_name")
        if not name:
            return
        view.seen_dates.setdefault(name, set()).add(file_date)
        stars = item.get("stars")
        if isinstance(stars, int):
            previous = view.prev_stars.get(name)
            if previous is None or file_date >= previous[0]:
                view.prev_stars[name] = (file_date, stars)


def _date_from_name(name: str) -> date | None:
    if len(name) < _DATE_PREFIX or not name.endswith(".json"):
        return None
    try:
        return date.fromisoformat(name[:_DATE_PREFIX])
    except ValueError:
        return None

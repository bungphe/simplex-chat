"""Scheduled routines: work an employee does on its own at set times.

A routine runs on its listed days, at or after its time, inside a window, and
once per period. Late or repeated scheduler ticks are harmless: outside the
window nothing runs, and a period that already ran is not run again. The
period is marked before the work starts, so a crash mid-run is not retried in
a loop.

Days: "daily", "weekdays", "mon-fri", "mon,wed,fri", "sat-sun"...
Period: "day" (default), "week" (ISO week) or "month". "mon-fri" with period
"month" means the first weekday of each month on which the window is reached.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any

from .i18n import tr

DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
PERIODS = ("day", "week", "month")
DELIVERY = ("admins", "none")
_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")


@dataclass(frozen=True)
class Routine:
    id: str
    task: str
    at: time
    days: frozenset[int]  # 0 = Monday
    window_minutes: int = 180
    period: str = "day"
    deliver: str = "admins"
    days_spec: str = "daily"

    def period_key(self, now: datetime) -> str:
        if self.period == "week":
            y, w, _ = now.isocalendar()
            return f"{y}-W{w:02d}"
        if self.period == "month":
            return f"{now:%Y-%m}"
        return f"{now:%Y-%m-%d}"

    def window(self, day: datetime) -> tuple[datetime, datetime]:
        start = day.replace(hour=self.at.hour, minute=self.at.minute, second=0, microsecond=0)
        return start, start + timedelta(minutes=self.window_minutes)

    def in_window(self, now: datetime) -> bool:
        if now.weekday() not in self.days:
            return False
        start, end = self.window(now)
        return start <= now < end

    def next_start(self, now: datetime, last_period: str | None = None) -> datetime | None:
        """The next window start at or after `now` whose period has not run yet."""
        for d in range(400):
            day = now + timedelta(days=d)
            if day.weekday() not in self.days:
                continue
            start, end = self.window(day)
            if end <= now or self.period_key(start) == last_period:
                continue
            return max(start, now) if start <= now else start
        return None

    def describe(self) -> str:
        per = {"day": "", "week": tr(", mỗi tuần một lần"), "month": tr(", mỗi tháng một lần")}[self.period]
        return f"{self.days_spec} {self.at:%H:%M}{per}"


def parse_days(spec: str) -> frozenset[int]:
    s = spec.strip().lower()
    if s in ("daily", "everyday", "every day", "*"):
        return frozenset(range(7))
    if s in ("weekdays", "workdays"):
        return frozenset(range(5))
    if s == "weekends":
        return frozenset({5, 6})
    days: set[int] = set()
    for part in s.split(","):
        part = part.strip()
        if "-" in part:
            a, b = (p.strip() for p in part.split("-", 1))
            if a not in DAY_NAMES or b not in DAY_NAMES:
                raise ValueError(f"unknown day range '{part}'")
            i, j = DAY_NAMES.index(a), DAY_NAMES.index(b)
            days.update(range(i, j + 1) if i <= j else [*range(i, 7), *range(j + 1)])
        elif part in DAY_NAMES:
            days.add(DAY_NAMES.index(part))
        else:
            raise ValueError(f"unknown day '{part}'")
    if not days:
        raise ValueError("no days")
    return frozenset(days)


def parse_routine(raw: dict[str, Any]) -> Routine:
    rid = str(raw.get("id") or "")
    if not _ID.match(rid):
        raise ValueError(f"routine id '{rid}' must be lowercase letters, digits, '-' or '_'")
    task = str(raw.get("task") or "").strip()
    if not task:
        raise ValueError(f"routine {rid}: 'task' is required")
    try:
        hh, mm = str(raw.get("at", "")).split(":")
        at = time(int(hh), int(mm))
    except ValueError:
        raise ValueError(f"routine {rid}: 'at' must be HH:MM, e.g. 08:00") from None
    days_spec = str(raw.get("days", "daily"))
    try:
        days = parse_days(days_spec)
    except ValueError as e:
        raise ValueError(f"routine {rid}: days: {e}") from None
    period = raw.get("period", "day")
    if period not in PERIODS:
        raise ValueError(f"routine {rid}: period must be one of {PERIODS}")
    deliver = raw.get("deliver", "admins")
    if deliver not in DELIVERY:
        raise ValueError(f"routine {rid}: deliver must be one of {DELIVERY}")
    window = int(raw.get("window_minutes", 180))
    if not 1 <= window <= 24 * 60:
        raise ValueError(f"routine {rid}: window_minutes must be between 1 and 1440")
    return Routine(rid, task, at, days, window, period, deliver, days_spec)

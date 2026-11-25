"""Write today's date to last_run.json every 10-15 or 15-30 days (picked at random each time)."""
from __future__ import annotations

import json
import random
from datetime import date, timedelta
from pathlib import Path

STATE = Path(__file__).resolve().parent.parent / "last_run.json"


def pick_gap() -> int:
    low, high = random.choice([(10, 15), (15, 30)])
    return random.randint(low, high)


def update(today: date) -> bool:
    try:
        due = date.fromisoformat(json.loads(STATE.read_text(encoding="utf-8"))["next"])
    except (OSError, ValueError, KeyError):
        due = today
    if today < due:
        return False
    state = {"date": today.isoformat(), "next": (today + timedelta(days=pick_gap())).isoformat()}
    STATE.write_text(json.dumps(state) + "\n", encoding="utf-8")
    return True


if __name__ == "__main__":
    print("updated" if update(date.today()) else "not due")

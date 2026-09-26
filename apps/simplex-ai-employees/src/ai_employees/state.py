"""Per-employee persistent state: admin overrides, admins, conversation memory, notes,
routine runs and the approval queue."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class EmployeeState:
    """JSON file per employee. Small and human-readable; written atomically."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self.data: dict[str, Any] = {
            "overrides": {},
            "admins": [],
            "contacts": {},
            "history": {},
            "notes": {},
            "routines": {},
            "actions": [],
            "next_action_id": 1,
        }
        if self.path.exists():
            self.data.update(json.loads(self.path.read_text(encoding="utf-8")))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=self.path.name, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    # overrides (runtime config set by admins)

    @property
    def overrides(self) -> dict[str, Any]:
        return self.data["overrides"]

    def set_override(self, key: str, value: Any) -> None:
        self.overrides[key] = value
        self.save()

    def clear_overrides(self) -> None:
        self.data["overrides"] = {}
        self.save()

    # admins (contact IDs in this employee's SimpleX account)

    def is_admin(self, contact_id: int) -> bool:
        return contact_id in self.data["admins"]

    def add_admin(self, contact_id: int) -> None:
        if contact_id not in self.data["admins"]:
            self.data["admins"].append(contact_id)
            self.save()

    def remove_admin(self, contact_id: int) -> None:
        if contact_id in self.data["admins"]:
            self.data["admins"].remove(contact_id)
            self.save()

    @property
    def admins(self) -> list[int]:
        return list(self.data["admins"])

    # contacts seen, for names in reports and the admin UI

    def remember_contact(self, contact_id: int, name: str) -> None:
        if self.data["contacts"].get(str(contact_id)) != name:
            self.data["contacts"][str(contact_id)] = name
            self.save()

    def contact_name(self, contact_id: int) -> str:
        return self.data["contacts"].get(str(contact_id), f"#{contact_id}")

    @property
    def contacts(self) -> dict[int, str]:
        return {int(k): v for k, v in self.data["contacts"].items()}

    # conversation memory: plain-text user/assistant turns per contact

    def history(self, contact_id: int) -> list[dict[str, str]]:
        """Turns as model messages (role and content only)."""
        return [{"role": t["role"], "content": t["content"]} for t in self.timed_history(contact_id)]

    def timed_history(self, contact_id: int) -> list[dict[str, str]]:
        """Turns with their timestamps (`ts`, when recorded)."""
        return list(self.data["history"].get(str(contact_id), []))

    def append_turn(self, contact_id: int, user: str, assistant: str, keep: int) -> None:
        ts = now_iso()
        turns = self.data["history"].setdefault(str(contact_id), [])
        turns += [
            {"role": "user", "content": user, "ts": ts},
            {"role": "assistant", "content": assistant, "ts": ts},
        ]
        if keep > 0 and len(turns) > keep:
            # keep an even number so history always starts with a user turn
            del turns[: len(turns) - (keep - keep % 2)]
        self.save()

    def forget(self, contact_id: int | None = None) -> None:
        if contact_id is None:
            self.data["history"] = {}
            self.data["notes"] = {}
        else:
            self.data["history"].pop(str(contact_id), None)
            self.data["notes"].pop(str(contact_id), None)
        self.save()

    # notes the agent keeps about a contact (remember / recall skills)

    def notes(self, contact_id: int) -> dict[str, str]:
        return dict(self.data["notes"].get(str(contact_id), {}))

    def set_note(self, contact_id: int, key: str, value: str) -> None:
        self.data["notes"].setdefault(str(contact_id), {})[key] = value
        self.save()

    # routines: the last period each one ran for, and its last result

    def routine(self, routine_id: str) -> dict[str, Any]:
        return dict(self.data["routines"].get(routine_id, {}))

    def update_routine(self, routine_id: str, **fields: Any) -> None:
        self.data["routines"].setdefault(routine_id, {}).update(fields)
        self.save()

    # approval queue for outbound actions

    @property
    def actions(self) -> list[dict[str, Any]]:
        return self.data["actions"]

    def add_action(self, **fields: Any) -> dict[str, Any]:
        action = {"id": self.data["next_action_id"], "created": now_iso(), **fields}
        self.data["next_action_id"] += 1
        self.data["actions"].append(action)
        self.save()
        return action

    def action(self, action_id: int) -> dict[str, Any] | None:
        return next((a for a in self.data["actions"] if a["id"] == action_id), None)

    def update_action(self, action_id: int, **fields: Any) -> dict[str, Any]:
        action = self.action(action_id)
        if action is None:
            raise KeyError(action_id)
        action.update(fields)
        self.save()
        return action

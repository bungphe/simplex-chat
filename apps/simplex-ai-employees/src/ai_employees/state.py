"""Per-employee persistent state: admin overrides, admins, conversation memory, notes."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


class EmployeeState:
    """JSON file per employee. Small and human-readable; written atomically."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self.data: dict[str, Any] = {"overrides": {}, "admins": [], "history": {}, "notes": {}}
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

    @property
    def admins(self) -> list[int]:
        return list(self.data["admins"])

    # conversation memory: plain-text user/assistant turns per contact

    def history(self, contact_id: int) -> list[dict[str, str]]:
        return list(self.data["history"].get(str(contact_id), []))

    def append_turn(self, contact_id: int, user: str, assistant: str, keep: int) -> None:
        turns = self.data["history"].setdefault(str(contact_id), [])
        turns += [{"role": "user", "content": user}, {"role": "assistant", "content": assistant}]
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

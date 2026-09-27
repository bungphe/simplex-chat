"""Staff accounts for the admin web UI.

- The owner logs in as "admin" with the password from the config file (admin_ui); that
  account always works, so a lost staff password can never lock the owner out.
- Staff accounts are kept in state_dir/users.json (owner-only file), passwords as
  salted scrypt hashes. Roles:
    admin  everything the owner can do;
    agent  sales staff: the unified inbox only, optionally limited to some channels.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .state import now_iso

OWNER = "admin"
ROLES = ("admin", "agent")
MIN_PASSWORD = 10
_USERNAME = re.compile(r"^[a-z0-9][a-z0-9._-]{1,31}$")
_SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}


@dataclass(frozen=True)
class User:
    username: str
    name: str
    role: str
    channels: tuple[str, ...] = field(default_factory=tuple)  # empty: every channel

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def sees(self, channel: str) -> bool:
        return self.is_admin or not self.channels or channel in self.channels

    def to_dict(self) -> dict[str, Any]:
        return {
            "username": self.username,
            "name": self.name,
            "role": self.role,
            "channels": list(self.channels),
        }


def _hash(password: str, salt: bytes) -> str:
    return hashlib.scrypt(password.encode(), salt=salt, maxmem=64 * 1024 * 1024, **_SCRYPT).hex()


class Users:
    def __init__(self, path: str | os.PathLike[str], owner_password: str):
        self.path = Path(path)
        self._owner_password = owner_password
        self._data: dict[str, dict[str, Any]] = (
            json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        )

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    @staticmethod
    def _user(username: str, d: dict[str, Any]) -> User:
        return User(username, d.get("name") or username, d["role"], tuple(d.get("channels") or ()))

    def owner(self) -> User:
        return User(OWNER, "Chủ", "admin")

    def get(self, username: str) -> User | None:
        if username == OWNER:
            return self.owner()
        d = self._data.get(username)
        return self._user(username, d) if d and not d.get("disabled") else None

    def authenticate(self, username: str, password: str) -> User | None:
        username = username.strip().lower() or OWNER
        if username == OWNER:
            ok = hmac.compare_digest(password.encode(), self._owner_password.encode())
            return self.owner() if ok else None
        d = self._data.get(username)
        if not d or d.get("disabled"):
            _hash(password, b"0" * 16)  # same work either way: no timing hint about usernames
            return None
        if not hmac.compare_digest(_hash(password, bytes.fromhex(d["salt"])), d["hash"]):
            return None
        return self._user(username, d)

    def list(self) -> list[dict[str, Any]]:
        rows = [{**self.owner().to_dict(), "owner": True, "disabled": False}]
        for u, d in sorted(self._data.items()):
            rows.append(
                {
                    **self._user(u, d).to_dict(),
                    "disabled": bool(d.get("disabled")),
                    "created": d.get("created"),
                }
            )
        return rows

    @staticmethod
    def _check(role: str | None, channels: Any, password: str | None) -> None:
        if role is not None and role not in ROLES:
            raise ValueError(f"vai trò phải là một trong {ROLES}")
        if channels is not None and (
            not isinstance(channels, list) or not all(isinstance(c, str) for c in channels)
        ):
            raise ValueError("channels phải là danh sách id kênh")
        if password is not None and len(password) < MIN_PASSWORD:
            raise ValueError(f"mật khẩu cần ít nhất {MIN_PASSWORD} ký tự")

    def add(
        self, username: str, name: str, role: str, password: str, channels: list[str] | None = None
    ) -> User:
        username = username.strip().lower()
        if not _USERNAME.match(username) or username == OWNER:
            raise ValueError("tên đăng nhập: 2-32 ký tự a-z, 0-9, . _ - (không dùng 'admin')")
        if username in self._data:
            raise ValueError("tên đăng nhập đã có")
        self._check(role, channels or [], password)
        salt = secrets.token_bytes(16)
        self._data[username] = {
            "name": name.strip()[:60] or username,
            "role": role,
            "channels": channels or [],
            "salt": salt.hex(),
            "hash": _hash(password, salt),
            "created": now_iso(),
        }
        self._save()
        return self._user(username, self._data[username])

    def update(self, username: str, **fields: Any) -> None:
        d = self._data.get(username)
        if d is None:
            raise KeyError(username)
        self._check(fields.get("role"), fields.get("channels"), fields.get("password"))
        if (password := fields.get("password")) is not None:
            salt = secrets.token_bytes(16)
            d["salt"], d["hash"] = salt.hex(), _hash(password, salt)
        for key in ("role", "channels", "disabled"):
            if fields.get(key) is not None:
                d[key] = fields[key]
        if fields.get("name"):
            d["name"] = str(fields["name"]).strip()[:60]
        self._save()

    def remove(self, username: str) -> None:
        if self._data.pop(username, None) is None:
            raise KeyError(username)
        self._save()

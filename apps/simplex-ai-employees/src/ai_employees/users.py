"""Staff accounts for the admin web UI.

- The owner logs in as "admin" with the password from the config file (admin_ui); that
  account always works, so a lost staff password can never lock the owner out.
- Staff accounts are kept in the office database (shared by all processes), passwords as
  salted scrypt hashes. Roles:
    admin      everything the owner can do;
    manager    store manager: inbox, point of sale, inventory, delivery, marketing and
               reports; cancels and takes back sales (not accounts, models or AI settings);
    agent      sales staff: the unified inbox (optionally limited to some channels) and the
               point of sale;
    cashier    the point of sale only;
    warehouse  inventory: products, purchasing, transfers, stock counts;
    delivery   delivery bookings, trips and drivers;
    marketing  promotions, vouchers, combos, advertising, customer segments, reports.
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

from .db import Database, DocStore
from .state import now_iso

OWNER = "admin"
ROLES = ("admin", "manager", "agent", "cashier", "warehouse", "delivery", "marketing")
# what each role works on (admins: everything); "-read": may look, not change
ROLE_AREAS: dict[str, tuple[str, ...]] = {
    "manager": ("inbox", "pos", "inventory", "delivery", "marketing", "reports", "crm"),
    "agent": ("inbox", "pos"),
    "cashier": ("pos",),
    "warehouse": ("inventory",),
    "delivery": ("delivery",),
    "marketing": ("marketing", "reports", "crm-read", "inventory-read"),
}
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
    def __init__(
        self, docs: DocStore, owner_password: str, legacy_path: str | os.PathLike[str] | None = None
    ):
        self.docs = docs
        self._owner_password = owner_password
        if legacy_path is not None and (old := Path(legacy_path)).exists():  # users.json from before
            accounts = json.loads(old.read_text(encoding="utf-8"))
            self.docs.update("users", lambda d: d.update(accounts), {})
            old.rename(old.with_suffix(".json.imported"))

    @property
    def _data(self) -> dict[str, dict[str, Any]]:
        return self.docs.get("users", {})

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
        self._check(role, channels or [], password)
        salt = secrets.token_bytes(16)
        account = {
            "name": name.strip()[:60] or username,
            "role": role,
            "channels": channels or [],
            "salt": salt.hex(),
            "hash": _hash(password, salt),
            "created": now_iso(),
        }

        def change(d: dict[str, Any]) -> None:
            if username in d:
                raise ValueError("tên đăng nhập đã có")
            d[username] = account

        self.docs.update("users", change, {})
        return self._user(username, account)

    def update(self, username: str, **fields: Any) -> None:
        self._check(fields.get("role"), fields.get("channels"), fields.get("password"))
        hashed = None
        if (password := fields.get("password")) is not None:  # hash once, outside the retry loop
            salt = secrets.token_bytes(16)
            hashed = (salt.hex(), _hash(password, salt))

        def change(accounts: dict[str, Any]) -> None:
            d = accounts.get(username)
            if d is None:
                raise KeyError(username)
            if hashed:
                d["salt"], d["hash"] = hashed
            for key in ("role", "channels", "disabled"):
                if fields.get(key) is not None:
                    d[key] = fields[key]
            if fields.get("name"):
                d["name"] = str(fields["name"]).strip()[:60]

        self.docs.update("users", change, {})

    def remove(self, username: str) -> None:
        def change(accounts: dict[str, Any]) -> None:
            if accounts.pop(username, None) is None:
                raise KeyError(username)

        self.docs.update("users", change, {})


class Sessions:
    """Login sessions in the office database, so any web process accepts them. Only a hash
    of each token is stored: a copy of the database cannot be used to log in."""

    def __init__(self, db: Database):
        self.db = db
        db.script(
            "CREATE TABLE IF NOT EXISTS web_sessions (token TEXT PRIMARY KEY, username TEXT NOT NULL, expiry REAL NOT NULL)"
        )

    @staticmethod
    def _key(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def add(self, token: str, username: str, expiry: float) -> None:
        self.db.execute(
            "INSERT INTO web_sessions (token, username, expiry) VALUES (?, ?, ?)",
            (self._key(token), username, expiry),
        )

    def get(self, token: str) -> tuple[float, str] | None:
        row = self.db.row("SELECT username, expiry FROM web_sessions WHERE token=?", (self._key(token),))
        return (float(row["expiry"]), row["username"]) if row else None

    def remove(self, token: str) -> None:
        self.db.execute("DELETE FROM web_sessions WHERE token=?", (self._key(token),))

    def drop_user(self, username: str) -> None:
        self.db.execute("DELETE FROM web_sessions WHERE username=?", (username,))

    def purge(self, now: float) -> None:
        self.db.execute("DELETE FROM web_sessions WHERE expiry<?", (now,))

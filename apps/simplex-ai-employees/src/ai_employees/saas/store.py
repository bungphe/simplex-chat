"""The control plane's database: tenants, verification codes, invoices, events, sessions.

Rows are plain dicts. Passwords are salted scrypt hashes (as in users.py); session and
verification tokens are stored hashed, so a copy of the database logs nobody in.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from datetime import UTC, datetime
from typing import Any

from ..db import Database, IntegrityError

_SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}
SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
    id {autoid}, slug TEXT NOT NULL UNIQUE, shop_name TEXT NOT NULL, owner_name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE, phone TEXT NOT NULL DEFAULT '', password_hash TEXT NOT NULL,
    plan TEXT NOT NULL, status TEXT NOT NULL, trial_ends TEXT, paid_until TEXT, created TEXT NOT NULL,
    lang TEXT NOT NULL DEFAULT 'vi', region TEXT NOT NULL DEFAULT 'VN',
    admin_port {int}, shop_port {int}, provision_state TEXT NOT NULL DEFAULT 'queued',
    provision_error TEXT NOT NULL DEFAULT '', suspended_at TEXT, cancel_requested {int} NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS email_codes (
    id {autoid}, email TEXT NOT NULL, code_hash TEXT NOT NULL, purpose TEXT NOT NULL,
    created {real} NOT NULL, attempts {int} NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS email_codes_email ON email_codes (email);
CREATE TABLE IF NOT EXISTS invoices (
    id {autoid}, tenant_id {int} NOT NULL, period_start TEXT NOT NULL, period_end TEXT NOT NULL,
    amount {int} NOT NULL, currency TEXT NOT NULL, status TEXT NOT NULL, gateway TEXT NOT NULL DEFAULT '',
    ref TEXT NOT NULL DEFAULT '', due TEXT NOT NULL, created TEXT NOT NULL, paid_at TEXT);
CREATE INDEX IF NOT EXISTS invoices_tenant ON invoices (tenant_id, period_start);
CREATE TABLE IF NOT EXISTS events (
    id {autoid}, tenant_id {int}, ts TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS events_tenant ON events (tenant_id, id);
CREATE TABLE IF NOT EXISTS saas_sessions (
    token TEXT PRIMARY KEY, kind TEXT NOT NULL, subject TEXT NOT NULL, expiry {real} NOT NULL);
"""


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, maxmem=64 * 1024 * 1024, **_SCRYPT)
    return f"{salt.hex()}${digest.hex()}"


def check_password(password: str, stored: str) -> bool:
    salt, _, digest = (stored or "0" * 32 + "$" + "0" * 64).partition("$")
    try:
        computed = hashlib.scrypt(
            password.encode(), salt=bytes.fromhex(salt), maxmem=64 * 1024 * 1024, **_SCRYPT
        )
    except ValueError:
        return False
    return hmac.compare_digest(computed.hex(), digest)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class SaasStore:
    def __init__(self, db: Database):
        self.db = db
        db.script(SCHEMA)

    # --- tenants -----------------------------------------------------------------
    def add_tenant(self, **fields: Any) -> dict[str, Any]:
        cols = dict(fields, created=now_iso())
        names = ", ".join(cols)
        marks = ", ".join("?" for _ in cols)
        try:
            tid = self.db.execute(
                f"INSERT INTO tenants ({names}) VALUES ({marks}) RETURNING id", tuple(cols.values())
            )
        except IntegrityError:
            raise ValueError("slug or email already taken") from None
        tenant = self.tenant(int(tid or 0))
        assert tenant is not None
        return tenant

    def tenant(self, tenant_id: int) -> dict[str, Any] | None:
        return self.db.row("SELECT * FROM tenants WHERE id=?", (tenant_id,))

    def tenant_by(self, column: str, value: str) -> dict[str, Any] | None:
        assert column in ("slug", "email")
        return self.db.row(f"SELECT * FROM tenants WHERE {column}=?", (value,))

    def tenants(self, statuses: tuple[str, ...] = ()) -> list[dict[str, Any]]:
        if statuses:
            marks = ", ".join("?" for _ in statuses)
            return self.db.rows(f"SELECT * FROM tenants WHERE status IN ({marks}) ORDER BY id", statuses)
        return self.db.rows("SELECT * FROM tenants ORDER BY id")

    def update_tenant(self, tenant_id: int, **fields: Any) -> None:
        assert fields
        sets = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE tenants SET {sets} WHERE id=?", (*fields.values(), tenant_id))

    def retire_identifiers(self, tenant: dict[str, Any]) -> None:
        """A deleted tenant keeps its row; its slug and email become free for new sign-ups."""
        tid = tenant["id"]
        self.update_tenant(tid, slug=f"{tenant['slug']}~{tid}", email=f"{tenant['email']}~{tid}")

    # --- verification codes ------------------------------------------------------
    def new_code(self, email: str, purpose: str) -> str:
        code = f"{secrets.randbelow(10**6):06d}"
        self.db.execute("DELETE FROM email_codes WHERE email=? AND purpose=?", (email, purpose))
        self.db.execute(
            "INSERT INTO email_codes (email, code_hash, purpose, created) VALUES (?, ?, ?, ?)",
            (email, _sha(code), purpose, time.time()),
        )
        return code

    def check_code(self, email: str, purpose: str, code: str, max_age: float, max_attempts: int) -> bool:
        row = self.db.row("SELECT * FROM email_codes WHERE email=? AND purpose=?", (email, purpose))
        if (
            row is None
            or time.time() - float(row["created"]) > max_age
            or int(row["attempts"]) >= max_attempts
        ):
            return False
        if not hmac.compare_digest(row["code_hash"], _sha(code.strip())):
            self.db.execute("UPDATE email_codes SET attempts=attempts+1 WHERE id=?", (row["id"],))
            return False
        self.db.execute("DELETE FROM email_codes WHERE id=?", (row["id"],))
        return True

    # --- invoices ----------------------------------------------------------------
    def add_invoice(
        self, tenant_id: int, period_start: str, period_end: str, amount: int, currency: str, due: str
    ) -> dict[str, Any]:
        iid = self.db.execute(
            "INSERT INTO invoices (tenant_id, period_start, period_end, amount, currency, status, due, created) "
            "VALUES (?, ?, ?, ?, ?, 'due', ?, ?) RETURNING id",
            (tenant_id, period_start, period_end, amount, currency, due, now_iso()),
        )
        invoice = self.invoice(int(iid or 0))
        assert invoice is not None
        return invoice

    def invoice(self, invoice_id: int) -> dict[str, Any] | None:
        return self.db.row("SELECT * FROM invoices WHERE id=?", (invoice_id,))

    def invoices(self, tenant_id: int | None = None, statuses: tuple[str, ...] = ()) -> list[dict[str, Any]]:
        where, params = [], []
        if tenant_id is not None:
            where.append("tenant_id=?")
            params.append(tenant_id)
        if statuses:
            where.append(f"status IN ({', '.join('?' for _ in statuses)})")
            params.extend(statuses)
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        return self.db.rows(f"SELECT * FROM invoices{clause} ORDER BY id DESC", params)

    def invoice_for_period(self, tenant_id: int, period_start: str) -> dict[str, Any] | None:
        return self.db.row(
            "SELECT * FROM invoices WHERE tenant_id=? AND period_start=? AND status<>'void'",
            (tenant_id, period_start),
        )

    def update_invoice(self, invoice_id: int, **fields: Any) -> None:
        sets = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE invoices SET {sets} WHERE id=?", (*fields.values(), invoice_id))

    # --- events ------------------------------------------------------------------
    def log(self, tenant_id: int | None, actor: str, action: str, detail: Any = "") -> None:
        text = detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)
        self.db.execute(
            "INSERT INTO events (tenant_id, ts, actor, action, detail) VALUES (?, ?, ?, ?, ?)",
            (tenant_id, now_iso(), actor, action, text[:2000]),
        )

    def events(self, tenant_id: int, limit: int = 100) -> list[dict[str, Any]]:
        return self.db.rows(
            "SELECT * FROM events WHERE tenant_id=? ORDER BY id DESC LIMIT ?", (tenant_id, limit)
        )

    def has_event(self, tenant_id: int, action: str) -> bool:
        return (
            self.db.row("SELECT 1 FROM events WHERE tenant_id=? AND action=?", (tenant_id, action))
            is not None
        )

    # --- sessions ----------------------------------------------------------------
    def add_session(self, kind: str, subject: str, ttl: float) -> str:
        token = secrets.token_urlsafe(32)
        self.db.execute("DELETE FROM saas_sessions WHERE expiry<?", (time.time(),))
        self.db.execute(
            "INSERT INTO saas_sessions (token, kind, subject, expiry) VALUES (?, ?, ?, ?)",
            (_sha(token), kind, subject, time.time() + ttl),
        )
        return token

    def session(self, kind: str, token: str) -> str | None:
        if not token:
            return None
        row = self.db.row(
            "SELECT subject, expiry FROM saas_sessions WHERE token=? AND kind=?", (_sha(token), kind)
        )
        if row is None:
            return None
        if float(row["expiry"]) < time.time():
            self.drop_session(token)
            return None
        return str(row["subject"])

    def drop_session(self, token: str) -> None:
        self.db.execute("DELETE FROM saas_sessions WHERE token=?", (_sha(token),))

    def drop_sessions(self, kind: str, subject: str) -> None:
        self.db.execute("DELETE FROM saas_sessions WHERE kind=? AND subject=?", (kind, subject))

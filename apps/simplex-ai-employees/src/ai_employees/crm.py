"""Customers across channels: one contact for a person, whichever channels they write on.

Every inbox conversation (a customer on one channel) is linked to one contact. A new
conversation gets its own contact; phone numbers and email addresses the customer
writes, or that the channel itself gives (WhatsApp numbers, email senders), fill the
contact's details. Contacts that share a phone number or an email address are shown
to staff as likely duplicates, and merging them joins their conversations: staff see
the customer's whole history, and the AI employee remembers what was said on the
other channels. Nothing is merged automatically.

Companies group contacts (a business customer with several buyers); a contact whose
email domain is a company's domain is linked to it.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from .db import Database
from .state import now_iso

if TYPE_CHECKING:
    from .inbox import Conversation

SCHEMA = """
CREATE TABLE IF NOT EXISTS crm_companies (
  id {id},
  name TEXT NOT NULL,
  domain TEXT NOT NULL DEFAULT '',
  phone TEXT NOT NULL DEFAULT '',
  address TEXT NOT NULL DEFAULT '',
  notes TEXT NOT NULL DEFAULT '',
  created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS crm_contacts (
  id {id},
  name TEXT NOT NULL DEFAULT '',
  phone TEXT NOT NULL DEFAULT '',
  phone_key TEXT NOT NULL DEFAULT '',
  email TEXT NOT NULL DEFAULT '',
  company_id {int} REFERENCES crm_companies(id) ON DELETE SET NULL,
  notes TEXT NOT NULL DEFAULT '',
  created TEXT NOT NULL,
  updated TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS crm_contacts_phone ON crm_contacts (phone_key);
CREATE INDEX IF NOT EXISTS crm_contacts_email ON crm_contacts (email);
CREATE INDEX IF NOT EXISTS crm_contacts_company ON crm_contacts (company_id);
CREATE TABLE IF NOT EXISTS crm_links (
  conversation_id {int} PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
  contact_id {int} NOT NULL REFERENCES crm_contacts(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS crm_links_contact ON crm_links (contact_id)
"""

# Vietnamese numbers (0xx or +84) and other international numbers written with a +
_PHONE = re.compile(r"(?<![\w+])(?:\+?84|0)(?:[ .-]?\d){9}(?!\d)|(?<![\w+])\+\d(?:[ .-]?\d){7,13}(?!\d)")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[a-zA-Z]{2,}")
FREE_MAIL = {"gmail.com", "yahoo.com", "yahoo.com.vn", "hotmail.com", "outlook.com", "icloud.com", "live.com"}
FIELDS = ("name", "phone", "email", "company_id", "notes", "vip")


def phone_key(phone: str) -> str:
    """The number in one form for matching: digits, Vietnamese 0xx as 84xx."""
    digits = re.sub(r"\D", "", phone)
    if digits.startswith("0") and len(digits) == 10:
        digits = "84" + digits[1:]
    return digits if 8 <= len(digits) <= 15 else ""


def find_phone(text: str) -> str:
    m = _PHONE.search(text)
    return m.group(0).strip() if m else ""


def find_email(text: str) -> str:
    m = _EMAIL.search(text)
    return m.group(0).lower() if m else ""


def channel_details(conv: Conversation, channel_type: str) -> dict[str, str]:
    """What the channel itself says about the customer (the WhatsApp number, the email)."""
    if channel_type == "whatsapp" and conv.external_id.endswith("@c.us"):
        return {"phone": "+" + conv.external_id.split("@", 1)[0]}
    if channel_type == "email" and "@" in conv.external_id:
        return {"email": conv.external_id.lower()}
    return {}


class CRM:
    def __init__(self, db: Database):
        self.db = db
        db.script(SCHEMA)
        # added later: VIP customers get VIP prices (see inventory.py)
        if db.postgres:
            db.execute(
                "ALTER TABLE crm_contacts ADD COLUMN IF NOT EXISTS vip {int} NOT NULL DEFAULT 0".replace(
                    "{int}", "BIGINT"
                )
            )
        elif "vip" not in {r["name"] for r in db.rows("PRAGMA table_info(crm_contacts)")}:
            db.execute("ALTER TABLE crm_contacts ADD COLUMN vip INTEGER NOT NULL DEFAULT 0")

    # contacts of conversations

    def contact_of(self, conv_id: int) -> dict[str, Any] | None:
        return self.db.row(
            "SELECT c.* FROM crm_links l JOIN crm_contacts c ON c.id=l.contact_id WHERE l.conversation_id=?",
            (conv_id,),
        )

    def contacts_of(self, conv_ids: list[int]) -> dict[int, dict[str, Any]]:
        if not conv_ids:
            return {}
        rows = self.db.rows(
            "SELECT l.conversation_id AS conv, c.id, c.name, c.company_id FROM crm_links l "
            f"JOIN crm_contacts c ON c.id=l.contact_id WHERE l.conversation_id IN ({','.join('?' * len(conv_ids))})",
            conv_ids,
        )
        return {int(r.pop("conv")): r for r in rows}

    def observe(self, conv: Conversation, text: str, channel_type: str = "") -> dict[str, Any]:
        """A customer message: make sure the conversation has a contact, and keep the
        phone number, email and name it reveals (never overwriting what staff entered)."""
        contact = self.contact_of(conv.id)
        if contact is None:
            contact = self._create(conv, channel_type)
        found = {"name": conv.customer_name, "phone": find_phone(text), "email": find_email(text)}
        missing = {k: v for k, v in found.items() if v and not contact[k]}
        if missing:
            contact = self.update(int(contact["id"]), **missing)
        return contact

    def _create(self, conv: Conversation, channel_type: str) -> dict[str, Any]:
        details = channel_details(conv, channel_type)
        now = now_iso()
        cid = self.db.execute(
            "INSERT INTO crm_contacts (name, phone, phone_key, email, created, updated) "
            "VALUES (?, ?, ?, ?, ?, ?) RETURNING id",
            (
                conv.customer_name,
                details.get("phone", ""),
                phone_key(details.get("phone", "")),
                details.get("email", ""),
                now,
                now,
            ),
        )
        linked = self.db.execute(
            "INSERT INTO crm_links (conversation_id, contact_id) VALUES (?, ?) "
            "ON CONFLICT DO NOTHING RETURNING conversation_id",
            (conv.id, cid),
        )
        if linked is None:  # another process linked it first: use theirs
            self.db.execute("DELETE FROM crm_contacts WHERE id=?", (cid,))
            existing = self.contact_of(conv.id)
            assert existing is not None
            return existing
        contact = self.contact(int(cid or 0))
        assert contact is not None
        if contact["email"]:
            self._link_company(contact)
        return self.contact(int(contact["id"])) or contact

    # contacts

    def contact(self, contact_id: int) -> dict[str, Any] | None:
        return self.db.row("SELECT * FROM crm_contacts WHERE id=?", (contact_id,))

    def update(self, contact_id: int, **fields: Any) -> dict[str, Any]:
        fields = {k: v for k, v in fields.items() if k in FIELDS}
        if "phone" in fields:
            fields["phone"] = str(fields["phone"] or "").strip()[:40]
            fields["phone_key"] = phone_key(fields["phone"])
        if "email" in fields:
            fields["email"] = str(fields["email"] or "").strip().lower()[:200]
        for key, limit in (("name", 120), ("notes", 4000)):
            if key in fields:
                fields[key] = str(fields[key] or "").strip()[:limit]
        if "vip" in fields:
            fields["vip"] = 1 if fields["vip"] else 0
        if "company_id" in fields:
            fields["company_id"] = int(fields["company_id"]) if fields["company_id"] else None
            if fields["company_id"] is not None and self.company(fields["company_id"]) is None:
                raise ValueError("no such company")
        if fields:
            sets = ", ".join(f"{k}=?" for k in fields)
            self.db.execute(
                f"UPDATE crm_contacts SET {sets}, updated=? WHERE id=?",
                (*fields.values(), now_iso(), contact_id),
            )
        contact = self.contact(contact_id)
        if contact is None:
            raise KeyError(contact_id)
        if fields.get("email") and not contact["company_id"]:
            self._link_company(contact)
            contact = self.contact(contact_id) or contact
        return contact

    def _link_company(self, contact: dict[str, Any]) -> None:
        domain = contact["email"].rpartition("@")[2]
        if not domain or domain in FREE_MAIL:
            return
        company = self.db.row("SELECT id FROM crm_companies WHERE domain=? ORDER BY id LIMIT 1", (domain,))
        if company:
            self.db.execute(
                "UPDATE crm_contacts SET company_id=? WHERE id=? AND company_id IS NULL",
                (company["id"], contact["id"]),
            )

    def conversations(self, contact_id: int) -> list[int]:
        return [
            int(r["conversation_id"])
            for r in self.db.rows(
                "SELECT conversation_id FROM crm_links WHERE contact_id=? ORDER BY conversation_id",
                (contact_id,),
            )
        ]

    def search(
        self, query: str = "", company_id: int | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        sql = (
            "SELECT c.id, c.name, c.phone, c.email, c.company_id, c.notes, c.vip, c.created, "
            "MAX(v.last_ts) AS last_ts, COUNT(v.id) AS conversation_count "
            "FROM crm_contacts c LEFT JOIN crm_links l ON l.contact_id=c.id "
            "LEFT JOIN conversations v ON v.id=l.conversation_id WHERE 1=1"
        )
        args: list[Any] = []
        if query:
            like = f"%{query.strip()}%"
            sql += " AND (LOWER(c.name) LIKE LOWER(?) OR c.phone LIKE ? OR LOWER(c.email) LIKE LOWER(?) OR c.phone_key LIKE ?)"
            args += [like, like, like, f"%{phone_key(query) or query.strip()}%"]
        if company_id is not None:
            sql += " AND c.company_id=?"
            args.append(company_id)
        sql += (
            " GROUP BY c.id, c.name, c.phone, c.email, c.company_id, c.notes, c.vip, c.created"
            " ORDER BY last_ts DESC NULLS LAST, c.id DESC LIMIT ?"
        )
        return self.db.rows(sql, [*args, limit])

    def duplicates(self, contact_id: int | None = None) -> list[dict[str, Any]]:
        """Groups of contacts with the same phone number or email address."""
        groups = []
        for field, key in (("phone_key", "phone"), ("email", "email")):
            sql = f"SELECT {field} AS v FROM crm_contacts WHERE {field}<>''"
            args: list[Any] = []
            if contact_id is not None:
                sql += f" AND {field}=(SELECT {field} FROM crm_contacts WHERE id=?)"
                args.append(contact_id)
            sql += f" GROUP BY {field} HAVING COUNT(*)>1 LIMIT 100"
            for r in self.db.rows(sql, args):
                members = self.db.rows(
                    f"SELECT id, name, phone, email FROM crm_contacts WHERE {field}=? ORDER BY id", (r["v"],)
                )
                groups.append({"reason": key, "value": members[0][key], "contacts": members})
        return groups

    def merge(self, keep: int, other: int) -> dict[str, Any]:
        """Join `other` into `keep`: its conversations move over, and empty details are
        filled from it (details already on `keep` win)."""
        if keep == other:
            raise ValueError("cannot merge a contact with itself")
        a, b = self.contact(keep), self.contact(other)
        if a is None or b is None:
            raise KeyError(other if a else keep)
        filled = {
            k: b[k] for k in ("name", "phone", "phone_key", "email", "company_id", "vip") if not a[k] and b[k]
        }
        if b["notes"]:
            filled["notes"] = f"{a['notes']}\n{b['notes']}".strip()[:4000]
        with self.db.transaction():
            self.db.execute("UPDATE crm_links SET contact_id=? WHERE contact_id=?", (keep, other))
            if filled:
                sets = ", ".join(f"{k}=?" for k in filled)
                self.db.execute(f"UPDATE crm_contacts SET {sets} WHERE id=?", (*filled.values(), keep))
            self.db.execute("UPDATE crm_contacts SET updated=? WHERE id=?", (now_iso(), keep))
            self.db.execute("DELETE FROM crm_contacts WHERE id=?", (other,))
        merged = self.contact(keep)
        assert merged is not None
        return merged

    def split(self, conv: Conversation) -> dict[str, Any]:
        """Undo a wrong merge: the conversation gets a contact of its own again."""
        current = self.contact_of(conv.id)
        if current is not None and len(self.conversations(int(current["id"]))) == 1:
            return current  # already on its own
        self.db.execute("DELETE FROM crm_links WHERE conversation_id=?", (conv.id,))
        return self._create(conv, "")

    # companies

    def companies(self) -> list[dict[str, Any]]:
        return self.db.rows(
            "SELECT co.*, (SELECT COUNT(*) FROM crm_contacts c WHERE c.company_id=co.id) AS contact_count "
            "FROM crm_companies co ORDER BY co.name"
        )

    def company(self, company_id: int) -> dict[str, Any] | None:
        return self.db.row("SELECT * FROM crm_companies WHERE id=?", (company_id,))

    def save_company(self, company_id: int | None, **fields: Any) -> dict[str, Any]:
        clean = {
            k: str(fields.get(k) or "").strip()[:limit]
            for k, limit in (("name", 120), ("domain", 120), ("phone", 40), ("address", 300), ("notes", 4000))
            if k in fields
        }
        if "domain" in clean:
            clean["domain"] = clean["domain"].lower().removeprefix("@").removeprefix("www.")
        if company_id is None:
            if not clean.get("name"):
                raise ValueError("Tên công ty là bắt buộc")
            company_id = self.db.execute(
                "INSERT INTO crm_companies (name, domain, phone, address, notes, created) "
                "VALUES (?, ?, ?, ?, ?, ?) RETURNING id",
                (*(clean.get(k, "") for k in ("name", "domain", "phone", "address", "notes")), now_iso()),
            )
        elif clean:
            if "name" in clean and not clean["name"]:
                raise ValueError("Tên công ty là bắt buộc")
            sets = ", ".join(f"{k}=?" for k in clean)
            self.db.execute(f"UPDATE crm_companies SET {sets} WHERE id=?", (*clean.values(), company_id))
        company = self.company(int(company_id or 0))
        if company is None:
            raise KeyError(company_id)
        if company["domain"] and company["domain"] not in FREE_MAIL:
            # contacts already writing from this domain join it
            self.db.execute(
                "UPDATE crm_contacts SET company_id=? WHERE company_id IS NULL AND email LIKE ?",
                (company["id"], f"%@{company['domain']}"),
            )
        return company

    def delete_company(self, company_id: int) -> None:
        with self.db.transaction():
            self.db.execute("UPDATE crm_contacts SET company_id=NULL WHERE company_id=?", (company_id,))
            self.db.execute("DELETE FROM crm_companies WHERE id=?", (company_id,))

"""Privacy and personal-data compliance (Nghị định 13/2023/NĐ-CP essentials).

- AI disclosure: the first answer the AI sends in a conversation starts with a notice that
  the customer is talking to an AI assistant (once per conversation; a `disclosed` column on
  the conversation remembers it, claimed atomically so two processes never both send it).
- The shop's privacy policy: a text the owner edits in the admin UI, rendered on the web
  shop at /privacy with the shop's name, address and contact details filled in.
- Right of access and erasure: `export_contact` gathers everything stored about a CRM
  contact; `erase_contact` removes it. Erasure keeps the books: orders and delivery
  bookings stay as rows with the customer's details replaced by "[đã xoá]" and their
  contact link cleared; conversations (messages, attachments, labels) are deleted
  outright; loyalty point rows, web-shop sessions and login codes are deleted; the
  contact row itself is deleted; the AI employees forget the customer (turns, summaries,
  notes, language). Every export and erasure is written to `privacy_log` (who, when,
  what, how many rows; never the erased details themselves).
- Retention: `apply_retention` deletes messages older than `retention_days` from closed
  conversations (attachments are part of the message rows).

Settings live in the office documents under "privacy_settings" (see `DEFAULTS`).
"""

from __future__ import annotations

import html
import json
import logging
import re
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from aiohttp import web

from .crm import phone_key
from .i18n import tr
from .state import now_iso

if TYPE_CHECKING:
    from .db import Database
    from .employee import Employee, Office
    from .inbox import Conversation

log = logging.getLogger(__name__)

KEY = "privacy_settings"
ERASED = "[đã xoá]"
LOG_ACTIONS = ("export", "erase", "retention")
MAX_TEXT = 20_000

DEFAULT_DISCLOSURE = (
    "Bạn đang trò chuyện với trợ lý AI của {shop}. Nhắn 'nhân viên' hoặc /staff để gặp người thật. "
    "Chính sách bảo mật: {url}"
)

DEFAULT_POLICY = """# Chính sách bảo mật dữ liệu cá nhân của {shop}

{shop} ("chúng tôi") tôn trọng quyền riêng tư của khách hàng. Chính sách này cho biết chúng tôi thu thập, sử dụng, lưu trữ và bảo vệ dữ liệu cá nhân của bạn như thế nào khi bạn nhắn tin, đặt hàng hoặc truy cập website của chúng tôi, theo Nghị định 13/2023/NĐ-CP về bảo vệ dữ liệu cá nhân.

## 1. Dữ liệu chúng tôi thu thập
- Họ tên, số điện thoại, email, địa chỉ giao hàng.
- Nội dung trao đổi với chúng tôi trên các kênh chat (SimpleX, Zalo, Facebook Messenger, website, email…), kể cả ảnh và tệp bạn gửi.
- Đơn hàng, thanh toán, lịch sử mua hàng, điểm tích luỹ.
- Ngôn ngữ bạn dùng và tên hiển thị trên nền tảng chat.

## 2. Mục đích sử dụng
- Tư vấn và trả lời tin nhắn của bạn (bởi nhân viên hoặc trợ lý AI).
- Xử lý đơn hàng, giao hàng, bảo hành, xuất hoá đơn và chăm sóc sau bán.
- Thông báo về đơn hàng; thông báo ưu đãi khi bạn đồng ý nhận.
- Thống kê nội bộ để cải thiện dịch vụ.
Chúng tôi không bán dữ liệu cá nhân của bạn.

## 3. Trợ lý AI và các bên xử lý dữ liệu
Một phần tin nhắn được trợ lý AI trả lời. Khi đó nội dung trao đổi và thông tin cần thiết để trả lời được gửi tới nhà cung cấp mô hình AI mà chúng tôi sử dụng, để xử lý thay mặt chúng tôi. Bạn có thể yêu cầu gặp người thật bất cứ lúc nào bằng cách nhắn "nhân viên".
Dữ liệu cũng đi qua nền tảng chat bạn dùng (Zalo, Meta, Telegram…) theo chính sách của nền tảng đó. Đơn vị vận chuyển nhận tên, số điện thoại và địa chỉ để giao hàng.

## 4. Thời gian lưu trữ
Nội dung chat được lưu {retention}. Dữ liệu đơn hàng và hoá đơn được lưu theo thời hạn của pháp luật về kế toán và thuế.

## 5. Quyền của bạn
- Xem và nhận bản sao dữ liệu cá nhân của mình.
- Sửa thông tin chưa đúng.
- Yêu cầu xoá dữ liệu: chúng tôi xoá nội dung chat, trí nhớ của trợ lý AI và tài khoản mua hàng; đơn hàng được ẩn danh (giữ số liệu kế toán, bỏ tên, số điện thoại, địa chỉ).
- Rút lại sự đồng ý, phản đối hoặc hạn chế việc xử lý dữ liệu.
Chúng tôi trả lời yêu cầu của bạn trong vòng 72 giờ kể từ khi nhận được.

## 6. Bảo mật
Dữ liệu được lưu trên máy chủ do chúng tôi quản lý; chỉ nhân viên được phân quyền mới truy cập. Tin nhắn SimpleX được mã hoá đầu-cuối giữa bạn và tài khoản của cửa hàng.

## 7. Cookie trên website
Website chỉ dùng cookie cần thiết để giữ giỏ hàng và phiên đăng nhập của bạn; không dùng cookie theo dõi quảng cáo.

## 8. Liên hệ
{shop} – {address}
Điện thoại: {phone} · Email: {email}
Chính sách này có thể được cập nhật; bản mới nhất luôn ở {url}."""

DEFAULTS: dict[str, Any] = {
    "ai_disclosure": True,
    "disclosure_text": DEFAULT_DISCLOSURE,
    "policy_text": DEFAULT_POLICY,
    "retention_days": 0,  # 0: keep chat messages forever
    "contact_email": "",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS privacy_log (
  id {id}, ts TEXT NOT NULL, actor TEXT NOT NULL DEFAULT '', action TEXT NOT NULL,
  contact_id {int}, subject TEXT NOT NULL DEFAULT '', counts TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS privacy_log_by_time ON privacy_log (ts)
"""

_PLACEHOLDER = re.compile(r"\{(shop|address|phone|email|url|retention)\}")
# a line is cut into segments so that one with an empty value can be left out
_SEGMENTS = re.compile(r"((?<=[.!?])\s+|\s+[·–|]\s+)")


def ensure_schema(db: Database) -> None:
    """The audit table and the per-conversation disclosure flag (called by the hub)."""
    db.script(SCHEMA)
    db.add_columns("conversations", {"disclosed": "TEXT"})


def _has_table(db: Database, name: str) -> bool:
    if db.postgres:
        sql = "SELECT 1 AS x FROM information_schema.tables WHERE table_schema=current_schema() AND table_name=?"
    else:
        sql = "SELECT 1 AS x FROM sqlite_master WHERE type='table' AND name=?"
    return db.row(sql, (name,)) is not None


# ---------------------------------------------------------------------------- #
# settings


def settings(office: Office) -> dict[str, Any]:
    saved = office.docs.get(KEY) or {}
    return {**DEFAULTS, **{k: v for k, v in saved.items() if k in DEFAULTS}}


def save_settings(office: Office, changes: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    if "ai_disclosure" in changes:
        clean["ai_disclosure"] = bool(changes["ai_disclosure"])
    for key in ("disclosure_text", "policy_text"):
        if key in changes:
            clean[key] = str(changes[key] or "").replace("\r\n", "\n").strip()[:MAX_TEXT]
    if "contact_email" in changes:
        clean["contact_email"] = str(changes["contact_email"] or "").strip()[:200]
    if "retention_days" in changes:
        try:
            days = int(changes["retention_days"] or 0)
        except (TypeError, ValueError):
            raise ValueError(tr("Số ngày lưu tin nhắn phải là số")) from None
        if not 0 <= days <= 3650:
            raise ValueError(tr("Số ngày lưu tin nhắn phải từ 0 đến 3650"))
        clean["retention_days"] = days
    if clean:
        office.docs.update(KEY, lambda d: d.update(clean), {})
    return settings(office)


def policy_url(office: Office) -> str:
    """The public address of the policy page, when the web shop is configured."""
    shop = getattr(office, "storefront", None)
    base = (getattr(shop, "public_url", "") or "").rstrip("/")
    return f"{base}/privacy" if base else ""


def values(office: Office) -> dict[str, str]:
    """What {shop}, {address}, {phone}, {email}, {url} and {retention} stand for."""
    s = office.inventory.settings()
    p = settings(office)
    days = int(p["retention_days"] or 0)
    return {
        "shop": s["shop_name"] or tr("Cửa hàng"),
        "address": s["shop_address"],
        "phone": s["shop_phone"],
        "email": p["contact_email"],
        "url": policy_url(office),
        "retention": tr("{0} ngày", days) if days > 0 else tr("cho đến khi bạn yêu cầu xoá"),
    }


def fill(text: str, values: dict[str, str]) -> str:
    """Substitute the placeholders. A sentence or " · "-separated segment whose value is
    empty (no email, no web shop) is left out rather than shown with a hole."""
    out = []
    for line in text.split("\n"):
        parts = _SEGMENTS.split(line)  # segment, separator, segment, separator, ...
        kept = ""
        for i in range(0, len(parts), 2):
            segment = parts[i]
            if any(not values.get(name) for name in _PLACEHOLDER.findall(segment)):
                continue
            kept += (parts[i - 1] if kept and i else "") + segment
        out.append(_PLACEHOLDER.sub(lambda m: values.get(m.group(1), ""), kept))
    return "\n".join(out)


# ---------------------------------------------------------------------------- #
# AI disclosure


def disclosure_for(office: Office, conv: Conversation, channel_type: str) -> str | None:
    """The notice to send before this conversation's first AI answer, or None when it was
    sent already (or the shop turned it off). Claiming the conversation and reading the
    text is one statement, so only one process ever sends it."""
    p = settings(office)
    if not p["ai_disclosure"] or not p["disclosure_text"].strip():
        return None
    claimed = office.hub.inbox.db.execute(
        "UPDATE conversations SET disclosed=? WHERE id=? AND disclosed IS NULL RETURNING id",
        (now_iso(), conv.id),
    )
    if claimed is None:
        return None
    log.info("privacy: AI disclosure for conversation %s (%s)", conv.id, channel_type or "?")
    return fill(p["disclosure_text"], values(office)).strip()


def undisclose(office: Office, conv_id: int) -> None:
    """The notice could not be delivered: the next answer carries it again."""
    office.hub.inbox.db.execute("UPDATE conversations SET disclosed=NULL WHERE id=?", (conv_id,))


# ---------------------------------------------------------------------------- #
# the policy page (web shop)


def policy_html(text: str) -> str:
    """Plain text to HTML: "# " / "## " headings, "- " bullets, blank lines between paragraphs."""
    e = html.escape
    out: list[str] = []
    paragraph: list[str] = []
    bullets: list[str] = []

    def flush() -> None:
        if paragraph:
            out.append("<p>" + "<br>".join(e(x) for x in paragraph) + "</p>")
            paragraph.clear()
        if bullets:
            out.append("<ul>" + "".join(f"<li>{e(x)}</li>" for x in bullets) + "</ul>")
            bullets.clear()

    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            flush()
        elif line.startswith("- "):
            if paragraph:
                flush()
            bullets.append(line[2:].strip())
        elif line.startswith("#"):
            flush()
            level = 1 if not line.startswith("##") else 2
            out.append(f"<h{level}>{e(line.lstrip('#').strip())}</h{level}>")
        else:
            if bullets:
                flush()
            paragraph.append(line)
    flush()
    return "".join(out)


async def policy_page(request: web.Request) -> web.Response:
    from .storefront import OFFICE, _page

    office = request.app[OFFICE]
    body = policy_html(fill(settings(office)["policy_text"], values(office)))
    return _page(request, tr("Chính sách bảo mật"), f'<section class="policy">{body}</section>')


def shop_routes(r: web.UrlDispatcher) -> None:
    from .storefront import _with_customer

    r.add_get("/privacy", _with_customer(policy_page))


# ---------------------------------------------------------------------------- #
# right of access: everything about one contact


def _mask(name: str, phone: str) -> str:
    """How the audit log refers to a customer, without keeping their details."""
    parts = []
    if name.strip():
        parts.append(name.strip()[0] + "…")
    if digits := re.sub(r"\D", "", phone):
        parts.append("…" + digits[-3:])
    return " ".join(parts)


def _log(
    db: Database, actor: str, action: str, contact_id: int | None, subject: str, counts: dict[str, Any]
) -> int:
    return int(
        db.execute(
            "INSERT INTO privacy_log (ts, actor, action, contact_id, subject, counts) "
            "VALUES (?, ?, ?, ?, ?, ?) RETURNING id",
            (now_iso(), actor[:80], action, contact_id, subject[:80], json.dumps(counts, ensure_ascii=False)),
        )
        or 0
    )


def log_entries(office: Office, limit: int = 200) -> list[dict[str, Any]]:
    rows = office.hub.inbox.db.rows("SELECT * FROM privacy_log ORDER BY id DESC LIMIT ?", (limit,))
    for r in rows:
        r["counts"] = json.loads(r["counts"]) if r["counts"] else {}
    return rows


def _memory_owners(office: Office, conv: Conversation) -> list[Employee]:
    """The employees whose memory may hold this conversation's customer: a SimpleX contact
    id belongs to one employee's account; an external contact id may have been answered by
    any employee the conversation was assigned to."""
    if conv.is_simplex:
        e = office.employees.get(conv.channel.split(":", 1)[1])
        return [e] if e else []
    return list(office.employees.values())


def _order_ids(db: Database, contact_id: int, conv_ids: list[int]) -> list[int]:
    if not _has_table(db, "inv_orders"):
        return []
    sql, args = "SELECT id FROM inv_orders WHERE contact_id=?", [contact_id]
    if conv_ids:
        sql += f" OR conversation_id IN ({','.join('?' * len(conv_ids))})"
        args += conv_ids
    return [int(r["id"]) for r in db.rows(sql + " ORDER BY id", args)]


def export_contact(office: Office, contact_id: int, actor: str = "") -> dict[str, Any]:
    """Everything stored about a customer, as one JSON-able document (their right of access).
    Internal staff notes are kept apart (`internal_notes`) so they can be left out of what
    is handed over; the shop's costs and margins are not included."""
    hub = office.hub
    crm, inbox, db = hub.crm, hub.inbox, hub.inbox.db
    contact = crm.contact(contact_id)
    if contact is None:
        raise KeyError(contact_id)
    conv_ids = crm.conversations(contact_id)
    conversations = []
    memory = []
    for cid in conv_ids:
        conv = inbox.conversation(cid)
        if conv is None:
            continue
        msgs = db.rows(
            "SELECT id, sender, author, text, ts, attachments, translation FROM messages "
            "WHERE conversation_id=? ORDER BY id",
            (cid,),
        )
        for m in msgs:
            m["attachments"] = json.loads(m["attachments"]) if m.get("attachments") else []
        conversations.append(
            {
                **conv.to_dict(),
                "labels": inbox.labels(cid),
                "messages": [m for m in msgs if m["sender"] != "note"],
                "internal_notes": [m for m in msgs if m["sender"] == "note"],
            }
        )
        for e in _memory_owners(office, conv):
            st = e.state
            turns = st.timed_history(conv.contact_id) + st.unsummarized(conv.contact_id)
            summary, notes, lang = (
                st.summary(conv.contact_id),
                st.notes(conv.contact_id),
                st.language(conv.contact_id),
            )
            if turns or summary or notes or lang:
                memory.append(
                    {
                        "employee": e.id,
                        "conversation_id": cid,
                        "contact_id": conv.contact_id,
                        "turns": turns,
                        "summary": summary,
                        "notes": notes,
                        "language": lang,
                    }
                )
    orders = []
    for oid in _order_ids(db, contact_id, conv_ids):
        o = office.inventory.order(oid)
        for key in ("cost", "profit", "expected_profit"):
            o.pop(key, None)
        orders.append(o)
    order_ids = [int(o["id"]) for o in orders]
    bookings: list[dict[str, Any]] = []
    if order_ids and _has_table(db, "dl_bookings"):
        bookings = db.rows(
            f"SELECT * FROM dl_bookings WHERE order_id IN ({','.join('?' * len(order_ids))}) ORDER BY id",
            order_ids,
        )
    points = (
        db.rows("SELECT * FROM crm_points WHERE contact_id=? ORDER BY id", (contact_id,))
        if _has_table(db, "crm_points")
        else []
    )
    sessions = codes = 0
    if _has_table(db, "sf_sessions"):
        sessions = int(db.row("SELECT COUNT(*) AS n FROM sf_sessions WHERE contact_id=?", (contact_id,))["n"])
        codes = int(db.row("SELECT COUNT(*) AS n FROM sf_codes WHERE contact_id=?", (contact_id,))["n"])
    company = crm.company(int(contact["company_id"])) if contact.get("company_id") else None
    out = {
        "exported_at": now_iso(),
        "shop": office.inventory.settings().get("shop_name", ""),
        "contact": {**contact, "company": company["name"] if company else ""},
        "conversations": conversations,
        "orders": orders,
        "delivery_bookings": bookings,
        "loyalty_points": points,
        "storefront": {"sessions": sessions, "login_codes": codes},
        "ai_memory": memory,
    }
    _log(
        db,
        actor,
        "export",
        contact_id,
        _mask(contact["name"], contact["phone"]),
        {
            "conversations": len(conversations),
            "messages": sum(len(c["messages"]) for c in conversations),
            "orders": len(orders),
        },
    )
    return out


# ---------------------------------------------------------------------------- #
# right of erasure


def matches_confirmation(contact: dict[str, Any], confirm: str) -> bool:
    """The typed confirmation must be the customer's name or phone number (or "#id" for a
    customer without either)."""
    typed = " ".join(str(confirm or "").split()).casefold()
    if not typed:
        return False
    if contact["name"] and typed == " ".join(contact["name"].split()).casefold():
        return True
    if contact["phone"] and (
        typed == contact["phone"].casefold() or phone_key(typed) == contact["phone_key"]
    ):
        return True
    return typed in (f"#{contact['id']}", str(contact["id"])) and not (contact["name"] or contact["phone"])


def erase_contact(office: Office, contact_id: int, actor: str) -> dict[str, Any]:
    """Remove a customer's personal data (see the module doc for what is deleted and what
    is anonymised). The inbox, CRM, orders, loyalty and web-shop rows change in one
    transaction; the employees' memories (separate databases in the SQLite setup) are
    cleared right after and reported in `memory`."""
    hub = office.hub
    crm, inbox, db = hub.crm, hub.inbox, hub.inbox.db
    contact = crm.contact(contact_id)
    if contact is None:
        raise KeyError(contact_id)
    subject = _mask(contact["name"], contact["phone"])
    conv_ids = crm.conversations(contact_id)
    convs = [c for c in (inbox.conversation(cid) for cid in conv_ids) if c is not None]
    order_ids = _order_ids(db, contact_id, conv_ids)
    counts: dict[str, Any] = {
        "conversations": len(convs),
        "messages": 0,
        "orders": len(order_ids),
        "bookings": 0,
        "points_rows": 0,
        "points": int(contact.get("points") or 0),
        "sessions": 0,
        "login_codes": 0,
    }
    marks = ",".join("?" * len(conv_ids))
    order_marks = ",".join("?" * len(order_ids))
    with db.transaction():
        if conv_ids:
            counts["messages"] = int(
                db.row(f"SELECT COUNT(*) AS n FROM messages WHERE conversation_id IN ({marks})", conv_ids)[
                    "n"
                ]
            )
            # messages, labels and the CRM link go with the conversation (ON DELETE CASCADE);
            # explicit for databases created before foreign keys were enforced
            db.execute(f"DELETE FROM messages WHERE conversation_id IN ({marks})", conv_ids)
            db.execute(f"DELETE FROM conversation_labels WHERE conversation_id IN ({marks})", conv_ids)
            db.execute(f"DELETE FROM crm_links WHERE conversation_id IN ({marks})", conv_ids)
            db.execute(f"DELETE FROM conversations WHERE id IN ({marks})", conv_ids)
        if order_ids:
            db.execute(
                f"UPDATE inv_orders SET customer_name=?, phone=?, address=?, email=?, note='', "
                f"contact_id=NULL, conversation_id=NULL WHERE id IN ({order_marks})",
                (ERASED, ERASED, ERASED, ERASED, *order_ids),
            )
            if _has_table(db, "dl_bookings"):
                counts["bookings"] = int(
                    db.row(
                        f"SELECT COUNT(*) AS n FROM dl_bookings WHERE order_id IN ({order_marks})", order_ids
                    )["n"]
                )
                db.execute(
                    f"UPDATE dl_bookings SET customer_name=?, phone=?, address=?, lat='', lng='', notes='' "
                    f"WHERE order_id IN ({order_marks})",
                    (ERASED, ERASED, ERASED, *order_ids),
                )
        if _has_table(db, "crm_points"):
            counts["points_rows"] = int(
                db.row("SELECT COUNT(*) AS n FROM crm_points WHERE contact_id=?", (contact_id,))["n"]
            )
            db.execute("DELETE FROM crm_points WHERE contact_id=?", (contact_id,))
        if _has_table(db, "sf_sessions"):
            counts["sessions"] = int(
                db.row("SELECT COUNT(*) AS n FROM sf_sessions WHERE contact_id=?", (contact_id,))["n"]
            )
            counts["login_codes"] = int(
                db.row("SELECT COUNT(*) AS n FROM sf_codes WHERE contact_id=?", (contact_id,))["n"]
            )
            db.execute("DELETE FROM sf_sessions WHERE contact_id=?", (contact_id,))
            db.execute("DELETE FROM sf_codes WHERE contact_id=?", (contact_id,))
        db.execute("DELETE FROM crm_contacts WHERE id=?", (contact_id,))
        counts["log_id"] = _log(db, actor, "erase", contact_id, subject, dict(counts))
    # the employees' memory of the customer (their own databases when SQLite is used)
    memory: dict[str, int] = {}
    for conv in convs:
        for e in _memory_owners(office, conv):
            st, who = e.state, conv.contact_id
            try:
                turns = len(st.timed_history(who)) + len(st.unsummarized(who))
                known = turns or st.notes(who) or st.summary(who) or st.language(who)
                st.forget(who)
                st.db.execute("DELETE FROM mem_contacts WHERE employee=? AND contact=?", (e.id, who))
                if known:
                    memory[e.id] = memory.get(e.id, 0) + turns
            except Exception:  # the customer's rows are gone already; report, do not undo
                log.exception("privacy: memory of %s for contact %s not cleared", e.id, conv.contact_id)
                memory[e.id] = -1
    counts["memory"] = memory
    log.info("privacy: %s erased customer %s (%s): %s", actor or "-", contact_id, subject, counts)
    return {"contact_id": contact_id, "subject": subject, **counts}


# ---------------------------------------------------------------------------- #
# retention


STATE_KEY = "privacy_state"  # when the retention job last ran (shared by all processes)
RETENTION_EVERY = timedelta(hours=6)


def apply_retention(office: Office, now: datetime | None = None, force: bool = False) -> dict[str, Any]:
    """Delete messages older than `retention_days` from closed conversations (their
    attachments are part of the rows). Nothing happens while the setting is 0. Safe to
    call from every scheduler tick: it runs at most every `RETENTION_EVERY` unless forced."""
    days = int(settings(office)["retention_days"] or 0)
    if days <= 0:
        return {"retention_days": 0, "messages": 0}
    moment = now or datetime.now().astimezone()
    if not force:
        last = (office.docs.get(STATE_KEY) or {}).get("retention_ran")
        try:
            if last and moment - datetime.fromisoformat(last) < RETENTION_EVERY:
                return {"retention_days": days, "messages": 0, "skipped": True}
        except (TypeError, ValueError):
            pass
        office.docs.update(
            STATE_KEY, lambda d: d.__setitem__("retention_ran", moment.isoformat(timespec="seconds")), {}
        )
    cutoff = (moment - timedelta(days=days)).isoformat(timespec="seconds")
    db = office.hub.inbox.db
    old = "conversation_id IN (SELECT id FROM conversations WHERE status='closed') AND ts<?"
    with db.transaction():
        n = int(db.row(f"SELECT COUNT(*) AS n FROM messages WHERE {old}", (cutoff,))["n"])
        if n:
            db.execute(f"DELETE FROM messages WHERE {old}", (cutoff,))
            db.execute(
                "UPDATE conversations SET last_preview='' WHERE status='closed' AND last_preview<>'' "
                "AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.conversation_id=conversations.id)"
            )
            _log(db, "scheduler", "retention", None, "", {"messages": n, "days": days, "before": cutoff})
    if n:
        log.info("privacy: retention removed %d message(s) older than %d days", n, days)
    return {"retention_days": days, "messages": n, "before": cutoff}


# ---------------------------------------------------------------------------- #
# admin API (admins only)


def _w() -> Any:
    from . import web as w

    return w


def _admin(request: web.Request) -> Any:
    w = _w()
    user = w._user(request)
    if not user.is_admin:
        raise w.ApiError(403, tr("Chỉ quản trị viên quản lý quyền riêng tư"))
    return user


def _office(request: web.Request) -> Office:
    return request.app[_w().OFFICE]


def _contact_id(request: web.Request) -> int:
    try:
        return int(request.match_info["id"])
    except ValueError:
        raise _w().ApiError(400, tr("{0} phải là số", "id")) from None


def _settings_json(office: Office) -> dict[str, Any]:
    return {
        "settings": settings(office),
        "policy_url": policy_url(office),
        "defaults": {"disclosure_text": DEFAULT_DISCLOSURE, "policy_text": DEFAULT_POLICY},
        "preview": fill(settings(office)["disclosure_text"], values(office)).strip(),
    }


async def api_settings(request: web.Request) -> web.Response:
    _admin(request)
    return _w()._json(_settings_json(_office(request)))


async def api_settings_save(request: web.Request) -> web.Response:
    w = _w()
    user = _admin(request)
    data = await w._body(request)
    try:
        save_settings(_office(request), data)
    except ValueError as e:
        raise w.ApiError(400, str(e)) from None
    log.info("admin UI: %s changed the privacy settings (%s)", user.username, ", ".join(sorted(data)))
    return w._json(_settings_json(_office(request)))


async def api_export(request: web.Request) -> web.Response:
    w = _w()
    user = _admin(request)
    cid = _contact_id(request)
    try:
        data = export_contact(_office(request), cid, user.username)
    except KeyError:
        raise w.ApiError(404, tr("Không có khách hàng này")) from None
    body = json.dumps(data, ensure_ascii=False, indent=1, default=str)
    return web.Response(
        text=body,
        content_type="application/json",
        charset="utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="khach-hang-{cid}.json"',
            "Cache-Control": "no-store",
        },
    )


async def api_erase(request: web.Request) -> web.Response:
    w = _w()
    user = _admin(request)
    office = _office(request)
    cid = _contact_id(request)
    data = await w._body(request)
    contact = office.hub.crm.contact(cid)
    if contact is None:
        raise w.ApiError(404, tr("Không có khách hàng này"))
    if not matches_confirmation(contact, str(data.get("confirm") or "")):
        raise w.ApiError(400, tr("Gõ đúng tên hoặc số điện thoại của khách để xác nhận xoá"))
    summary = erase_contact(office, cid, user.username)
    return w._json({"erased": summary})


async def api_log(request: web.Request) -> web.Response:
    _admin(request)
    return _w()._json({"log": log_entries(_office(request))})


def add_routes(r: web.UrlDispatcher) -> None:
    r.add_get("/api/privacy/settings", api_settings)
    r.add_put("/api/privacy/settings", api_settings_save)
    r.add_get(r"/api/privacy/contacts/{id:\d+}/export", api_export)
    r.add_post(r"/api/privacy/contacts/{id:\d+}/erase", api_erase)
    r.add_get("/api/privacy/log", api_log)

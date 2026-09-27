"""Admin web UI: a JSON API plus a single page, served from the office process.

Security model:
- Password login per account (users.py): the owner ("admin", password from the config)
  and staff accounts with a role; "agent" accounts reach only the inbox, optionally only
  some channels. Sessions are random tokens in HttpOnly, SameSite=Strict cookies.
- Every state-changing request must carry `X-Requested-With: ai-employees`, which a
  cross-site form or image cannot send, so a logged-in browser cannot be driven by
  another site.
- API keys are write-only: the UI can set a key, never read one back.
- Binds to 127.0.0.1 by default; put a TLS reverse proxy in front to reach it remotely.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import secrets
import time
from dataclasses import replace
from importlib import resources
from typing import TYPE_CHECKING, Any

from aiohttp import BodyPartReader, web

from . import i18n
from . import skills as sk
from .config import EFFORT_LEVELS, AdminUIConfig, ConfigError
from .i18n import tr
from .providers import PROVIDERS, ModelError
from .users import ROLE_AREAS, Sessions, User, Users

if TYPE_CHECKING:
    from .employee import Employee, Office

log = logging.getLogger(__name__)

COOKIE = "aie_session"
SESSION_TTL = 12 * 3600
LOGIN_DELAY = 1.0  # seconds after a wrong password: slows down guessing
LOGIN_MAX_FAILURES = 10  # wrong passwords per address, and per account, within LOGIN_WINDOW
LOGIN_WINDOW = 15 * 60
CSRF_HEADER = "X-Requested-With"
CSRF_VALUE = "ai-employees"
STATIC = (
    "admin.html",
    "admin.js",
    "admin.css",
    "inventory.js",
    "business.js",
    "i18n.js",
    "icon.svg",
    "manifest.webmanifest",
)
# the installable app (PWA): icons, and the service worker served at the root (its scope)
STATIC_BINARY = ("icon-192.png", "icon-512.png", "apple-touch-icon.png")
CONTENT_TYPES = {
    "html": "text/html",
    "js": "application/javascript",
    "css": "text/css",
    "svg": "image/svg+xml",
    "webmanifest": "application/manifest+json",
    "png": "image/png",
}
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}

UI_LANG = "ui_lang"  # cookie: the admin UI's language in this browser
OFFICE: web.AppKey[Office] = web.AppKey("office")
PASSWORD: web.AppKey[str] = web.AppKey("password")
SESSIONS: web.AppKey[Sessions] = web.AppKey("sessions")
USERS: web.AppKey[Users] = web.AppKey("users")
USER: web.RequestKey[User] = web.RequestKey("user")
LOGIN_FAILURES: web.AppKey[dict[str, list[float]]] = web.AppKey("login_failures")


# What each staff role may call besides its own account (admins: everything).
_AREA_PREFIXES = {
    "inbox": ("/api/inbox",),
    "pos": ("/api/pos",),
    "inventory": ("/api/inventory",),
    "delivery": ("/api/delivery",),
    "marketing": ("/api/marketing",),
    "reports": ("/api/reports",),
    "crm": ("/api/crm",),
}


def _may(user: User, method: str, path: str) -> bool:
    if user.is_admin:
        return True
    if path in (
        "/api/me",
        "/api/me/password",
        "/api/me/lang",
        "/api/logout",
        "/api/notices",
    ) or path.startswith(("/api/notices/", "/api/me/simplex")):
        return True
    if method == "GET" and path == "/api/channels":
        return True
    # settings, the marketplace and mail credentials stay with admins (store managers may look)
    if path.startswith(("/api/inventory/settings", "/api/inventory/marketplaces", "/api/inventory/mail")):
        return method == "GET" and user.role == "manager"
    areas = ROLE_AREAS.get(user.role, ())
    for area in areas:
        if area.endswith("-read"):
            if method == "GET" and any(path.startswith(p) for p in _AREA_PREFIXES[area[:-5]]):
                return True
        elif any(path == p or path.startswith(p + "/") for p in _AREA_PREFIXES[area]):
            return True
    return False


def _user(request: web.Request) -> User:
    return request[USER]


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _json(data: Any, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, dumps=lambda d: json.dumps(d, ensure_ascii=False))


@web.middleware
async def guard(request: web.Request, handler: Any) -> web.StreamResponse:
    path = request.path
    try:
        if path.startswith("/api/"):
            if request.method not in ("GET", "HEAD") and request.headers.get(CSRF_HEADER) != CSRF_VALUE:
                raise ApiError(403, "missing request header")
            i18n.activate(request_language(request))
            if path != "/api/login":
                user = _session_user(request)
                if user is None:
                    raise ApiError(401, "not logged in")
                i18n.activate(request_language(request, user))  # texts and errors in their language
                if not _may(user, request.method, path):
                    raise ApiError(403, tr("Tài khoản của bạn không có quyền này"))
                request[USER] = user
        resp = await handler(request)
    except ApiError as e:
        resp = _json({"error": str(e)}, status=e.status)
    except web.HTTPException:
        raise
    except Exception:
        log.exception("admin UI: %s %s failed", request.method, path)
        resp = _json({"error": "internal error"}, status=500)
    for k, v in SECURITY_HEADERS.items():
        resp.headers.setdefault(k, v)  # a handler may relax caching (attachments)
    return resp


def _session_user(request: web.Request) -> User | None:
    token = request.cookies.get(COOKIE, "")
    sessions = request.app[SESSIONS]
    session = sessions.get(token) if token else None
    if session is None:
        return None
    expiry, username = session
    user = request.app[USERS].get(username)  # a disabled or deleted account loses its sessions
    if expiry < time.time() or user is None:
        sessions.remove(token)
        return None
    return user


def _drop_sessions(app: web.Application, username: str) -> None:
    app[SESSIONS].drop_user(username)


async def _body(request: web.Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except json.JSONDecodeError:
        raise ApiError(400, "invalid JSON") from None
    if not isinstance(data, dict):
        raise ApiError(400, "expected a JSON object")
    return data


def _employee(request: web.Request) -> Employee:
    e = request.app[OFFICE].employees.get(request.match_info["emp"])
    if e is None:
        raise ApiError(404, "no such employee")
    return e


# --------------------------------------------------------------------------- #
# Pages and login


async def page(request: web.Request) -> web.Response:
    name = request.match_info.get("file") or {
        "/sw.js": "sw.js",
        "/manifest.webmanifest": "manifest.webmanifest",
    }.get(request.path, "admin.html")
    if name not in (*STATIC, *STATIC_BINARY, "sw.js"):
        raise web.HTTPNotFound()
    ctype = CONTENT_TYPES[name.rsplit(".", 1)[1]]
    files = resources.files("ai_employees").joinpath("static", name)
    if name in STATIC_BINARY:
        return web.Response(body=files.read_bytes(), content_type=ctype)
    return web.Response(text=files.read_text(encoding="utf-8"), content_type=ctype, charset="utf-8")


async def no_content(request: web.Request) -> web.Response:
    return web.Response(status=204)


def _behind_proxy(request: web.Request) -> bool:
    """The request came from our own reverse proxy (a loopback or private address, e.g. Docker's)."""
    try:
        return ipaddress.ip_address(request.remote or "").is_private
    except ValueError:
        return False


def _client_ip(request: web.Request) -> str:
    """The visitor's address; behind our reverse proxy, the X-Forwarded-For entry it added."""
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded and _behind_proxy(request):
        return forwarded.split(",")[-1].strip()
    return request.remote or ""


def _https(request: web.Request) -> bool:
    """Served over TLS: directly, or by the TLS reverse proxy in front (X-Forwarded-Proto)."""
    proto = request.headers.get("X-Forwarded-Proto", "").split(",")[-1].strip().lower()
    return request.secure or (proto == "https" and _behind_proxy(request))


def _start_session(request: web.Request, resp: web.Response, user: User) -> None:
    token = secrets.token_urlsafe(32)
    request.app[SESSIONS].purge(time.time())
    request.app[SESSIONS].add(token, user.username, time.time() + SESSION_TTL)
    resp.set_cookie(
        COOKIE, token, httponly=True, samesite="Strict", secure=_https(request), max_age=SESSION_TTL, path="/"
    )


def _login_keys(request: web.Request, username: str) -> tuple[str, str]:
    return "ip:" + _client_ip(request), "user:" + (username.strip().lower() or "admin")[:64]


def _login_blocked(request: web.Request, keys: tuple[str, ...]) -> bool:
    failures = request.app[LOGIN_FAILURES]
    since = time.monotonic() - LOGIN_WINDOW
    for key in [k for k, times in failures.items() if not times or times[-1] < since]:
        del failures[key]  # forget old failures (keeps the table small)
    return any(sum(1 for t in failures.get(k, ()) if t >= since) >= LOGIN_MAX_FAILURES for k in keys)


async def login(request: web.Request) -> web.Response:
    data = await _body(request)
    username, password = str(data.get("username", "")), str(data.get("password", ""))
    keys = _login_keys(request, username)
    failures = request.app[LOGIN_FAILURES]
    if _login_blocked(request, keys):
        log.warning("admin UI: login for %r from %s refused: too many failures", username[:40], keys[0][3:])
        raise ApiError(
            429, tr("Đăng nhập sai quá nhiều lần; vui lòng thử lại sau {0} phút", LOGIN_WINDOW // 60)
        )
    user = await asyncio.to_thread(request.app[USERS].authenticate, username, password)
    if user is None:
        for key in keys:
            failures.setdefault(key, []).append(time.monotonic())
        await asyncio.sleep(LOGIN_DELAY)  # slow down guessing
        log.warning("admin UI: failed login for %r from %s", username[:40], keys[0][3:])
        raise ApiError(401, tr("Sai tên đăng nhập hoặc mật khẩu"))
    failures.pop(keys[1], None)  # the address keeps its count: one's own account cannot reset it
    log.info("admin UI: %s logged in", user.username)
    resp = _json({"ok": True, "user": user.to_dict()})
    _start_session(request, resp, user)
    return resp


async def logout(request: web.Request) -> web.Response:
    request.app[SESSIONS].remove(request.cookies.get(COOKIE, ""))
    resp = _json({"ok": True})
    resp.del_cookie(COOKIE, path="/")
    return resp


# --------------------------------------------------------------------------- #
# Accounts


async def me(request: web.Request) -> web.Response:
    return _json({"user": _user(request).to_dict()})


async def me_language(request: web.Request) -> web.Response:
    data = await _body(request)
    code = i18n.normalize(str(data.get("lang") or ""))
    if data.get("lang") and code is None:
        raise ApiError(400, i18n.tr("Ngôn ngữ không hỗ trợ"))
    request.app[USERS].set_language(_user(request).username, code or "")
    resp = _json({"lang": code or ""})
    if code:
        resp.set_cookie(UI_LANG, code, max_age=365 * 86400, samesite="Lax", path="/")
    return resp


def request_language(request: web.Request, user: User | None = None) -> str:
    """The staff member's choice, else the language this browser asked for, else the office's."""
    return (
        (user.lang if user else "")
        or i18n.normalize(request.cookies.get(UI_LANG))
        or i18n.best_match(request.headers.get("Accept-Language"))
        or i18n.default()
    )


async def i18n_catalog(request: web.Request) -> web.Response:
    """The admin UI's texts in the visitor's language: loaded before the page's scripts."""
    lang = (
        i18n.normalize(request.cookies.get(UI_LANG))
        or i18n.best_match(request.headers.get("Accept-Language"))
        or i18n.default()
    )
    ui = i18n.ui_texts()
    data = {
        "lang": lang,
        "rtl": lang in i18n.RTL,
        "languages": i18n.LANGUAGES,
        "msgs": {k: v for k, v in i18n.catalog(lang).items() if k in ui},
    }
    body = "const I18N = " + json.dumps(data, ensure_ascii=False) + ";\n"
    return web.Response(text=body, content_type="application/javascript", charset="utf-8")


async def me_password(request: web.Request) -> web.Response:
    user = _user(request)
    data = await _body(request)
    if user.username == "admin":
        raise ApiError(400, tr("Mật khẩu của tài khoản chủ đặt trong file cấu hình (admin_ui)"))
    users = request.app[USERS]
    if await asyncio.to_thread(users.authenticate, user.username, str(data.get("old", ""))) is None:
        await asyncio.sleep(1.0)
        raise ApiError(400, tr("Mật khẩu hiện tại không đúng"))
    try:
        await asyncio.to_thread(users.update, user.username, password=str(data.get("new", "")))
    except ValueError as e:
        raise ApiError(400, str(e)) from None
    _drop_sessions(request.app, user.username)  # logged in elsewhere (a lost phone?): not any more
    resp = _json({"ok": True})
    _start_session(request, resp, user)  # this browser stays logged in
    return resp


def _channel_ids(office: Office) -> set[str]:
    return {f"simplex:{e}" for e in office.employees} | set(office.hub.channels)


async def users_list(request: web.Request) -> web.Response:
    office = request.app[OFFICE]
    channels = [{"id": c, **_channel_label(office, c)} for c in sorted(_channel_ids(office))]
    return _json({"users": request.app[USERS].list(), "channels": channels})


def _check_channels(request: web.Request, channels: Any) -> None:
    if not isinstance(channels, list) or not all(isinstance(c, str) for c in channels):
        raise ApiError(400, tr("channels phải là danh sách id kênh"))
    if set(channels) - _channel_ids(request.app[OFFICE]):
        raise ApiError(400, tr("Có kênh không tồn tại"))


async def users_add(request: web.Request) -> web.Response:
    data = await _body(request)
    channels = data.get("channels") or []
    _check_channels(request, channels)
    try:
        user = await asyncio.to_thread(
            request.app[USERS].add,
            str(data.get("username", "")),
            str(data.get("name", "")),
            str(data.get("role", "agent")),
            str(data.get("password", "")),
            channels,
        )
    except ValueError as e:
        raise ApiError(400, str(e)) from None
    log.info("admin UI: %s added account %s (%s)", _user(request).username, user.username, user.role)
    return await users_list(request)


async def users_patch(request: web.Request) -> web.Response:
    username = request.match_info["username"]
    data = await _body(request)
    if data.get("channels") is not None:
        _check_channels(request, data["channels"])
    fields = {k: data.get(k) for k in ("name", "role", "channels", "disabled", "password")}
    if fields["password"] is not None:
        fields["password"] = str(fields["password"])
    try:
        await asyncio.to_thread(request.app[USERS].update, username, **fields)
    except KeyError:
        raise ApiError(404, tr("Không có tài khoản này")) from None
    except ValueError as e:
        raise ApiError(400, str(e)) from None
    if any(fields[k] is not None for k in ("role", "channels", "disabled", "password")):
        _drop_sessions(request.app, username)  # new rights or password: log in again
    return await users_list(request)


async def users_delete(request: web.Request) -> web.Response:
    username = request.match_info["username"]
    try:
        request.app[USERS].remove(username)
    except KeyError:
        raise ApiError(404, tr("Không có tài khoản này")) from None
    _drop_sessions(request.app, username)
    return await users_list(request)


# --------------------------------------------------------------------------- #
# Overview and employees


def _routines(e: Employee) -> list[dict[str, Any]]:
    s = e.settings
    local = e.local_now()
    out = []
    for r in s.routines:
        st = e.state.routine(r.id)
        nxt = r.next_start(local, st.get("period"))
        out.append(
            {
                "id": r.id,
                "schedule": r.describe(),
                "task": r.task,
                "deliver": r.deliver,
                "paused": r.id in s.paused_routines,
                "next": nxt.isoformat(timespec="minutes") if nxt else None,
                "next_local": f"{nxt:%H:%M %d/%m}" if nxt else None,
                "timezone": s.timezone,
                "last_run": st.get("last_run"),
                "last_status": st.get("last_status"),
                "last_output": st.get("last_output"),
            }
        )
    return out


def _summary(e: Employee, stats: dict[str, Any]) -> dict[str, Any]:
    s = e.settings
    profile = e.office.model_profile(s.model)
    return {
        "id": e.id,
        "display_name": s.display_name,
        "short_descr": s.short_descr,
        "paused": s.paused,
        "model": s.model,
        "model_desc": profile.describe() if profile else "?",
        "address": getattr(e.bot, "address", None),
        "admins": len(e.state.admins),
        "contacts": len(e.state.contacts),
        "pending": len(e.actions.pending()),
        "memory_pending": sum(1 for m in e.state.shared_memory if m["status"] == "pending"),
        "routines": _routines(e),
        "stats": stats.get(e.id, {}),
    }


async def overview(request: web.Request) -> web.Response:
    office = request.app[OFFICE]
    stats = office.runlog.summary(hours=24)
    return _json({"employees": [_summary(e, stats) for e in office.employees.values()]})


def _detail(e: Employee) -> dict[str, Any]:
    s = e.settings
    office = e.office
    return {
        **_summary(e, office.runlog.summary(hours=24)),
        "system_prompt": s.system_prompt,
        "effort": s.effort,
        "effort_levels": list(EFFORT_LEVELS),
        "skills": list(s.skills),
        "available_skills": [
            {
                "name": n,
                "description": sk.REGISTRY[n].description
                if n in sk.REGISTRY
                else tr("Nhóm skill: ") + ", ".join(sk.GROUPS.get(n, ())),
            }
            for n in sk.available()
        ],
        "releases": list(s.releases),
        "actions": list(office.config.actions),
        "corrections": list(s.corrections),
        "models": office.model_names(),
        "overrides": list(e.state.overrides),
        "admin_contacts": [{"id": c, "name": e.state.contact_name(c)} for c in e.state.admins],
        "welcome": s.welcome,
        "shared_memory": e.state.shared_memory,
    }


async def employee_get(request: web.Request) -> web.Response:
    return _json(_detail(_employee(request)))


async def employee_patch(request: web.Request) -> web.Response:
    e = _employee(request)
    data = await _body(request)
    office = request.app[OFFICE]
    st = e.state
    if "system_prompt" in data:
        text = str(data["system_prompt"]).strip()
        if not text:
            raise ApiError(400, tr("Prompt không được để trống"))
        st.set_override("system_prompt", text)
    if "model" in data:
        if office.model_profile(str(data["model"])) is None:
            raise ApiError(400, tr("Model '{0}' chưa được khai báo", data["model"]))
        st.set_override("model", str(data["model"]))
    if "effort" in data:
        if data["effort"] is not None and data["effort"] not in EFFORT_LEVELS:
            raise ApiError(400, tr("effort không hợp lệ"))
        st.set_override("effort", data["effort"])
    if "paused" in data:
        st.set_override("paused", bool(data["paused"]))
    if "skills" in data:
        skills = [str(x) for x in data["skills"]]
        if unknown := [x for x in skills if x not in sk.available()]:
            raise ApiError(400, tr("Skill không tồn tại: {0}", ", ".join(unknown)))
        st.set_override("skills", skills)
    if "releases" in data:
        releases = [str(x) for x in data["releases"]]
        if unknown := [x for x in releases if x not in office.config.actions]:
            raise ApiError(400, tr("Hành động không tồn tại: {0}", ", ".join(unknown)))
        st.set_override("releases", releases)
    return _json(_detail(e))


async def employee_reset(request: web.Request) -> web.Response:
    e = _employee(request)
    e.state.clear_overrides()
    return _json(_detail(e))


async def memory_add(request: web.Request) -> web.Response:
    e = _employee(request)
    text = str((await _body(request)).get("text", "")).strip()
    if not text:
        raise ApiError(400, tr("Nội dung trống"))
    e.state.add_memory(text, "active", tr("quản trị {0}", _user(request).name))
    return _json(_detail(e))


async def memory_approve(request: web.Request) -> web.Response:
    e = _employee(request)
    data = await _body(request)
    fields: dict[str, Any] = {"status": "active", "approved_by": _user(request).name}
    if isinstance(data.get("text"), str) and data["text"].strip():
        fields["text"] = " ".join(data["text"].split())[:500]  # approve an edited version
    try:
        e.state.update_memory(int(request.match_info["mid"]), **fields)
    except (KeyError, ValueError):
        raise ApiError(404, tr("Không có ghi nhớ này")) from None
    return _json(_detail(e))


async def memory_delete(request: web.Request) -> web.Response:
    e = _employee(request)
    try:
        e.state.remove_memory(int(request.match_info["mid"]))
    except (KeyError, ValueError):
        raise ApiError(404, tr("Không có ghi nhớ này")) from None
    return _json(_detail(e))


async def correction_add(request: web.Request) -> web.Response:
    e = _employee(request)
    text = str((await _body(request)).get("text", "")).strip()
    if not text:
        raise ApiError(400, tr("Nội dung trống"))
    e.add_correction(text)
    return _json(_detail(e))


async def correction_delete(request: web.Request) -> web.Response:
    e = _employee(request)
    try:
        e.remove_correction(int(request.match_info["n"]))
    except (ValueError, IndexError):
        raise ApiError(404, "no such correction") from None
    return _json(_detail(e))


async def admin_remove(request: web.Request) -> web.Response:
    e = _employee(request)
    e.state.remove_admin(_int(request.match_info["cid"], "cid"))
    return _json(_detail(e))


# --------------------------------------------------------------------------- #
# Routines


async def routine_run(request: web.Request) -> web.Response:
    e = _employee(request)
    r = e.settings.routine(request.match_info["rid"])
    if r is None:
        raise ApiError(404, "no such routine")
    res = await e.run_routine(r, e.local_now(), manual=True)
    return _json({"status": res.status, "text": res.text})


async def routine_pause(request: web.Request) -> web.Response:
    e = _employee(request)
    rid = request.match_info["rid"]
    if e.settings.routine(rid) is None:
        raise ApiError(404, "no such routine")
    e.set_routine_paused(rid, bool((await _body(request)).get("paused")))
    return _json(_detail(e))


# --------------------------------------------------------------------------- #
# Approvals


async def approvals(request: web.Request) -> web.Response:
    office = request.app[OFFICE]

    def tagged(e: Employee, actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{**a, "employee": e.id, "employee_name": e.settings.display_name} for a in actions]

    pending = [a for e in office.employees.values() for a in tagged(e, e.state.pending_actions())]
    pending.sort(key=lambda a: a["created"])
    recent = [
        a
        for e in office.employees.values()
        for a in tagged(e, e.state.recent_actions(150))
        if a["status"] != "pending"
    ]
    recent.sort(key=lambda a: a["created"], reverse=True)
    return _json({"pending": pending, "recent": recent[:100]})


async def approval_decide(request: web.Request) -> web.Response:
    e = _employee(request)
    n = _int(request.match_info["n"], "n")
    decision = request.match_info["decision"]
    by = "web admin"
    if decision == "approve":
        msg = await e.actions.approve(n, by=by)
    elif decision == "reject":
        msg = await e.actions.reject(n, by=by, reason=str((await _body(request)).get("reason", "")).strip())
    else:
        raise ApiError(404, "unknown decision")
    return _json({"message": msg, "action": e.state.action(n)})


# --------------------------------------------------------------------------- #
# Models


def _model_rows(office: Office) -> list[dict[str, Any]]:
    rows = []
    for name in office.model_names():
        p = office.model_profile(name)
        if p is None:
            continue
        rows.append(
            {
                "name": name,
                "provider": p.provider,
                "model": p.model,
                "base_url": p.base_url,
                "api_key_env": p.api_key_env,
                "has_key": bool(p.key()),
                "source": "ui" if name in office.runtime_models else "config",
                "used_by": [e.id for e in office.employees.values() if e.settings.model == name],
                "describe": p.describe(),
            }
        )
    return rows


async def models_get(request: web.Request) -> web.Response:
    return _json({"models": _model_rows(request.app[OFFICE]), "providers": list(PROVIDERS)})


async def model_add(request: web.Request) -> web.Response:
    office = request.app[OFFICE]
    data = await _body(request)
    name = str(data.pop("name", "")).strip()
    if not name or not name.replace("-", "").replace("_", "").isalnum():
        raise ApiError(400, tr("Tên model chỉ gồm chữ, số, '-' hoặc '_'"))
    raw = {k: v for k, v in data.items() if v not in (None, "", {})}
    if isinstance(raw.get("extra_body"), str):
        try:
            raw["extra_body"] = json.loads(raw["extra_body"])
        except json.JSONDecodeError:
            raise ApiError(400, tr("extra_body phải là JSON")) from None
    try:
        office.add_runtime_model(name, raw)
    except ConfigError as e:
        raise ApiError(400, str(e)) from None
    return _json({"models": _model_rows(office)})


async def model_delete(request: web.Request) -> web.Response:
    office = request.app[OFFICE]
    name = request.match_info["name"]
    try:
        office.remove_runtime_model(name)
    except KeyError:
        raise ApiError(
            400, tr("Chỉ xoá được model thêm từ giao diện; model trong file cấu hình sửa trong file")
        ) from None
    return _json({"models": _model_rows(office)})


async def model_test(request: web.Request) -> web.Response:
    office = request.app[OFFICE]
    name = request.match_info["name"]
    model = office.model_for(name)
    if model is None:
        raise ApiError(404, "no such model")
    base = next(iter(office.employees.values())).base
    settings = replace(base, effort=None, max_tokens=512)
    started = time.monotonic()
    try:
        turn = await model.step(
            system=("Reply with the single word OK.", "This is a connection test."),
            messages=model.messages([], "ping"),
            tools=[],
            settings=settings,
        )
    except ModelError as e:
        return _json({"ok": False, "error": str(e)[:500]})
    ms = int((time.monotonic() - started) * 1000)
    return _json({"ok": True, "text": turn.text[:200], "ms": ms, "stop": turn.stop})


# --------------------------------------------------------------------------- #
# Conversations and run log


async def conversations(request: web.Request) -> web.Response:
    e = _employee(request)
    rows = [{**r, "admin": e.state.is_admin(r["id"])} for r in e.state.contact_overview()]
    return _json({"contacts": rows})


async def conversation_get(request: web.Request) -> web.Response:
    e = _employee(request)
    cid = _int(request.match_info["cid"], "cid")
    return _json(
        {
            "id": cid,
            "name": e.state.contact_name(cid),
            "turns": e.state.timed_history(cid),
            "notes": e.state.notes(cid),
            "summary": e.state.summary(cid),
        }
    )


async def conversation_forget(request: web.Request) -> web.Response:
    e = _employee(request)
    e.state.forget(_int(request.match_info["cid"], "cid"))
    return _json({"ok": True})


async def runlog(request: web.Request) -> web.Response:
    office = request.app[OFFICE]
    q = request.query
    limit = max(1, min(_int(q.get("limit", "200"), "limit"), 2000))
    rows = office.runlog.tail(limit=limit, employee=q.get("employee") or None, kind=q.get("kind") or None)
    return _json({"records": list(reversed(rows))})


# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Unified inbox


def _hub(request: web.Request):
    return request.app[OFFICE].hub


def _channel_label(office: Office, channel: str) -> dict[str, str]:
    if channel.startswith("simplex:"):
        e = office.employees.get(channel.split(":", 1)[1])
        return {"type": "simplex", "name": f"SimpleX · {e.settings.display_name if e else channel}"}
    ch = office.hub.channels.get(channel)
    names = {
        "zalo_oa": "Zalo OA",
        "zalo_personal": tr("Zalo cá nhân"),
        "facebook": "Messenger",
        "webhook": "Webhook",
        "telegram": "Telegram",
        "whatsapp": "WhatsApp",
        "email": "Email",
    }
    return {"type": ch.type if ch else "?", "name": f"{names.get(ch.type, '?') if ch else '?'} · {channel}"}


def _conv_json(office: Office, conv: Any, labels: list[str] | None = None) -> dict[str, Any]:
    from . import lang

    e = office.employees.get(conv.employee)
    language = e.state.language(conv.contact_id) if e else {}
    code = language.get("lang", "")
    return {
        **conv.to_dict(),
        "channel_info": _channel_label(office, conv.channel),
        "employee_name": e.settings.display_name if e else conv.employee,
        "lang": code,
        "lang_name": tr(lang.name(code, "vi")) if code else "",  # the table's Vietnamese names
        "lang_source": language.get("source", ""),
        "country": language.get("country", ""),
        "staff_language": office.config.staff_language,
        "labels": labels if labels is not None else office.hub.inbox.labels(conv.id),
    }


async def inbox_list(request: web.Request) -> web.Response:
    office = request.app[OFFICE]
    q = request.query
    user = _user(request)
    who = q.get("assignee") or None
    teams = None
    if who == "me":  # assigned to me, or to one of my teams and nobody in particular
        who, teams = user.username, office.hub.desk.teams_of(user.username) or None
    convs = office.hub.inbox.list(
        channel=q.get("channel") or None,
        mode=q.get("mode") or None,
        query=q.get("q") or None,
        status=q.get("status") if q.get("status") in ("open", "closed") else None,
        label=q.get("label") or None,
        assignee=who,
        teams=teams,
        team=q.get("team") or None,
        waiting=q.get("waiting") == "1",
    )
    convs = [c for c in convs if user.sees(c.channel)]
    labels = office.hub.inbox.labels_for([c.id for c in convs])
    channels = [
        {"id": f"simplex:{e.id}", **_channel_label(office, f"simplex:{e.id}")}
        for e in office.employees.values()
    ] + [{"id": c, **_channel_label(office, c)} for c in office.hub.channels]
    return _json(
        {
            "conversations": [_conv_json(office, c, labels.get(c.id, [])) for c in convs],
            "channels": [c for c in channels if user.sees(c["id"])],
        }
    )


def _inbox_conv(request: web.Request) -> Any:
    try:
        conv = _hub(request).inbox.conversation(int(request.match_info["cid"]))
    except ValueError:
        conv = None
    if conv is None or not _user(request).sees(conv.channel):
        raise ApiError(404, "no such conversation")
    return conv


async def inbox_get(request: web.Request) -> web.Response:
    office = request.app[OFFICE]
    conv = _inbox_conv(request)
    e = office.employees.get(conv.employee)
    return _json(
        {
            "conversation": _conv_json(office, conv),
            "messages": office.hub.inbox.messages(conv.id),
            "notes": e.state.notes(conv.contact_id) if e else {},
            "summary": e.state.summary(conv.contact_id) if e else "",
            "employees": [{"id": x.id, "name": x.settings.display_name} for x in office.employees.values()],
        }
    )


async def inbox_reply(request: web.Request) -> web.Response:
    conv = _inbox_conv(request)
    data = await _body(request)
    text = str(data.get("text", "")).strip()
    if not text:
        raise ApiError(400, tr("Nội dung trống"))
    author = _user(request).name  # who replied is the logged-in account, not a typed name
    try:
        await _hub(request).human_reply(
            conv.id,
            text,
            author,
            take_over=bool(data.get("take_over", True)),
            translate=bool(data.get("translate")),
        )
    except Exception as e:  # noqa: BLE001 - surface the platform's error to the agent
        raise ApiError(502, tr("Không gửi được: {0}", e)) from None
    return await inbox_get(request)


async def inbox_mode(request: web.Request) -> web.Response:
    conv = _inbox_conv(request)
    mode = (await _body(request)).get("mode")
    if mode not in ("ai", "human"):
        raise ApiError(400, tr("mode phải là ai hoặc human"))
    hub = _hub(request)
    hub.inbox.set_mode(conv.id, mode)
    if mode == "ai" and hub.inbox.pending_customer_text(conv.id):
        request.app[OFFICE].cluster.request_reply(conv, 0)  # answer what is waiting
    return await inbox_get(request)


async def inbox_assign(request: web.Request) -> web.Response:
    conv = _inbox_conv(request)
    emp = str((await _body(request)).get("employee", ""))
    if emp not in request.app[OFFICE].employees or conv.is_simplex:
        raise ApiError(400, tr("Không đổi được nhân viên cho hội thoại này"))
    _hub(request).inbox.set_employee(conv.id, emp)
    return await inbox_get(request)


async def inbox_note(request: web.Request) -> web.Response:
    conv = _inbox_conv(request)
    text = str((await _body(request)).get("text", "")).strip()
    if not text:
        raise ApiError(400, tr("Nội dung trống"))
    _hub(request).add_note(conv.id, text[:4000], _user(request).name)
    return await inbox_get(request)


async def inbox_status(request: web.Request) -> web.Response:
    conv = _inbox_conv(request)
    status = (await _body(request)).get("status")
    if status not in ("open", "closed"):
        raise ApiError(400, tr("status phải là open hoặc closed"))
    _hub(request).inbox.set_status(conv.id, status)
    return await inbox_get(request)


async def inbox_assignee(request: web.Request) -> web.Response:
    conv = _inbox_conv(request)
    data = await _body(request)
    assignee, team = str(data.get("assignee") or ""), str(data.get("team") or "")
    users = request.app[USERS]
    if assignee and ((u := users.get(assignee)) is None or not u.sees(conv.channel)):
        raise ApiError(400, tr("Tài khoản này không có hoặc không được xem kênh này"))
    if team and team not in {t["id"] for t in _hub(request).desk.teams}:
        raise ApiError(400, tr("Không có nhóm này"))
    _hub(request).inbox.set_assignee(conv.id, assignee, team)
    return await inbox_get(request)


async def inbox_labels(request: web.Request) -> web.Response:
    conv = _inbox_conv(request)
    labels = (await _body(request)).get("labels")
    known = {lb["name"] for lb in _hub(request).desk.labels}
    if (
        not isinstance(labels, list)
        or not all(isinstance(x, str) for x in labels)
        or not set(labels) <= known
    ):
        raise ApiError(400, tr("Nhãn chưa được khai báo trong cài đặt hộp thư"))
    _hub(request).inbox.set_labels(conv.id, labels)
    return await inbox_get(request)


async def inbox_summary(request: web.Request) -> web.Response:
    conv = _inbox_conv(request)
    try:
        text = await _hub(request).summarize_thread(conv.id)
    except KeyError:
        raise ApiError(404, "no employee for this conversation") from None
    except Exception as e:  # noqa: BLE001 - no model reachable
        raise ApiError(502, tr("AI chưa tóm tắt được: {0}", e)) from None
    return _json({"text": text})


async def inbox_meta(request: web.Request) -> web.Response:
    """What the inbox needs to show and edit conversations: labels, saved replies, teams, staff."""
    office = request.app[OFFICE]
    desk = office.hub.desk.get()
    users = [
        {"username": u["username"], "name": u["name"]} for u in request.app[USERS].list() if not u["disabled"]
    ]
    return _json(
        {
            **desk,
            "users": users,
            "me": _user(request).username,
            "my_teams": office.hub.desk.teams_of(_user(request).username),
        }
    )


async def inbox_sla(request: web.Request) -> web.Response:
    """Answer times and who is waiting (admins: it counts every channel)."""
    if not _user(request).is_admin:
        raise ApiError(403, tr("Chỉ quản trị viên xem được báo cáo SLA"))
    from datetime import datetime, timedelta

    from .state import now_iso

    office = request.app[OFFICE]
    try:
        hours = min(max(float(request.query.get("hours", "24")), 1), 24 * 90)
    except ValueError:
        raise ApiError(400, tr("hours phải là số")) from None
    since = (datetime.now().astimezone() - timedelta(hours=hours)).isoformat(timespec="seconds")
    target = office.hub.desk.sla_seconds()
    report = office.hub.inbox.sla(since, now_iso(), target)
    report["oldest_waiting"] = [_conv_json(office, c) for c in report["oldest_waiting"]]
    names = {u["username"]: u["name"] for u in request.app[USERS].list()}
    teams = {t["id"]: t["name"] for t in office.hub.desk.teams}
    for w in report["workload"]:
        w["assignee_name"] = names.get(w["assignee"], w["assignee"])
        w["team_name"] = teams.get(w["team"], w["team"])
    return _json({**report, "hours": hours, "target_seconds": target})


async def desk_save(request: web.Request) -> web.Response:
    """Admins: replace one section of the inbox settings (labels, canned, teams, rules, sla_minutes)."""
    office = request.app[OFFICE]
    hub = office.hub
    section = request.match_info["section"]
    value = (await _body(request)).get("value")
    usernames = {u["username"] for u in request.app[USERS].list()}
    before = {lb["name"] for lb in hub.desk.labels}
    renames = {}
    if isinstance(value, list) and not all(isinstance(x, dict) for x in value):
        raise ApiError(400, tr("Mỗi mục phải là một đối tượng JSON"))
    if section == "labels" and isinstance(value, list):
        # {"name": new, "was": old}: a renamed label follows its conversations
        renames = {
            x["was"]: x.get("name") for x in value if isinstance(x.get("was"), str) and x["was"] in before
        }
    try:
        desk = hub.desk.save(section, value, usernames, _channel_ids(office))
    except ValueError as e:
        raise ApiError(400, str(e)) from None
    if section == "labels":
        now = {lb["name"] for lb in desk["labels"]}
        for old in before - now:
            hub.inbox.rename_label(old, renames.get(old) if renames.get(old) in now else None)
        rules = [
            {**r, "labels": [renames.get(x, x) for x in r["labels"] if renames.get(x, x) in now]}
            for r in desk["rules"]
        ]
        rules = [r for r in rules if r["labels"] or r["team"] or r["assignee"] or r["handoff"]]
        if rules != desk["rules"]:
            desk = hub.desk.save("rules", rules, usernames, _channel_ids(office))
    log.info("admin UI: %s changed inbox %s", _user(request).username, section)
    return _json(desk)


# --------------------------------------------------------------------------- #
# Customers (CRM): one contact across channels, companies


def _crm(request: web.Request):
    return request.app[OFFICE].hub.crm


def _int(value: Any, what: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ApiError(400, tr("{0} phải là số", what)) from None


def _contact_json(request: web.Request, contact: dict[str, Any], full: bool = False) -> dict[str, Any]:
    office = request.app[OFFICE]
    crm = office.hub.crm
    out = {k: contact[k] for k in ("id", "name", "phone", "email", "company_id", "notes", "created")}
    out["vip"] = bool(contact.get("vip"))
    for extra in ("last_ts", "conversation_count"):
        if extra in contact:
            out[extra] = contact[extra]
    company = crm.company(int(contact["company_id"])) if contact.get("company_id") else None
    out["company"] = company["name"] if company else ""
    if full:
        out.update({k: contact.get(k) for k in ("address", "lat", "lng")})
        user = _user(request)
        convs = [office.hub.inbox.conversation(c) for c in crm.conversations(int(contact["id"]))]
        out["conversations"] = [_conv_json(office, c) for c in convs if c and user.sees(c.channel)]
        # other customers' details only for admins (agents may be limited to some channels)
        out["duplicates"] = crm.duplicates(int(contact["id"])) if user.is_admin else []
    return out


async def crm_contacts(request: web.Request) -> web.Response:
    q = request.query
    company = _int(q["company"], "company") if q.get("company") else None
    rows = _crm(request).search(q.get("q", ""), company)
    return _json(
        {"contacts": [_contact_json(request, r) for r in rows], "companies": _crm(request).companies()}
    )


def _crm_contact(request: web.Request) -> dict[str, Any]:
    contact = _crm(request).contact(_int(request.match_info["id"], "id"))
    if contact is None:
        raise ApiError(404, "no such contact")
    return contact


async def crm_contact_get(request: web.Request) -> web.Response:
    return _json({"contact": _contact_json(request, _crm_contact(request), full=True)})


async def crm_contact_patch(request: web.Request) -> web.Response:
    contact = _crm_contact(request)
    data = await _body(request)
    if not _user(request).is_admin:
        data.pop("vip", None)  # VIP status changes prices: only admins set it
    try:
        _crm(request).update(int(contact["id"]), **data)
    except (TypeError, ValueError) as e:
        raise ApiError(400, str(e)) from None
    return await crm_contact_get(request)


async def crm_contact_merge(request: web.Request) -> web.Response:
    if not _user(request).is_admin:
        raise ApiError(403, tr("Chỉ quản trị viên gộp hoặc tách khách"))
    contact = _crm_contact(request)
    other = _int((await _body(request)).get("other"), "other")
    try:
        _crm(request).merge(int(contact["id"]), other)
    except (KeyError, ValueError) as e:
        raise ApiError(400, tr("Không gộp được: {0}", e)) from None
    log.info("admin UI: %s merged customer %s into %s", _user(request).username, other, contact["id"])
    return await crm_contact_get(request)


async def crm_duplicates(request: web.Request) -> web.Response:
    return _json({"groups": _crm(request).duplicates()})


async def crm_companies(request: web.Request) -> web.Response:
    return _json({"companies": _crm(request).companies()})


async def crm_company_save(request: web.Request) -> web.Response:
    cid = request.match_info.get("id")
    try:
        _crm(request).save_company(_int(cid, "id") if cid else None, **(await _body(request)))
    except ValueError as e:
        raise ApiError(400, str(e)) from None
    except KeyError:
        raise ApiError(404, "no such company") from None
    return await crm_companies(request)


async def crm_company_delete(request: web.Request) -> web.Response:
    _crm(request).delete_company(_int(request.match_info["id"], "id"))
    return await crm_companies(request)


def _inbox_contact(request: web.Request) -> tuple[Any, dict[str, Any]]:
    conv = _inbox_conv(request)
    crm = _crm(request)
    contact = crm.contact_of(conv.id) or crm.observe(conv, "", request.app[OFFICE].hub.channel_type(conv))
    return conv, contact


async def inbox_contact(request: web.Request) -> web.Response:
    """The customer behind this conversation, and their other channels."""
    _conv, contact = _inbox_contact(request)
    names = [{"id": c["id"], "name": c["name"]} for c in _crm(request).companies()]
    return _json({"contact": _contact_json(request, contact, full=True), "companies": names})


async def inbox_contact_save(request: web.Request) -> web.Response:
    _conv, contact = _inbox_contact(request)
    data = await _body(request)
    try:
        # VIP status changes prices: only admins set it
        allowed = (
            "name",
            "phone",
            "email",
            "company_id",
            "notes",
            *(("vip",) if _user(request).is_admin else ()),
        )
        _crm(request).update(int(contact["id"]), **{k: v for k, v in data.items() if k in allowed})
    except ValueError as e:
        raise ApiError(400, str(e)) from None
    return await inbox_contact(request)


async def inbox_contact_merge(request: web.Request) -> web.Response:
    """Admins: join another contact into this conversation's customer, or split it off."""
    if not _user(request).is_admin:
        raise ApiError(403, tr("Chỉ quản trị viên gộp hoặc tách khách"))
    conv, contact = _inbox_contact(request)
    data = await _body(request)
    try:
        if data.get("split"):
            _crm(request).split(conv)
        else:
            _crm(request).merge(int(contact["id"]), _int(data.get("other"), "other"))
    except (KeyError, ValueError) as e:
        raise ApiError(400, tr("Không gộp được: {0}", e)) from None
    return await inbox_contact(request)


# --------------------------------------------------------------------------- #
# Inventory: products, warehouses, purchasing, pricing, orders (see inventory.py)


def _inv(request: web.Request):
    return request.app[OFFICE].inventory


def _id(request: web.Request, key: str = "id") -> int:
    return _int(request.match_info[key], key)


async def _inv_call(fn: Any, *args: Any, **kwargs: Any) -> web.Response:
    from .inventory import InventoryError

    try:
        return _json(fn(*args, **kwargs))
    except InventoryError as e:
        raise ApiError(400, str(e)) from None


async def inv_overview(request: web.Request) -> web.Response:
    inv = _inv(request)
    return _json(
        {
            "settings": inv.settings(),
            "warehouses": inv.warehouses(),
            "suppliers": inv.suppliers(),
            "today": inv.summary(1),
            "month": inv.summary(30),
        }
    )


async def inv_settings(request: web.Request) -> web.Response:
    return await _inv_call(_inv(request).save_settings, await _body(request))


async def inv_warehouse_save(request: web.Request) -> web.Response:
    wid = _id(request) if "id" in request.match_info else None
    return await _inv_call(_inv(request).save_warehouse, wid, await _body(request))


async def inv_supplier_save(request: web.Request) -> web.Response:
    sid = _id(request) if "id" in request.match_info else None
    return await _inv_call(_inv(request).save_supplier, sid, await _body(request))


async def inv_products(request: web.Request) -> web.Response:
    q = request.query
    rows = _inv(request).products(q.get("q", ""), include_inactive=q.get("all") == "1")
    if level := q.get("level"):
        rows = [r for r in rows if r["level"] == level]
    return _json({"products": rows})


async def inv_product_get(request: web.Request) -> web.Response:
    return await _inv_call(_inv(request).product, _id(request))


async def inv_product_save(request: web.Request) -> web.Response:
    pid = _id(request) if "id" in request.match_info else None
    inv = _inv(request)
    data = await _body(request)
    from .inventory import InventoryError

    try:
        row = inv.save_product(pid, data)
        return _json(inv.product(int(row["id"])))
    except InventoryError as e:
        raise ApiError(400, str(e)) from None


async def inv_product_stage(request: web.Request) -> web.Response:
    data = await _body(request)
    return await _inv_call(
        _inv(request).set_stage,
        _id(request),
        _int(data.get("stage"), "stage"),
        _user(request).name,
        str(data.get("reason", "")),
    )


async def inv_product_opening(request: web.Request) -> web.Response:
    d = await _body(request)
    return await _inv_call(
        _inv(request).add_opening_stock,
        _id(request),
        _int(d.get("warehouse_id"), "warehouse_id"),
        d.get("qty"),
        d.get("unit_cost"),
        d.get("margin_pct"),
        d.get("price1"),
        _user(request).name,
    )


async def inv_product_adjust(request: web.Request) -> web.Response:
    d = await _body(request)
    return await _inv_call(
        _inv(request).adjust,
        _id(request),
        _int(d.get("warehouse_id"), "warehouse_id"),
        d.get("counted"),
        str(d.get("reason", "")),
        _user(request).name,
        d.get("unit_cost"),
    )


async def inv_lot_prices(request: web.Request) -> web.Response:
    d = await _body(request)
    return await _inv_call(
        _inv(request).set_lot_prices, _id(request), d.get("prices"), d.get("vip_price"), _user(request).name
    )


async def inv_export(request: web.Request) -> web.Response:
    return web.Response(
        text=_inv(request).export_csv(),
        content_type="text/csv",
        charset="utf-8",
        headers={"Content-Disposition": 'attachment; filename="san-pham.csv"'},
    )


async def inv_import(request: web.Request) -> web.Response:
    """A CSV file as the request body (up to 5 MB: bigger than the JSON limit)."""
    data = bytearray()
    while chunk := await request.content.read(65536):
        data.extend(chunk)
        if len(data) > 5 * 1024 * 1024:
            raise ApiError(413, tr("Tệp quá lớn (tối đa 5 MB)"))
    try:
        text = bytes(data).decode("utf-8")
    except UnicodeDecodeError:
        raise ApiError(400, tr("Tệp phải là CSV UTF-8 (trong Excel: Lưu thành CSV UTF-8)")) from None
    return await _inv_call(_inv(request).import_csv, text)


async def inv_calc(request: web.Request) -> web.Response:
    """Preview: landed costs of a container and the stage prices, before saving anything."""
    from .inventory import InventoryError

    inv = _inv(request)
    d = await _body(request)
    try:
        items = d.get("items") or []
        costs = inv.landed_costs(
            items, d.get("exchange_rate", "1"), inv.minor(d.get("freight", 0)), inv.minor(d.get("customs", 0))
        )
        out = []
        for it, c in zip(items, costs, strict=True):
            plan = inv.price_plan(c["landed_cost"], it.get("margin_pct"), it.get("price1"))
            out.append(
                {
                    **{k: inv.major(v) for k, v in c.items()},
                    **plan,
                    "landed_cost": inv.major(c["landed_cost"]),
                    "prices": [inv.major(p) for p in plan["prices"]],
                    "profit": inv.major(plan["profit"]),
                }
            )
    except InventoryError as e:
        raise ApiError(400, str(e)) from None
    return _json({"items": out})


async def inv_pos(request: web.Request) -> web.Response:
    return _json({"purchase_orders": _inv(request).pos(request.query.get("status") or None)})


async def inv_po_get(request: web.Request) -> web.Response:
    return await _inv_call(_inv(request).po, _id(request))


async def inv_po_save(request: web.Request) -> web.Response:
    pid = _id(request) if "id" in request.match_info else None
    return await _inv_call(_inv(request).save_po, pid, await _body(request), _user(request).name)


async def inv_po_status(request: web.Request) -> web.Response:
    return await _inv_call(
        _inv(request).set_po_status, _id(request), str((await _body(request)).get("status", ""))
    )


async def inv_po_receive(request: web.Request) -> web.Response:
    d = await _body(request)
    return await _inv_call(
        _inv(request).receive_po,
        _id(request),
        d.get("items") or [],
        _user(request).name,
        d.get("date") or None,
    )


async def inv_transfers(request: web.Request) -> web.Response:
    return _json({"transfers": _inv(request).transfers()})


async def inv_transfer_create(request: web.Request) -> web.Response:
    d = await _body(request)
    return await _inv_call(
        _inv(request).create_transfer,
        _int(d.get("from_wh"), "from_wh"),
        _int(d.get("to_wh"), "to_wh"),
        d.get("items") or [],
        str(d.get("note", "")),
        _user(request).name,
    )


async def inv_transfer_step(request: web.Request) -> web.Response:
    inv, tid, step = _inv(request), _id(request), request.match_info["step"]
    d = await _body(request)
    if step == "ship":
        return await _inv_call(inv.ship_transfer, tid, _user(request).name)
    if step == "receive":
        received = d.get("received") or {}
        if not isinstance(received, dict):
            raise ApiError(400, tr("received phải là một đối tượng JSON"))
        got = {_int(k, "received"): v for k, v in received.items()} or None
        return await _inv_call(inv.receive_transfer, tid, got, _user(request).name)
    if step == "cancel":
        return await _inv_call(inv.cancel_transfer, tid, _user(request).name)
    raise ApiError(404, "unknown step")


async def inv_orders(request: web.Request) -> web.Response:
    return _json({"orders": _inv(request).orders(request.query.get("status") or None)})


async def inv_order_get(request: web.Request) -> web.Response:
    return await _inv_call(_inv(request).order, _id(request))


async def inv_order_create(request: web.Request) -> web.Response:
    from .inventory import InventoryError
    from .web_business import check_staff_discount

    d = await _body(request)
    crm = _crm(request)
    contact = crm.contact(_int(d["contact_id"], "contact_id")) if d.get("contact_id") else None
    try:
        check_staff_discount(
            request, d.get("items"), d.get("discount", 0), bool(contact and contact.get("vip"))
        )
    except InventoryError as e:
        raise ApiError(400, str(e)) from None
    return await _inv_call(
        _inv(request).create_order,
        d.get("items") or [],
        warehouse_id=d.get("warehouse_id") or None,
        kind=str(d.get("kind", "now")),
        contact_id=int(contact["id"]) if contact else None,
        customer_name=str(d.get("customer_name") or (contact or {}).get("name") or ""),
        phone=str(d.get("phone") or (contact or {}).get("phone") or ""),
        address=str(d.get("address", "")),
        discount=d.get("discount", 0),
        vip=bool(contact and contact.get("vip")),
        note=str(d.get("note", "")),
        source="admin",
        actor=_user(request).name,
    )


async def inv_order_step(request: web.Request) -> web.Response:
    inv, oid, step = _inv(request), _id(request), request.match_info["step"]
    if step == "complete":
        return await _inv_call(inv.complete_order, oid, _user(request).name)
    if step == "cancel":
        return await _inv_call(inv.cancel_order, oid, _user(request).name)
    raise ApiError(404, "unknown step")


async def inv_pricing_run(request: web.Request) -> web.Response:
    """ "Cập nhật giá ngay": the daily price run, now (or for one product)."""
    d = await _body(request)
    pid = _int(d["product_id"], "product_id") if d.get("product_id") else None
    changes = _inv(request).run_pricing(actor=_user(request).name, product_id=pid)
    log.info("admin UI: %s ran the price update (%d change(s))", _user(request).username, len(changes))
    return _json({"changes": changes})


async def inv_pricing_log(request: web.Request) -> web.Response:
    return _json({"log": _inv(request).price_log()})


async def inv_reorder(request: web.Request) -> web.Response:
    return _json({"products": _inv(request).reorder()})


async def inv_moves(request: web.Request) -> web.Response:
    pid = _int(request.query["product"], "product") if request.query.get("product") else None
    return _json({"moves": _inv(request).moves(pid)})


async def inbox_products(request: web.Request) -> web.Response:
    """For staff in the inbox: price (for this customer), stock and arrivals; no costs."""
    conv = _inbox_conv(request)
    office = request.app[OFFICE]
    contact = office.hub.crm.contact_of(conv.id)
    vip = bool(contact and contact.get("vip"))
    return _json(
        {
            "products": _inv(request).lookup(request.query.get("q", ""), vip=vip, limit=20),
            "vip": vip,
            "currency": _inv(request).settings()["currency"],
        }
    )


async def inbox_read(request: web.Request) -> web.Response:
    conv = _inbox_conv(request)
    _hub(request).inbox.mark_read(conv.id)
    return _json({"ok": True})


async def inbox_media(request: web.Request) -> web.Response:
    """An attachment of a stored message, fetched from the platform (see media.py)."""
    from urllib.parse import quote

    from .media import INLINE_TYPES, MediaError, fetch

    conv = _inbox_conv(request)
    m = _hub(request).inbox.message(conv.id, int(request.match_info["mid"]))
    n = int(request.match_info["n"])
    if m is None or not 0 <= n < len(m["attachments"]):
        raise ApiError(404, "no such attachment")
    a = m["attachments"][n]
    url = a.get("thumb") if request.query.get("thumb") and a.get("thumb") else a.get("url") or a.get("thumb")
    channel = _hub(request).channels.get(conv.channel)
    private = url.startswith("telegram:") if url else False
    if not url or not (private or url.startswith(("https://", "http://"))):
        raise ApiError(404, "no remote file for this attachment")
    try:
        # files only the channel can download (Telegram, a WAHA server), with its credentials
        got = await channel.fetch_media(url) if channel is not None else None
        if got is None and private:
            raise MediaError("channel unavailable")
        ctype, body = got or await fetch(request.app[OFFICE].http_client, url)
    except (MediaError, Exception) as e:  # noqa: BLE001 - expired links, platform errors
        raise ApiError(502, tr("Không tải được tệp: {0}", e)) from None
    cache = {"Cache-Control": "private, max-age=3600"}
    if ctype in INLINE_TYPES:
        return web.Response(body=body, content_type=ctype, headers=cache)
    name = a.get("name") or "tep-dinh-kem"
    return web.Response(
        body=body,
        content_type="application/octet-stream",
        headers={**cache, "Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"},
    )


async def inbox_memory(request: web.Request) -> web.Response:
    """Staff correct what the AI remembers about this customer: the summary and saved facts."""
    conv = _inbox_conv(request)
    e = request.app[OFFICE].employees.get(conv.employee)
    if e is None:
        raise ApiError(404, "no employee for this conversation")
    data = await _body(request)
    if isinstance(data.get("summary"), str):
        e.state.set_summary(conv.contact_id, data["summary"])
    notes = data.get("notes")
    if isinstance(notes, dict):
        for key, value in notes.items():
            if value is None:
                e.state.delete_note(conv.contact_id, str(key))
            elif isinstance(value, str) and str(key).strip() and value.strip():
                try:
                    e.state.set_note(conv.contact_id, str(key), value.strip())
                except ValueError as err:
                    raise ApiError(400, str(err)) from None
    return await inbox_get(request)


async def inbox_suggest(request: web.Request) -> web.Response:
    conv = _inbox_conv(request)
    data = await _body(request) if request.can_read_body else {}
    return _json(
        {"text": await _hub(request).suggest(conv.id, in_staff_language=bool(data.get("staff_language")))}
    )


async def inbox_translate(request: web.Request) -> web.Response:
    """A customer's message in the staff language (cached on the message)."""
    from .agent import TranslationError

    conv = _inbox_conv(request)
    try:
        text = await _hub(request).translate_message(conv.id, int(request.match_info["mid"]))
    except KeyError:
        raise ApiError(404, "no such message") from None
    except TranslationError as e:
        raise ApiError(502, tr("Không dịch được: {0}", e)) from None
    return _json({"translation": text})


async def inbox_language(request: web.Request) -> web.Response:
    """Staff set the customer's country or language (kept over detection); empty = detect again."""
    from . import lang

    conv = _inbox_conv(request)
    e = request.app[OFFICE].employees.get(conv.employee)
    if e is None:
        raise ApiError(404, "no employee for this conversation")
    data = await _body(request)
    country = str(data.get("country") or "").upper()
    code = str(data.get("lang") or "")
    if country:
        if country not in lang.COUNTRIES:
            raise ApiError(400, tr("Không có nước này trong danh sách"))
        code = code or lang.COUNTRIES[country][1]
    if code and code not in lang.LANGUAGES:
        raise ApiError(400, tr("Không hỗ trợ ngôn ngữ này"))
    e.state.set_language(conv.contact_id, code or None, country or None)
    return await inbox_get(request)


async def languages(request: web.Request) -> web.Response:
    from . import lang

    return _json(
        {
            # names in the staff member's language (tr: the tables are in Vietnamese)
            "languages": [{"code": c, "name": tr(v[1]), "native": v[2]} for c, v in lang.LANGUAGES.items()],
            "countries": [{"code": c, "name": tr(v[0]), "lang": v[1]} for c, v in lang.COUNTRIES.items()],
            "staff_language": request.app[OFFICE].config.staff_language,
        }
    )


async def channels_get(request: web.Request) -> web.Response:
    office = request.app[OFFICE]
    hub = office.hub
    stats = hub.inbox.stats()
    rows = []
    for e in office.employees.values():
        cid = f"simplex:{e.id}"
        rows.append(
            {
                "id": cid,
                **_channel_label(office, cid),
                "employee": e.id,
                "auto_reply": True,
                "stats": stats.get(cid, {}),
                "state": {},
            }
        )
    for c, ch in hub.channels.items():
        rows.append(
            {
                "id": c,
                **_channel_label(office, c),
                "employee": ch.cfg.employee,
                "auto_reply": ch.cfg.auto_reply,
                "poll_seconds": ch.cfg.poll_seconds,
                "stats": stats.get(c, {}),
                "state": hub.inbox.channel_state(c),
            }
        )
    user = _user(request)
    return _json({"channels": [r for r in rows if user.sees(r["id"])]})


async def channel_poll(request: web.Request) -> web.Response:
    hub = _hub(request)
    cid = request.match_info["channel"]
    if cid not in hub.channels:
        raise ApiError(404, "no such channel")
    return _json({"added": await hub.poll_once(cid), "state": hub.inbox.channel_state(cid)})


async def channel_webhook(request: web.Request) -> web.Response:
    """Telegram: point the bot at this server's /hooks/<channel id>."""
    ch = _hub(request).channels.get(request.match_info["channel"])
    if ch is None or not hasattr(ch, "register_webhook"):
        raise ApiError(404, "this channel has no webhook registration")
    if not ch.accepts_push():
        raise ApiError(400, "set the channel's secret first (Telegram sends it back on every update)")
    try:
        url = await ch.register_webhook()
    except Exception as e:  # noqa: BLE001 - wrong token, no public URL
        raise ApiError(502, str(e)) from None
    _hub(request).inbox.set_channel_state(ch.id, webhook=url)
    return _json({"ok": True, "url": url})


async def channel_login(request: web.Request) -> web.Response:
    """Zalo personal accounts: start the gateway login and return its QR (a data: URI)."""
    hub = _hub(request)
    ch = hub.channels.get(request.match_info["channel"])
    if ch is None or not hasattr(ch, "login"):
        raise ApiError(404, "this channel has no QR login")
    try:
        return _json(await ch.login())
    except Exception as e:  # noqa: BLE001 - gateway down or misconfigured
        raise ApiError(502, str(e)) from None


# --------------------------------------------------------------------------- #
# SimpleX accounts


def _qr(link: str) -> str:
    import segno

    return segno.make(link, error="m").svg_data_uri(scale=4, border=2)


async def simplex_get(request: web.Request) -> web.Response:
    office = request.app[OFFICE]
    rows = []
    for e in office.employees.values():
        address = getattr(e.bot, "address", None)
        rows.append(
            {
                "id": e.id,
                "name": e.settings.display_name,
                "address": address,
                "qr": _qr(address) if address else None,
                "contacts": len(e.state.contacts),
                "admins": len(e.state.admins),
            }
        )
    return _json({"accounts": rows})


async def simplex_invite(request: web.Request) -> web.Response:
    e = _employee(request)
    user = await e.bot.api.api_get_active_user()
    link = await e.bot.api.api_create_link(user["userId"])
    return _json({"link": link, "qr": _qr(link)})


async def simplex_connect(request: web.Request) -> web.Response:
    e = _employee(request)
    link = str((await _body(request)).get("link", "")).strip()
    if not link:
        raise ApiError(400, tr("Thiếu link"))
    try:
        kind = await e.bot.api.api_connect_active_user(link)
    except Exception as err:  # noqa: BLE001 - invalid or already-used links
        raise ApiError(400, tr("Không kết nối được: {0}", err)) from None
    return _json({"ok": True, "kind": kind})


# --------------------------------------------------------------------------- #
# Public webhook for "webhook" channels (server-to-server, authenticated by a shared secret)


def _push_channel(request: web.Request, body: bytes = b"") -> Any:
    """The channel, if this request proves it comes from its platform or bridge."""
    ch = request.app[OFFICE].hub.channels.get(request.match_info["channel"])
    if ch is None or not ch.accepts_push():
        raise ApiError(404, "no such channel")
    if not ch.verify_push(request.headers, body, request.query):
        log.warning("hooks: rejected a request for %s from %s (bad signature)", ch.id, request.remote)
        raise ApiError(401, "bad signature")
    return ch


async def hook_verify(request: web.Request) -> web.Response:
    """Meta checks a Messenger callback URL with a GET before sending events."""
    ch = request.app[OFFICE].hub.channels.get(request.match_info["channel"])
    challenge = ch.verify_subscription(request.query) if hasattr(ch, "verify_subscription") else None
    if challenge is None:
        raise ApiError(403, "verification failed")
    return web.Response(text=challenge, content_type="text/plain")


async def _form_fields(request: web.Request, limit: int = 2 * 1024 * 1024) -> dict[str, str]:
    """The text fields of a form post. File parts (email attachments, often several MB,
    over the app's request limit) are read past and dropped: only their names are kept."""
    if not request.content_type.startswith("multipart/"):
        if (request.content_length or 0) > limit:
            raise ApiError(413, "form too large")
        return {k: v for k, v in (await request.post()).items() if isinstance(v, str)}
    fields: dict[str, str] = {}
    total = 0
    reader = await request.multipart()
    while (part := await reader.next()) is not None:
        if not isinstance(part, BodyPartReader) or part.filename or not part.name:
            if isinstance(part, BodyPartReader):
                while await part.read_chunk():
                    pass
            continue
        value = bytearray()
        while chunk := await part.read_chunk():
            total += len(chunk)
            if total > limit:
                raise ApiError(413, "form too large")
            value.extend(chunk)
        fields[part.name] = part.decode(bytes(value)).decode(part.get_charset(default="utf-8"), "replace")
    return fields


async def hook_inbound(request: web.Request) -> web.Response:
    office = request.app[OFFICE]
    target = office.hub.channels.get(request.match_info["channel"])
    form = target is not None and target.push_format == "form"
    # form posts (inbound email) are authenticated by the URL key or basic auth, not the body
    body = b"" if form else await request.read()
    ch = _push_channel(request, body)
    # The Zalo gateway posts to {WEBHOOK_URL}/{account}: the account must be this channel's.
    if (account := request.match_info.get("account")) is not None and account != getattr(ch, "account", None):
        raise ApiError(404, "no such account")
    try:
        if form:
            payload = await _form_fields(request)
        else:
            payload = json.loads(body)
        if not isinstance(payload, dict):
            raise ApiError(400, "expected a JSON object")
        conv = office.hub.push_inbound(ch.id, payload)
    except ValueError as e:
        raise ApiError(400, str(e)) from None
    return _json({"ok": True, "conversation": conv.id if conv else None})


async def hook_messages(request: web.Request) -> web.Response:
    """Replies for a bridge that polls instead of receiving reply_url calls."""
    office = request.app[OFFICE]
    ch = _push_channel(request)
    if ch.type != "webhook":
        raise ApiError(404, "no such channel")
    conv = office.hub.inbox.find(ch.id, request.match_info["conversation"])
    try:
        after = int(request.query.get("after", "0"))
    except ValueError:
        raise ApiError(400, "after must be a message id") from None
    msgs = [m for m in office.hub.inbox.messages(conv.id, notes=False) if m["id"] > after] if conv else []
    return _json({"messages": msgs})


async def me_simplex(request: web.Request) -> web.Response:
    """This staff member's SimpleX chats linked to their account, and where to find the
    AI employees in SimpleX."""
    office = request.app[OFFICE]
    return _json(
        {
            "links": office.staff_links.of_user(_user(request).username),
            "employees": [
                {"id": e.id, "name": e.base.display_name, "address": getattr(e.bot, "address", None) or ""}
                for e in office.employees.values()
            ],
        }
    )


async def me_simplex_code(request: web.Request) -> web.Response:
    from .staff_chat import CODE_MINUTES

    code = request.app[OFFICE].staff_links.new_code(_user(request).username)
    return _json({"code": code, "minutes": CODE_MINUTES})


async def me_simplex_remove(request: web.Request) -> web.Response:
    office = request.app[OFFICE]
    key = request.match_info["key"]
    if not office.staff_links.remove(_user(request).username, key):
        raise ApiError(404, "no such link")
    employee_id, _, cid = key.partition(":")
    employee = office.employees.get(employee_id)
    if employee is not None and cid.isdigit():
        office.hub.spawn(employee.staff.sync_menu(int(cid)))
    return _json({"ok": True})


def create_app(office: Office, password: str) -> web.Application:
    app = web.Application(middlewares=[guard], client_max_size=256 * 1024)
    app[OFFICE] = office
    app[PASSWORD] = password
    app[LOGIN_FAILURES] = {}
    app[SESSIONS] = Sessions(office.office_db)
    app[USERS] = Users(office.docs, password, legacy_path=os.path.join(office.config.state_dir, "users.json"))
    r = app.router
    r.add_get("/", page)
    r.add_get("/static/{file}", page)
    r.add_get("/sw.js", page)
    r.add_get("/manifest.webmanifest", page)
    r.add_get("/favicon.ico", no_content)
    r.add_post("/api/login", login)
    r.add_post("/api/logout", logout)
    r.add_get("/api/me", me)
    r.add_put("/api/me/lang", me_language)
    r.add_get("/i18n/catalog.js", i18n_catalog)
    r.add_post("/api/me/password", me_password)
    r.add_get("/api/me/simplex", me_simplex)
    r.add_post("/api/me/simplex", me_simplex_code)
    r.add_delete("/api/me/simplex/{key}", me_simplex_remove)
    r.add_get("/api/users", users_list)
    r.add_post("/api/users", users_add)
    r.add_patch("/api/users/{username}", users_patch)
    r.add_delete("/api/users/{username}", users_delete)
    r.add_get("/api/overview", overview)
    r.add_get("/api/employees/{emp}", employee_get)
    r.add_patch("/api/employees/{emp}", employee_patch)
    r.add_post("/api/employees/{emp}/reset", employee_reset)
    r.add_post("/api/employees/{emp}/corrections", correction_add)
    r.add_post("/api/employees/{emp}/memory", memory_add)
    r.add_post("/api/employees/{emp}/memory/{mid}/approve", memory_approve)
    r.add_delete("/api/employees/{emp}/memory/{mid}", memory_delete)
    r.add_delete("/api/employees/{emp}/corrections/{n}", correction_delete)
    r.add_delete("/api/employees/{emp}/admins/{cid}", admin_remove)
    r.add_post("/api/employees/{emp}/routines/{rid}/run", routine_run)
    r.add_post("/api/employees/{emp}/routines/{rid}/pause", routine_pause)
    r.add_get("/api/employees/{emp}/conversations", conversations)
    r.add_get("/api/employees/{emp}/conversations/{cid}", conversation_get)
    r.add_delete("/api/employees/{emp}/conversations/{cid}", conversation_forget)
    r.add_get("/api/approvals", approvals)
    r.add_post("/api/approvals/{emp}/{n}/{decision}", approval_decide)
    r.add_get("/api/models", models_get)
    r.add_post("/api/models", model_add)
    r.add_delete("/api/models/{name}", model_delete)
    r.add_post("/api/models/{name}/test", model_test)
    r.add_get("/api/runlog", runlog)
    r.add_get("/api/inbox", inbox_list)
    r.add_get("/api/inbox/languages", languages)  # before {cid}
    r.add_get("/api/inbox/meta", inbox_meta)
    r.add_get("/api/inbox/sla", inbox_sla)
    r.add_put("/api/desk/{section}", desk_save)
    r.add_get("/api/inbox/{cid}", inbox_get)
    r.add_post("/api/inbox/{cid}/reply", inbox_reply)
    r.add_post("/api/inbox/{cid}/mode", inbox_mode)
    r.add_post("/api/inbox/{cid}/assign", inbox_assign)
    r.add_post("/api/inbox/{cid}/read", inbox_read)
    r.add_post("/api/inbox/{cid}/note", inbox_note)
    r.add_post("/api/inbox/{cid}/status", inbox_status)
    r.add_post("/api/inbox/{cid}/assignee", inbox_assignee)
    r.add_post("/api/inbox/{cid}/labels", inbox_labels)
    r.add_post("/api/inbox/{cid}/summary", inbox_summary)
    from .web_business import add_routes

    add_routes(r)
    r.add_get("/api/inbox/{cid}/products", inbox_products)
    r.add_get("/api/inventory", inv_overview)
    r.add_put("/api/inventory/settings", inv_settings)
    r.add_post("/api/inventory/warehouses", inv_warehouse_save)
    r.add_patch("/api/inventory/warehouses/{id}", inv_warehouse_save)
    r.add_post("/api/inventory/suppliers", inv_supplier_save)
    r.add_patch("/api/inventory/suppliers/{id}", inv_supplier_save)
    r.add_get("/api/inventory/products", inv_products)
    r.add_get("/api/inventory/products.csv", inv_export)
    r.add_post("/api/inventory/products/import", inv_import)
    r.add_post("/api/inventory/products", inv_product_save)
    r.add_get(r"/api/inventory/products/{id:\d+}", inv_product_get)
    r.add_patch(r"/api/inventory/products/{id:\d+}", inv_product_save)
    r.add_post(r"/api/inventory/products/{id:\d+}/stage", inv_product_stage)
    r.add_post(r"/api/inventory/products/{id:\d+}/opening", inv_product_opening)
    r.add_post(r"/api/inventory/products/{id:\d+}/adjust", inv_product_adjust)
    r.add_put(r"/api/inventory/lots/{id:\d+}/prices", inv_lot_prices)
    r.add_post("/api/inventory/calc", inv_calc)
    r.add_get("/api/inventory/purchase-orders", inv_pos)
    r.add_post("/api/inventory/purchase-orders", inv_po_save)
    r.add_get(r"/api/inventory/purchase-orders/{id:\d+}", inv_po_get)
    r.add_put(r"/api/inventory/purchase-orders/{id:\d+}", inv_po_save)
    r.add_post(r"/api/inventory/purchase-orders/{id:\d+}/status", inv_po_status)
    r.add_post(r"/api/inventory/purchase-orders/{id:\d+}/receive", inv_po_receive)
    r.add_get("/api/inventory/transfers", inv_transfers)
    r.add_post("/api/inventory/transfers", inv_transfer_create)
    r.add_post(r"/api/inventory/transfers/{id:\d+}/{step}", inv_transfer_step)
    r.add_get("/api/inventory/orders", inv_orders)
    r.add_post("/api/inventory/orders", inv_order_create)
    r.add_get(r"/api/inventory/orders/{id:\d+}", inv_order_get)
    r.add_post(r"/api/inventory/orders/{id:\d+}/{step}", inv_order_step)
    r.add_post("/api/inventory/pricing/run", inv_pricing_run)
    r.add_get("/api/inventory/pricing/log", inv_pricing_log)
    r.add_get("/api/inventory/reorder", inv_reorder)
    r.add_get("/api/inventory/moves", inv_moves)
    r.add_get("/api/inbox/{cid}/contact", inbox_contact)
    r.add_post("/api/inbox/{cid}/contact", inbox_contact_save)
    r.add_post("/api/inbox/{cid}/contact/merge", inbox_contact_merge)
    r.add_get("/api/crm/contacts", crm_contacts)
    r.add_get("/api/crm/contacts/{id}", crm_contact_get)
    r.add_patch("/api/crm/contacts/{id}", crm_contact_patch)
    r.add_post("/api/crm/contacts/{id}/merge", crm_contact_merge)
    r.add_get("/api/crm/duplicates", crm_duplicates)
    r.add_get("/api/crm/companies", crm_companies)
    r.add_post("/api/crm/companies", crm_company_save)
    r.add_patch("/api/crm/companies/{id}", crm_company_save)
    r.add_delete("/api/crm/companies/{id}", crm_company_delete)
    r.add_post("/api/inbox/{cid}/suggest", inbox_suggest)
    r.add_post("/api/inbox/{cid}/memory", inbox_memory)
    r.add_post(r"/api/inbox/{cid:\d+}/messages/{mid:\d+}/translate", inbox_translate)
    r.add_post("/api/inbox/{cid}/language", inbox_language)
    r.add_get(r"/api/inbox/{cid:\d+}/media/{mid:\d+}/{n:\d+}", inbox_media)
    r.add_get("/api/channels", channels_get)
    r.add_post("/api/channels/{channel}/poll", channel_poll)
    r.add_get("/api/simplex", simplex_get)
    r.add_post("/api/simplex/{emp}/invite", simplex_invite)
    r.add_post("/api/simplex/{emp}/connect", simplex_connect)
    r.add_post("/api/channels/{channel}/login", channel_login)
    r.add_post("/api/channels/{channel}/webhook", channel_webhook)
    r.add_get("/hooks/{channel}", hook_verify)
    r.add_post("/hooks/{channel}", hook_inbound)
    r.add_post("/hooks/{channel}/{account}", hook_inbound)
    r.add_get("/hooks/{channel}/{conversation}", hook_messages)
    return app


async def start_admin_ui(office: Office, ui: AdminUIConfig) -> web.AppRunner:
    if not ui.password:
        raise ConfigError("admin_ui needs a password (password_env or password)")
    if len(ui.password) < 12:
        log.warning("admin UI: the password is short; use at least 12 characters")
    if os.environ.get("AI_ADMIN_UI_HOST"):
        log.info("admin UI host set by AI_ADMIN_UI_HOST (container); publish the port on 127.0.0.1 only")
    elif ui.host not in ("127.0.0.1", "localhost", "::1"):
        log.warning("admin UI listens on %s: put it behind a TLS reverse proxy", ui.host)
    runner = web.AppRunner(create_app(office, ui.password), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, ui.host, ui.port).start()
    log.info("admin UI: http://%s:%s", ui.host, ui.port)
    return runner

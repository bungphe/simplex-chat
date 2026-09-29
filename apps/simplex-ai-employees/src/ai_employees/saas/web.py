"""The control plane's web: public sign-up (/), tenant portal (/portal), operator console
(/console). Server-rendered pages, no inline scripts (CSP default-src 'self').

Security: forms carry a CSRF token (cookie + hidden field); JSON requests carry the
`X-Requested-With: ai-employees` header instead. Sessions are HttpOnly SameSite=Lax
cookies (Secure behind https). Sign-up, login and code checks are rate limited per address.
"""

from __future__ import annotations

import hmac
import html
import ipaddress
import json
import logging
import secrets
import time
from typing import Any
from urllib.parse import urlencode

from aiohttp import web

from .. import i18n
from ..i18n import LANGUAGES, RTL, number, tr
from .billing import PAYABLE, Billing
from .config import SaasConfig
from .service import SaasError, Service

log = logging.getLogger(__name__)

CSRF, TENANT, OPERATOR, LANG = "saas_csrf", "saas_t", "saas_op", "saas_lang"
CSRF_HEADER, CSRF_VALUE = "X-Requested-With", "ai-employees"
CSP = (
    "default-src 'self'; style-src 'self'; img-src 'self'; form-action 'self'; "
    "base-uri 'none'; frame-ancestors 'none'"
)
HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Cache-Control": "no-store",
}
# per address: (requests, seconds)
LIMITS = {"signup": (5, 3600), "verify": (10, 900), "login": (10, 900)}
STATUS_LABELS = {
    "pending_email": "Chờ xác nhận email",
    "trial": "Dùng thử",
    "active": "Đang hoạt động",
    "past_due": "Quá hạn thanh toán",
    "suspended": "Tạm dừng",
    "deleted": "Đã xoá",
}
INVOICE_LABELS = {
    "due": "Chờ thanh toán",
    "awaiting_confirmation": "Đã báo chuyển khoản",
    "paid": "Đã thanh toán",
    "void": "Đã huỷ",
}
CSS = """
body{font:16px/1.5 system-ui,sans-serif;margin:0;color:#1d2a35;background:#f6f8fa}
header{background:#12324a;color:#fff;padding:.8rem 1rem;display:flex;gap:1rem;align-items:center;flex-wrap:wrap}
header a{color:#fff;text-decoration:none}header .brand{font-weight:700;font-size:1.2rem}
main{max-width:64rem;margin:1.5rem auto;padding:0 1rem}footer{text-align:center;color:#667;padding:2rem 1rem}
footer a{margin:0 .3rem;color:#667}footer a.on{font-weight:700}
h1{font-size:1.6rem}h2{font-size:1.2rem;margin-top:2rem}table{border-collapse:collapse;width:100%;background:#fff}
th,td{border:1px solid #dde3e8;padding:.45rem .6rem;text-align:left;vertical-align:top}th{background:#eef2f5}
form.card,div.card{background:#fff;border:1px solid #dde3e8;border-radius:8px;padding:1rem;margin:1rem 0}
label{display:block;margin:.6rem 0 .2rem;font-weight:600}input,select{width:100%;box-sizing:border-box;padding:.5rem;border:1px solid #b9c3cc;border-radius:6px;font:inherit}
button{background:#1f6f8b;color:#fff;border:0;border-radius:6px;padding:.55rem 1rem;font:inherit;cursor:pointer;margin-top:.6rem}
button.danger{background:#a83232}form.inline{display:inline}form.inline button{margin:0 .2rem 0 0;padding:.3rem .6rem}
.msg{padding:.7rem 1rem;border-radius:6px;margin:1rem 0}.err{background:#fde8e8;color:#8a1f1f}.ok{background:#e6f6ec;color:#155d2c}
.secret{font-family:ui-monospace,monospace;font-size:1.2rem;background:#fff7d6;padding:.3rem .6rem;border-radius:4px}
.plans{display:grid;grid-template-columns:repeat(auto-fit,minmax(14rem,1fr));gap:1rem}.plans .card{margin:0}
.price{font-size:1.4rem;font-weight:700}.muted{color:#667}
"""

CFG: web.AppKey[SaasConfig] = web.AppKey("cfg")
SVC: web.AppKey[Service] = web.AppKey("svc")
BILL: web.AppKey[Billing] = web.AppKey("bill")
RATE: web.AppKey[dict[str, list[float]]] = web.AppKey("rate")
NEW_CSRF: web.RequestKey[str] = web.RequestKey("new_csrf")
IS_JSON: web.RequestKey[bool] = web.RequestKey("is_json")
FLASH: web.RequestKey[tuple[str, str] | None] = web.RequestKey("flash")


# --------------------------------------------------------------------------- #
# Plumbing


def _client_ip(request: web.Request) -> str:
    ip = request.remote or ""
    forwarded = request.headers.get("X-Forwarded-For", "")
    try:
        behind_proxy = ipaddress.ip_address(ip).is_private or ipaddress.ip_address(ip).is_loopback
    except ValueError:
        behind_proxy = False
    return forwarded.split(",")[-1].strip() if forwarded and behind_proxy else ip


def _secure(request: web.Request) -> bool:
    return request.secure or request.app[CFG].https


def _cookie(request: web.Request, resp: web.StreamResponse, name: str, value: str, seconds: int) -> None:
    resp.set_cookie(
        name, value, max_age=seconds, httponly=True, samesite="Lax", secure=_secure(request), path="/"
    )


def _rate(request: web.Request, bucket: str) -> None:
    limit, window = LIMITS[bucket]
    key = f"{bucket}:{_client_ip(request)}"
    now = time.monotonic()
    table = request.app[RATE]
    for k in [k for k, v in table.items() if not v or v[-1] < now - 3600]:
        del table[k]
    hits = [t for t in table.get(key, []) if t >= now - window]
    if len(hits) >= limit:
        raise SaasError(tr("Quá nhiều yêu cầu; vui lòng thử lại sau {0} phút", window // 60), 429)
    hits.append(now)
    table[key] = hits


async def _input(request: web.Request) -> dict[str, Any]:
    """A form (with the CSRF token) or a JSON body (with the CSRF header)."""
    if request.content_type == "application/json":
        request[IS_JSON] = True
        if request.headers.get(CSRF_HEADER) != CSRF_VALUE:
            raise SaasError("missing request header", 403)
        try:
            data = await request.json()
        except json.JSONDecodeError:
            raise SaasError("invalid JSON", 400) from None
        if not isinstance(data, dict):
            raise SaasError("expected a JSON object", 400)
        return {
            k: (v if isinstance(v, str) else json.dumps(v) if v is not None else "")[:500]
            for k, v in data.items()
        }
    data = await request.post()
    token = request.cookies.get(CSRF, "")
    if not token or not hmac.compare_digest(token, str(data.get("csrf", ""))):
        raise SaasError(tr("Phiên làm việc đã hết hạn, vui lòng tải lại trang."), 403)
    return {k: str(v)[:500] for k, v in data.items()}


def _reply(request: web.Request, location: str, data: dict[str, Any] | None = None) -> web.Response:
    if request.get(IS_JSON):
        return web.json_response(
            {"ok": True, **(data or {})}, dumps=lambda d: json.dumps(d, ensure_ascii=False)
        )
    raise web.HTTPSeeOther(location)


def _flash(url: str, kind: str, text: str) -> str:
    return f"{url}{'&' if '?' in url else '?'}{urlencode({kind: text})}"


@web.middleware
async def middleware(request: web.Request, handler: Any) -> web.StreamResponse:
    if not request.cookies.get(CSRF):
        request[NEW_CSRF] = secrets.token_urlsafe(24)
    chosen = i18n.normalize(request.query.get("lang"))
    lang = (
        chosen
        or i18n.normalize(request.cookies.get(LANG))
        or i18n.best_match(request.headers.get("Accept-Language"))
        or "vi"
    )
    with i18n.use_language(lang):
        try:
            resp = await handler(request)
        except web.HTTPException as exc:
            resp = exc
        except SaasError as e:
            if request.get(IS_JSON):
                resp = web.json_response({"error": str(e)}, status=e.status)
            else:
                resp = _page(
                    request, tr("Lỗi"), f'<p class="msg err">{html.escape(str(e))}</p>', status=e.status
                )
        except Exception:
            log.exception("saas: %s %s failed", request.method, request.path)
            resp = _page(
                request,
                tr("Lỗi"),
                f'<p class="msg err">{tr("Lỗi hệ thống, vui lòng thử lại sau")}</p>',
                status=500,
            )
    if chosen:
        _cookie(request, resp, LANG, chosen, 365 * 86400)
    for k, v in HEADERS.items():
        resp.headers.setdefault(k, v)
    resp.headers.setdefault("Content-Security-Policy", CSP)
    if request.get(NEW_CSRF):
        _cookie(request, resp, CSRF, request[NEW_CSRF], 86400)
    if isinstance(resp, web.HTTPException):
        raise resp
    return resp


def _hidden(request: web.Request) -> str:
    token = request.cookies.get(CSRF) or request.get(NEW_CSRF) or ""
    return f'<input type="hidden" name="csrf" value="{html.escape(token)}">'


def _page(request: web.Request, title: str, body: str, status: int = 200) -> web.Response:
    e = html.escape
    cfg = request.app[CFG]
    lang = i18n.current()
    nav = f'<a href="/">{e(tr("Trang chủ"))}</a><a href="/portal">{e(tr("Cổng khách hàng"))}</a>'
    if request.path.startswith("/console"):
        nav += f'<a href="/console">{e(tr("Bảng điều hành"))}</a>'
    langs = "".join(
        f'<a href="?lang={c}"{" class=on" if c == lang else ""} lang="{c}">{e(n)}</a>'
        for c, n in LANGUAGES.items()
    )
    notices = ""
    for kind in ("ok", "err"):
        if m := request.query.get(kind):
            notices += f'<p class="msg {kind}">{e(m[:300])}</p>'
    doc = (
        f'<!doctype html><html lang="{lang}"{" dir=rtl" if lang in RTL else ""}><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{e(title)} – {e(cfg.base_domain)}</title>"
        '<link rel="stylesheet" href="/static/saas.css"></head><body>'
        f'<header><a class="brand" href="/">{e(tr("Nhân viên AI"))} · {e(cfg.base_domain)}</a><nav>{nav}</nav></header>'
        f'<main>{notices}{body}</main><footer><div class="langs">{langs}</div></footer></body></html>'
    )
    return web.Response(text=doc, content_type="text/html", status=status)


def _tenant(request: web.Request) -> dict[str, Any]:
    svc = request.app[SVC]
    tid = svc.store.session("tenant", request.cookies.get(TENANT, ""))
    tenant = svc.store.tenant(int(tid)) if tid else None
    if tenant is None or tenant["status"] in ("deleted", "pending_email"):
        raise web.HTTPSeeOther("/portal/login")
    return tenant


def _operator(request: web.Request) -> str:
    who = request.app[SVC].store.session("operator", request.cookies.get(OPERATOR, ""))
    if not who:
        if request.get(IS_JSON) or request.content_type == "application/json":
            raise SaasError("not logged in", 401)
        raise web.HTTPSeeOther("/console/login")
    return who


def _money(amount: Any, currency: str) -> str:
    return f"{number(int(amount))} {currency}"


def _days_left(until: str | None, today: Any) -> int:
    from datetime import date

    return (date.fromisoformat(until) - today).days if until else 0


# --------------------------------------------------------------------------- #
# Public site


async def css(request: web.Request) -> web.Response:
    return web.Response(text=CSS, content_type="text/css", charset="utf-8")


async def health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def caddy_ask(request: web.Request) -> web.Response:
    """Caddy's on_demand_tls `ask` endpoint: 200 for a live tenant's host names, else 404."""
    cfg, svc = request.app[CFG], request.app[SVC]
    host = request.query.get("domain", "").lower().rstrip(".")
    slug, dot, domain = host.partition(".")
    if dot and domain == cfg.base_domain:
        tenant = svc.store.tenant_by("slug", slug.removesuffix("-shop"))
        if tenant and tenant["status"] not in ("pending_email", "deleted"):
            return web.Response(text="ok")
    raise web.HTTPNotFound()


def _plans_html(cfg: SaasConfig) -> str:
    e = html.escape
    cards = []
    for p in cfg.plans.values():
        lim = p.limits
        rows = [
            tr("{0} tài khoản nhân viên", lim.get("users") or "∞"),
            tr("{0} nhân viên AI", lim.get("employees") or "∞"),
            tr("{0} kênh chat", lim.get("channels") or "∞"),
            tr("{0} MB dữ liệu", lim.get("storage_mb") or "∞"),
            *p.features,
        ]
        price = tr("Miễn phí") if p.price_month <= 0 else tr("{0}/tháng", _money(p.price_month, cfg.currency))
        cards.append(
            f'<div class="card"><h3>{e(p.name)}</h3><div class="price">{e(price)}</div><ul>'
            + "".join(f"<li>{e(r)}</li>" for r in rows)
            + "</ul></div>"
        )
    return f'<div class="plans">{"".join(cards)}</div>'


async def landing(request: web.Request) -> web.Response:
    cfg = request.app[CFG]
    e = html.escape
    lang = i18n.current()
    options = "".join(
        f'<option value="{pid}"{" selected" if pid == cfg.default_plan else ""}>{e(p.name)}</option>'
        for pid, p in cfg.plans.items()
    )
    lang_opts = "".join(
        f'<option value="{c}"{" selected" if c == lang else ""}>{e(n)}</option>' for c, n in LANGUAGES.items()
    )
    body = (
        f"<h1>{e(tr('Nhân viên AI cho cửa hàng của bạn'))}</h1>"
        f"<p>{e(tr('Tư vấn bán hàng tự động trên SimpleX, Zalo, Facebook, Telegram, WhatsApp và website; kho hàng, điểm bán, giao hàng và báo cáo trong một trang quản trị. Mỗi cửa hàng chạy riêng biệt trên tên miền của mình.'))}</p>"
        f"<h2>{e(tr('Bảng giá'))}</h2>{_plans_html(cfg)}"
        f"<h2 id=signup>{e(tr('Dùng thử miễn phí {0} ngày', cfg.trial_days))}</h2>"
        f'<form method="post" action="/signup" class="card">{_hidden(request)}'
        f'<label>{e(tr("Tên cửa hàng"))}</label><input name="shop_name" required maxlength="80">'
        f'<label>{e(tr("Tên của bạn"))}</label><input name="owner_name" required maxlength="80">'
        f'<label>Email</label><input name="email" type="email" required maxlength="200">'
        f'<label>{e(tr("Số điện thoại"))}</label><input name="phone" maxlength="20">'
        f'<label>{e(tr("Mật khẩu (ít nhất {0} ký tự)", 10))}</label><input name="password" type="password" minlength="10" required>'
        f'<label>{e(tr("Tên miền con"))} (.{e(cfg.base_domain)})</label><input name="slug" pattern="[a-z0-9][a-z0-9-]{{2,30}}" placeholder="{e(tr("để trống: tự đặt theo tên cửa hàng"))}">'
        f'<label>{e(tr("Gói dịch vụ"))}</label><select name="plan">{options}</select>'
        f'<label>{e(tr("Ngôn ngữ"))}</label><select name="lang">{lang_opts}</select>'
        f"<button>{e(tr('Đăng ký dùng thử'))}</button></form>"
    )
    return _page(request, tr("Nhân viên AI cho cửa hàng"), body)


async def signup(request: web.Request) -> web.Response:
    data = await _input(request)
    _rate(request, "signup")
    tenant = await request.app[SVC].signup(data)
    url = "/verify?" + urlencode({"email": tenant["email"]})
    return _reply(request, url, {"email": tenant["email"], "slug": tenant["slug"]})


async def verify_page(request: web.Request) -> web.Response:
    e = html.escape
    email = request.query.get("email", "")[:200]
    body = (
        f"<h1>{e(tr('Xác nhận email'))}</h1><p>{e(tr('Chúng tôi đã gửi mã xác nhận 6 số đến email của bạn.'))}</p>"
        f'<form method="post" action="/verify" class="card">{_hidden(request)}'
        f'<label>Email</label><input name="email" type="email" value="{e(email)}" required>'
        f'<label>{e(tr("Mã xác nhận"))}</label><input name="code" inputmode="numeric" pattern="[0-9]{{6}}" required autocomplete="one-time-code">'
        f"<button>{e(tr('Xác nhận'))}</button></form>"
        f'<form method="post" action="/verify/resend" class="inline">{_hidden(request)}<input type="hidden" name="email" value="{e(email)}">'
        f"<button>{e(tr('Gửi lại mã'))}</button></form>"
    )
    return _page(request, tr("Xác nhận email"), body)


async def verify(request: web.Request) -> web.Response:
    data = await _input(request)
    _rate(request, "verify")
    svc = request.app[SVC]
    tenant = await svc.verify(data.get("email", ""), data.get("code", ""))
    resp = (
        _reply(request, "/portal", {"tenant": _public(request.app[CFG], tenant)})
        if request.get(IS_JSON)
        else web.HTTPSeeOther("/portal")
    )
    token = svc.store.add_session("tenant", str(tenant["id"]), request.app[CFG].session_hours * 3600)
    _cookie(request, resp, TENANT, token, request.app[CFG].session_hours * 3600)
    if isinstance(resp, web.HTTPException):
        raise resp
    return resp


async def verify_resend(request: web.Request) -> web.Response:
    data = await _input(request)
    _rate(request, "verify")
    await request.app[SVC].resend_code(data.get("email", ""))
    url = "/verify?" + urlencode(
        {"email": data.get("email", "")[:200], "ok": tr("Đã gửi lại mã (nếu email đang chờ xác nhận)")}
    )
    return _reply(request, url)


def _public(cfg: SaasConfig, t: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "id",
        "slug",
        "shop_name",
        "owner_name",
        "email",
        "phone",
        "plan",
        "status",
        "trial_ends",
        "paid_until",
        "created",
        "lang",
        "provision_state",
        "cancel_requested",
        "suspended_at",
    )
    return {
        **{k: t.get(k) for k in keys},
        "admin_url": cfg.admin_url(t["slug"]),
        "shop_url": cfg.shop_url(t["slug"]),
    }


# --------------------------------------------------------------------------- #
# Tenant portal


async def portal_login_page(request: web.Request) -> web.Response:
    e = html.escape
    body = (
        f"<h1>{e(tr('Cổng khách hàng'))}</h1>"
        f'<form method="post" action="/portal/login" class="card">{_hidden(request)}'
        f'<label>Email</label><input name="email" type="email" required>'
        f'<label>{e(tr("Mật khẩu"))}</label><input name="password" type="password" required>'
        f"<button>{e(tr('Đăng nhập'))}</button></form>"
    )
    return _page(request, tr("Đăng nhập"), body)


async def portal_login(request: web.Request) -> web.Response:
    data = await _input(request)
    _rate(request, "login")
    svc, cfg = request.app[SVC], request.app[CFG]
    tenant = svc.authenticate(data.get("email", ""), data.get("password", ""))
    if tenant is None:
        log.warning("portal: failed login from %s", _client_ip(request))
        raise SaasError(tr("Sai email hoặc mật khẩu"), 401)
    resp = (
        _reply(request, "/portal", {"tenant": _public(cfg, tenant)})
        if request.get(IS_JSON)
        else web.HTTPSeeOther("/portal")
    )
    _cookie(
        request,
        resp,
        TENANT,
        svc.store.add_session("tenant", str(tenant["id"]), cfg.session_hours * 3600),
        cfg.session_hours * 3600,
    )
    if isinstance(resp, web.HTTPException):
        raise resp
    return resp


async def portal_logout(request: web.Request) -> web.Response:
    await _input(request)
    request.app[SVC].store.drop_session(request.cookies.get(TENANT, ""))
    resp = web.HTTPSeeOther("/portal/login")
    resp.del_cookie(TENANT, path="/")
    raise resp


def _invoice_rows(request: web.Request, invoices: list[dict[str, Any]], console: bool) -> str:
    e = html.escape
    rows = []
    for inv in invoices:
        if console:
            action = ""
            if inv["status"] in PAYABLE:
                action = (
                    f'<form method="post" action="/console/invoices/{inv["id"]}/confirm" class="inline">{_hidden(request)}'
                    f'<input type="hidden" name="ref" value=""><button>{e(tr("Xác nhận đã thu"))}</button></form>'
                    f'<form method="post" action="/console/invoices/{inv["id"]}/void" class="inline">{_hidden(request)}'
                    f'<button class="danger">{e(tr("Huỷ"))}</button></form>'
                )
        else:
            action = (
                f'<a href="/portal/invoices/{inv["id"]}">{e(tr("Thanh toán"))}</a>'
                if inv["status"] in PAYABLE
                else ""
            )
        rows.append(
            f"<tr><td>#{inv['id']}</td><td>{e(inv['period_start'])} → {e(inv['period_end'])}</td>"
            f"<td>{e(_money(inv['amount'], inv['currency']))}</td><td>{e(inv['due'])}</td>"
            f"<td>{e(tr(INVOICE_LABELS.get(inv['status'], inv['status'])))}</td><td>{action}</td></tr>"
        )
    head = f"<tr><th>#</th><th>{e(tr('Kỳ'))}</th><th>{e(tr('Số tiền'))}</th><th>{e(tr('Hạn'))}</th><th>{e(tr('Trạng thái'))}</th><th></th></tr>"
    empty = f"<tr><td colspan=6 class=muted>{e(tr('Chưa có hoá đơn'))}</td></tr>"
    return f"<table>{head}{''.join(rows) or empty}</table>"


async def portal_home(request: web.Request) -> web.Response:
    t = _tenant(request)
    svc, cfg = request.app[SVC], request.app[CFG]
    e = html.escape
    if request.get(IS_JSON) or request.headers.get("Accept", "").startswith("application/json"):
        return web.json_response({"tenant": _public(cfg, t), "invoices": svc.store.invoices(t["id"])})
    plan = cfg.plan(t["plan"])
    body = f"<h1>{e(t['shop_name'])}</h1>"
    if pw := svc.reveal(t["id"]):
        body += (
            f'<div class="card"><p><b>{e(tr("Mật khẩu quản trị (chỉ hiển thị một lần, hãy lưu lại):"))}</b> '
            f'<span class="secret">{e(pw)}</span></p><p class="muted">{e(tr("Tài khoản: admin"))}</p></div>'
        )
    state = t["provision_state"]
    if state in ("queued", "running"):
        body += f'<p class="msg ok">{e(tr("Đang khởi tạo hệ thống của bạn, thường mất 1-2 phút. Trang sẽ tự làm mới."))}</p>'
        body += '<meta http-equiv="refresh" content="10">'
    elif state == "failed":
        body += f'<p class="msg err">{e(tr("Khởi tạo gặp lỗi; chúng tôi sẽ thử lại và liên hệ với bạn nếu cần."))}</p>'
    usage = await svc.usage(t)
    disk = f"{usage['disk_mb']} MB" if usage.get("disk_mb") is not None else "–"
    if t["status"] == "trial":
        period = tr(
            "Dùng thử đến {0} (còn {1} ngày)",
            t["trial_ends"],
            max(_days_left(t["trial_ends"], svc.today()), 0),
        )
    else:
        period = tr("Đã thanh toán đến {0}", t["paid_until"] or "–")
    body += (
        f'<div class="card"><table>'
        f"<tr><th>{e(tr('Trạng thái'))}</th><td>{e(tr(STATUS_LABELS[t['status']]))}</td></tr>"
        f"<tr><th>{e(tr('Gói dịch vụ'))}</th><td>{e(plan.name)} – {e(tr('{0}/tháng', _money(plan.price_month, cfg.currency)))}</td></tr>"
        f"<tr><th>{e(tr('Thời hạn'))}</th><td>{e(period)}</td></tr>"
        f"<tr><th>{e(tr('Dung lượng đã dùng'))}</th><td>{e(disk)} / {plan.limits.get('storage_mb') or '∞'} MB</td></tr>"
        f'<tr><th>{e(tr("Trang quản trị"))}</th><td><a href="{e(cfg.admin_url(t["slug"]))}">{e(cfg.admin_url(t["slug"]))}</a></td></tr>'
        f'<tr><th>{e(tr("Website bán hàng"))}</th><td><a href="{e(cfg.shop_url(t["slug"]))}">{e(cfg.shop_url(t["slug"]))}</a></td></tr>'
        f"</table></div>"
        f"<h2>{e(tr('Hoá đơn'))}</h2>{_invoice_rows(request, svc.store.invoices(t['id']), console=False)}"
        f"<h2>{e(tr('Tài khoản'))}</h2>"
        f'<form method="post" action="/portal/password" class="card">{_hidden(request)}<h3>{e(tr("Đổi mật khẩu"))}</h3>'
        f'<label>{e(tr("Mật khẩu hiện tại"))}</label><input name="current" type="password" required>'
        f'<label>{e(tr("Mật khẩu mới"))}</label><input name="new" type="password" minlength="10" required>'
        f"<button>{e(tr('Đổi mật khẩu'))}</button></form>"
        f'<div class="card"><form method="post" action="/portal/admin-password" class="inline">{_hidden(request)}'
        f"<button>{e(tr('Cấp lại mật khẩu quản trị'))}</button></form> "
        + (
            f'<form method="post" action="/portal/cancel" class="inline">{_hidden(request)}<input type="hidden" name="cancel" value="0">'
            f"<button>{e(tr('Rút lại yêu cầu ngừng dịch vụ'))}</button></form>"
            if t["cancel_requested"]
            else f'<form method="post" action="/portal/cancel" class="inline">{_hidden(request)}<input type="hidden" name="cancel" value="1">'
            f'<button class="danger">{e(tr("Yêu cầu ngừng dịch vụ"))}</button></form>'
        )
        + (
            f' <a href="/portal/backup">{e(tr("Tải bản sao lưu mới nhất"))}</a>'
            if svc.backend.backup_path(t)
            else ""
        )
        + f'</div><form method="post" action="/portal/logout" class="inline">{_hidden(request)}<button>{e(tr("Đăng xuất"))}</button></form>'
    )
    return _page(request, t["shop_name"], body)


async def portal_password(request: web.Request) -> web.Response:
    data = await _input(request)
    t = _tenant(request)
    request.app[SVC].change_password(t, data.get("current", ""), data.get("new", ""))
    resp = (
        _reply(request, "/portal/login", {})
        if request.get(IS_JSON)
        else web.HTTPSeeOther(_flash("/portal/login", "ok", tr("Đã đổi mật khẩu, hãy đăng nhập lại")))
    )
    resp.del_cookie(TENANT, path="/")
    if isinstance(resp, web.HTTPException):
        raise resp
    return resp


async def portal_admin_password(request: web.Request) -> web.Response:
    await _input(request)
    t = _tenant(request)
    svc = request.app[SVC]
    password = await svc.reset_admin_password(t, t["email"])
    if request.get(IS_JSON):
        return _reply(request, "/portal", {"admin_password": password})
    svc.reveals[t["id"]] = password
    return _reply(request, "/portal")


async def portal_cancel(request: web.Request) -> web.Response:
    data = await _input(request)
    t = _tenant(request)
    request.app[SVC].request_cancellation(t, t["email"], data.get("cancel", "1") == "1")
    return _reply(request, _flash("/portal", "ok", tr("Đã ghi nhận")))


def _portal_invoice(request: web.Request, t: dict[str, Any]) -> dict[str, Any]:
    inv = request.app[SVC].store.invoice(int(request.match_info["iid"]))
    if inv is None or inv["tenant_id"] != t["id"]:
        raise SaasError(tr("Không tìm thấy hoá đơn"), 404)
    return inv


async def portal_invoice(request: web.Request) -> web.Response:
    t = _tenant(request)
    inv = _portal_invoice(request, t)
    cfg = request.app[CFG]
    e = html.escape
    bank = "".join(f"<tr><th>{e(k)}</th><td>{e(v)}</td></tr>" for k, v in cfg.bank.items())
    body = (
        f"<h1>{e(tr('Hoá đơn #{0}', inv['id']))}</h1><div class='card'><table>"
        f"<tr><th>{e(tr('Kỳ'))}</th><td>{e(inv['period_start'])} → {e(inv['period_end'])}</td></tr>"
        f"<tr><th>{e(tr('Số tiền'))}</th><td><b>{e(_money(inv['amount'], inv['currency']))}</b></td></tr>"
        f"<tr><th>{e(tr('Hạn thanh toán'))}</th><td>{e(inv['due'])}</td></tr>"
        f"<tr><th>{e(tr('Trạng thái'))}</th><td>{e(tr(INVOICE_LABELS.get(inv['status'], inv['status'])))}</td></tr></table></div>"
        f"<h2>{e(tr('Chuyển khoản ngân hàng'))}</h2><div class='card'><table>{bank}"
        f"<tr><th>{e(tr('Nội dung chuyển khoản'))}</th><td><span class='secret'>SAAS-{inv['id']}</span></td></tr></table>"
        f"<p class='muted'>{e(tr('Ghi đúng nội dung để hệ thống đối soát. Sau khi chuyển, bấm nút dưới đây; dịch vụ được gia hạn ngay khi chúng tôi xác nhận.'))}</p>"
        + (
            f'<form method="post" action="/portal/invoices/{inv["id"]}/transferred">{_hidden(request)}<button>{e(tr("Đã chuyển khoản"))}</button></form>'
            if inv["status"] == "due"
            else ""
        )
        + "</div>"
    )
    return _page(request, tr("Hoá đơn #{0}", inv["id"]), body)


async def portal_transferred(request: web.Request) -> web.Response:
    await _input(request)
    t = _tenant(request)
    inv = _portal_invoice(request, t)
    request.app[BILL].claim_transferred(inv, t["email"])
    return _reply(
        request,
        _flash("/portal", "ok", tr("Cảm ơn bạn, chúng tôi sẽ xác nhận sớm")),
        {"status": "awaiting_confirmation"},
    )


async def portal_backup(request: web.Request) -> web.Response:
    t = _tenant(request)
    path = request.app[SVC].backend.backup_path(t)
    if path is None:
        raise SaasError(tr("Chưa có bản sao lưu"), 404)
    return web.FileResponse(path, headers={"Content-Disposition": f'attachment; filename="{path.name}"'})


# --------------------------------------------------------------------------- #
# Operator console


async def console_login_page(request: web.Request) -> web.Response:
    e = html.escape
    body = (
        f"<h1>{e(tr('Bảng điều hành'))}</h1>"
        f'<form method="post" action="/console/login" class="card">{_hidden(request)}'
        f'<label>{e(tr("Tên đăng nhập"))}</label><input name="username" required>'
        f'<label>{e(tr("Mật khẩu"))}</label><input name="password" type="password" required>'
        f"<button>{e(tr('Đăng nhập'))}</button></form>"
    )
    return _page(request, tr("Bảng điều hành"), body)


async def console_login(request: web.Request) -> web.Response:
    data = await _input(request)
    _rate(request, "login")
    cfg, svc = request.app[CFG], request.app[SVC]
    username = data.get("username", "").strip().lower()
    ok = False
    for op in cfg.operators:  # constant work whatever the username
        ok |= hmac.compare_digest(op.username, username) & hmac.compare_digest(
            op.password, data.get("password", "")
        )
    if not ok:
        log.warning("console: failed login from %s", _client_ip(request))
        raise SaasError(tr("Sai tên đăng nhập hoặc mật khẩu"), 401)
    resp = (
        _reply(request, "/console", {"user": username})
        if request.get(IS_JSON)
        else web.HTTPSeeOther("/console")
    )
    _cookie(
        request,
        resp,
        OPERATOR,
        svc.store.add_session("operator", username, cfg.session_hours * 3600),
        cfg.session_hours * 3600,
    )
    if isinstance(resp, web.HTTPException):
        raise resp
    return resp


async def console_logout(request: web.Request) -> web.Response:
    await _input(request)
    request.app[SVC].store.drop_session(request.cookies.get(OPERATOR, ""))
    resp = web.HTTPSeeOther("/console/login")
    resp.del_cookie(OPERATOR, path="/")
    raise resp


async def console_home(request: web.Request) -> web.Response:
    _operator(request)
    svc, cfg = request.app[SVC], request.app[CFG]
    e = html.escape
    tenants = [t for t in svc.store.tenants() if t["status"] != "deleted" or request.query.get("all")]
    if request.headers.get("Accept", "").startswith("application/json"):
        return web.json_response({"tenants": [_public(cfg, t) for t in tenants]})
    rows = []
    for t in tenants:
        last = svc.store.invoices(t["id"])[:1]
        last_txt = f"#{last[0]['id']} {tr(INVOICE_LABELS[last[0]['status']])}" if last else "–"
        rows.append(
            f'<tr><td><a href="/console/tenants/{t["id"]}">{e(t["slug"])}</a><br><span class="muted">{e(t["shop_name"])}</span></td>'
            f"<td>{e(tr(STATUS_LABELS[t['status']]))}<br><span class='muted'>{e(t['provision_state'])}</span></td>"
            f"<td>{e(t['plan'])}</td><td>{e(t['trial_ends'] or '')}</td><td>{e(t['paid_until'] or '')}</td><td>{e(last_txt)}</td>"
            f"<td>{'✓' if t['cancel_requested'] else ''}</td></tr>"
        )
    pending = svc.store.invoices(statuses=("awaiting_confirmation", "due"))
    plan_opts = "".join(f'<option value="{pid}">{e(p.name)}</option>' for pid, p in cfg.plans.items())
    body = (
        f"<h1>{e(tr('Khách hàng'))} ({len(tenants)})</h1><table><tr><th>{e(tr('Tên miền con'))}</th><th>{e(tr('Trạng thái'))}</th>"
        f"<th>{e(tr('Gói'))}</th><th>{e(tr('Hết dùng thử'))}</th><th>{e(tr('Đã trả đến'))}</th><th>{e(tr('Hoá đơn gần nhất'))}</th><th>{e(tr('Xin ngừng'))}</th></tr>"
        f"{''.join(rows)}</table><p><a href='/console?all=1'>{e(tr('Kể cả đã xoá'))}</a></p>"
        f"<h2>{e(tr('Hoá đơn chờ xử lý'))}</h2>{_invoice_rows(request, pending, console=True)}"
        f"<h2>{e(tr('Tạo khách hàng thủ công'))}</h2><form method='post' action='/console/tenants' class='card'>{_hidden(request)}"
        f"<label>{e(tr('Tên cửa hàng'))}</label><input name='shop_name' required><label>{e(tr('Tên chủ'))}</label><input name='owner_name' required>"
        f"<label>Email</label><input name='email' type='email' required><label>{e(tr('Mật khẩu cổng khách hàng'))}</label><input name='password' minlength='10' required>"
        f"<label>{e(tr('Tên miền con'))}</label><input name='slug' pattern='[a-z0-9][a-z0-9-]{{2,30}}'><label>{e(tr('Gói'))}</label><select name='plan'>{plan_opts}</select>"
        f"<label>{e(tr('Ngôn ngữ'))}</label><input name='lang' value='vi' maxlength='5'><button>{e(tr('Tạo và khởi tạo'))}</button></form>"
        f"<form method='post' action='/console/daily' class='inline'>{_hidden(request)}<button>{e(tr('Chạy công việc hằng ngày ngay'))}</button></form> "
        f"<form method='post' action='/console/logout' class='inline'>{_hidden(request)}<button>{e(tr('Đăng xuất'))}</button></form>"
    )
    return _page(request, tr("Bảng điều hành"), body)


def _console_tenant(request: web.Request) -> dict[str, Any]:
    t = request.app[SVC].store.tenant(int(request.match_info["tid"]))
    if t is None:
        raise SaasError(tr("Không tìm thấy khách hàng"), 404)
    return t


async def console_tenant(request: web.Request) -> web.Response:
    _operator(request)
    t = _console_tenant(request)
    svc, cfg = request.app[SVC], request.app[CFG]
    e = html.escape
    if request.headers.get("Accept", "").startswith("application/json"):
        return web.json_response(
            {
                "tenant": _public(cfg, t),
                "invoices": svc.store.invoices(t["id"]),
                "events": svc.store.events(t["id"]),
            }
        )
    usage = await svc.usage(t)
    if pw := svc.reveal(t["id"]):
        pw_html = f'<p class="msg ok">{e(tr("Mật khẩu quản trị mới (hiển thị một lần):"))} <span class="secret">{e(pw)}</span></p>'
    else:
        pw_html = ""
    base = f"/console/tenants/{t['id']}"
    plan_opts = "".join(
        f'<option value="{pid}"{" selected" if pid == t["plan"] else ""}>{e(p.name)}</option>'
        for pid, p in cfg.plans.items()
    )

    def form(action: str, label: str, extra: str = "", danger: bool = False) -> str:
        return (
            f'<form method="post" action="{base}/{action}" class="inline">{_hidden(request)}{extra}'
            f"<button{' class=danger' if danger else ''}>{e(label)}</button></form>"
        )

    events = "".join(
        f"<tr><td>{e(ev['ts'])}</td><td>{e(ev['actor'])}</td><td>{e(ev['action'])}</td><td>{e(ev['detail'])}</td></tr>"
        for ev in svc.store.events(t["id"])
    )
    body = (
        f"<h1>{e(t['slug'])} – {e(t['shop_name'])}</h1>{pw_html}<div class='card'><table>"
        f"<tr><th>{e(tr('Trạng thái'))}</th><td>{e(tr(STATUS_LABELS[t['status']]))} ({e(t['provision_state'])}"
        f"{' – ' + e(t['provision_error']) if t['provision_error'] else ''}); {e(tr('container'))}: {e(svc.backend.status(t))}</td></tr>"
        f"<tr><th>{e(tr('Chủ'))}</th><td>{e(t['owner_name'])} · {e(t['email'])} · {e(t['phone'])}</td></tr>"
        f"<tr><th>{e(tr('Gói'))}</th><td>{e(t['plan'])}</td></tr><tr><th>{e(tr('Hết dùng thử'))}</th><td>{e(t['trial_ends'] or '')}</td></tr>"
        f"<tr><th>{e(tr('Đã trả đến'))}</th><td>{e(t['paid_until'] or '')}</td></tr>"
        f"<tr><th>{e(tr('Dung lượng'))}</th><td>{usage.get('disk_mb')} MB</td></tr>"
        f"<tr><th>{e(tr('Cổng'))}</th><td>{t['admin_port']} / {t['shop_port']}</td></tr>"
        f'<tr><th>URL</th><td><a href="{e(cfg.admin_url(t["slug"]))}">{e(cfg.admin_url(t["slug"]))}</a> · <a href="{e(cfg.shop_url(t["slug"]))}">{e(cfg.shop_url(t["slug"]))}</a></td></tr>'
        f"</table></div><div class='card'>"
        + form("suspend", tr("Tạm dừng"), danger=True)
        + form("resume", tr("Mở lại"))
        + form("provision", tr("Khởi tạo lại"))
        + form("admin-password", tr("Cấp lại mật khẩu quản trị"))
        + form("plan", tr("Đổi gói"), f'<select name="plan">{plan_opts}</select>')
        + form(
            "extend", tr("Gia hạn dùng thử"), '<input name="days" type="number" value="7" min="1" max="365">'
        )
        + form(
            "delete",
            tr("Xoá"),
            f'<input name="confirm" placeholder="{e(tr("gõ tên miền con để xác nhận"))}">',
            danger=True,
        )
        + f"</div><h2>{e(tr('Hoá đơn'))}</h2>{_invoice_rows(request, svc.store.invoices(t['id']), console=True)}"
        f"<h2>{e(tr('Nhật ký'))}</h2><table><tr><th>{e(tr('Lúc'))}</th><th>{e(tr('Ai'))}</th><th>{e(tr('Việc'))}</th><th></th></tr>{events}</table>"
    )
    return _page(request, t["slug"], body)


async def console_create(request: web.Request) -> web.Response:
    data = await _input(request)
    who = _operator(request)
    svc = request.app[SVC]
    tenant = await svc.signup(data)  # validated like a public sign-up, then verified by the operator
    svc.store.log(tenant["id"], who, "created_manually")
    tenant = await svc.activate_trial(tenant, actor=who)
    return _reply(
        request,
        f"/console/tenants/{tenant['id']}",
        {
            "tenant": _public(request.app[CFG], tenant),
            "admin_password": svc.reveals.get(tenant["id"]) if request.get(IS_JSON) else None,
        },
    )


async def console_action(request: web.Request) -> web.Response:
    data = await _input(request)
    who = _operator(request)
    t = _console_tenant(request)
    svc = request.app[SVC]
    action = request.match_info["action"]
    result: dict[str, Any] = {}
    if action == "suspend":
        await svc.suspend(t, who, "operator")
    elif action == "resume":
        await svc.resume(t, who)
    elif action == "provision":
        if t["status"] in ("deleted", "pending_email"):
            raise SaasError(tr("Dịch vụ không hoạt động"), 409)
        password = secrets.token_urlsafe(12)
        svc.reveals[t["id"]] = password
        svc.spawn(
            svc._provision(t["id"], {"AI_ADMIN_PASSWORD": password, "AI_ADMIN_TOKEN": secrets.token_hex(16)})
        )
        result["admin_password"] = password if request.get(IS_JSON) else None
    elif action == "admin-password":
        password = await svc.reset_admin_password(t, who)
        if request.get(IS_JSON):
            result["admin_password"] = password
        else:
            svc.reveals[t["id"]] = password
    elif action == "plan":
        await svc.change_plan(t, data.get("plan", ""), who)
    elif action == "extend":
        try:
            days = int(data.get("days", "0"))
        except ValueError:
            raise SaasError(tr("Số ngày gia hạn: 1-365")) from None
        await svc.extend_trial(t, days, who)
    elif action == "delete":
        if data.get("confirm", "") != t["slug"]:
            raise SaasError(tr("Gõ đúng tên miền con để xác nhận xoá"), 400)
        result["backup"] = await svc.delete(t, who, keep_backup=True)
    else:
        raise SaasError("unknown action", 404)
    return _reply(request, f"/console/tenants/{t['id']}", result)


async def console_invoice(request: web.Request) -> web.Response:
    data = await _input(request)
    who = _operator(request)
    bill = request.app[BILL]
    inv = bill.store.invoice(int(request.match_info["iid"]))
    if inv is None:
        raise SaasError(tr("Không tìm thấy hoá đơn"), 404)
    action = request.match_info["action"]
    if action == "confirm":
        await bill.mark_paid(inv, data.get("gateway") or "bank_transfer", data.get("ref", ""), actor=who)
    elif action == "void":
        bill.void(inv, who)
    else:
        raise SaasError("unknown action", 404)
    return _reply(request, f"/console/tenants/{inv['tenant_id']}", {"status": action})


async def console_daily(request: web.Request) -> web.Response:
    await _input(request)
    who = _operator(request)
    counts = await request.app[BILL].daily()
    request.app[SVC].store.log(None, who, "daily_run", counts)
    return _reply(
        request, _flash("/console", "ok", tr("Đã chạy: {0}", json.dumps(counts))), {"counts": counts}
    )


# --------------------------------------------------------------------------- #


def create_app(cfg: SaasConfig, service: Service, billing: Billing) -> web.Application:
    app = web.Application(middlewares=[middleware], client_max_size=64 * 1024)
    app[CFG], app[SVC], app[BILL], app[RATE] = cfg, service, billing, {}
    r = app.router
    r.add_get("/", landing)
    r.add_get("/static/saas.css", css)
    r.add_get("/health", health)
    r.add_get("/caddy-ask", caddy_ask)
    r.add_post("/signup", signup)
    r.add_get("/verify", verify_page)
    r.add_post("/verify", verify)
    r.add_post("/verify/resend", verify_resend)
    r.add_get("/portal", portal_home)
    r.add_get("/portal/login", portal_login_page)
    r.add_post("/portal/login", portal_login)
    r.add_post("/portal/logout", portal_logout)
    r.add_post("/portal/password", portal_password)
    r.add_post("/portal/admin-password", portal_admin_password)
    r.add_post("/portal/cancel", portal_cancel)
    r.add_get("/portal/invoices/{iid:\\d+}", portal_invoice)
    r.add_post("/portal/invoices/{iid:\\d+}/transferred", portal_transferred)
    r.add_get("/portal/backup", portal_backup)
    r.add_get("/console", console_home)
    r.add_get("/console/login", console_login_page)
    r.add_post("/console/login", console_login)
    r.add_post("/console/logout", console_logout)
    r.add_post("/console/tenants", console_create)
    r.add_get("/console/tenants/{tid:\\d+}", console_tenant)
    r.add_post("/console/tenants/{tid:\\d+}/{action}", console_action)
    r.add_post("/console/invoices/{iid:\\d+}/{action}", console_invoice)
    r.add_post("/console/daily", console_daily)
    return app

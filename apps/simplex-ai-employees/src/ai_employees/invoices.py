"""Invoices / receipts as a web page (printing), as an email, and as a chat message."""

from __future__ import annotations

import html
import logging
from typing import TYPE_CHECKING, Any

from .i18n import RTL, current, number, tr, use_language
from .inventory import InventoryError

if TYPE_CHECKING:
    from .employee import Office

log = logging.getLogger(__name__)
PAY = {
    "cash": "Tiền mặt",
    "card": "Thẻ",
    "transfer": "Chuyển khoản",
    "wallet": "Ví điện tử",
    "cod": "Thu hộ (COD)",
    "other": "Khác",
    "refund": "Hoàn tiền",
}
# the page's own inline style and print button (no other script, no remote content)
RECEIPT_CSP = "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'"


def _money(r: dict[str, Any]) -> Any:
    cur = tr("đ") if r["shop"]["currency"] == "VND" else r["shop"]["currency"]
    return lambda v: html.escape(f"{number(v)} {cur}")


RECEIPT_STYLE = (
    "body{font-family:system-ui,sans-serif;max-width:420px;margin:12px auto;font-size:13px;color:#111}"
    "table{width:100%;border-collapse:collapse}td{padding:3px 2px;vertical-align:top;border-bottom:1px dashed #ccc}"
    "td:nth-child(n+2){text-align:right;white-space:nowrap}h1{font-size:16px;margin:0}.c{text-align:center}"
    ".t{font-weight:700;font-size:15px}@media print{button{display:none}}"
)


def receipt_html(office: Office, oid: int, printable: bool = True, lang: str | None = None) -> str:
    """The receipt page, in `lang` (else the shop's language: it is for the customer)."""
    with use_language(lang):
        return _receipt_html(office, oid, printable, current())


def _receipt_html(office: Office, oid: int, printable: bool, lang: str) -> str:
    r = office.inventory.receipt(oid)
    e, m, shop = html.escape, _money(r), r["shop"]
    rows = "".join(
        f"<tr><td>{e(i['name'])}<br><small>{e(i['sku'])}{' · ' + e(i['promo']) if i['promo'] else ''}</small></td>"
        f"<td>{i['qty']}</td><td>{m(i['unit_price'])}</td><td>{m(i['line_total'])}</td></tr>"
        for i in r["items"]
    )
    pays = "".join(
        f"<div>{e(tr(PAY.get(p['method'], p['method'])))}: {m(p['amount'])}"
        + (f" · {e(tr('tiền thối'))} {m(p['change'])}" if p["change"] else "")
        + "</div>"
        for p in r["payments"]
    )
    discount = r["discount"] + r["voucher_discount"]
    totals = [f"{e(tr('Tạm tính'))}: {m(r['subtotal'])}"]
    if discount:
        totals.append(f"{e(tr('Giảm giá'))}: -{m(discount)}")
    if r["shipping_fee"]:
        totals.append(f"{e(tr('Phí giao hàng'))}: {m(r['shipping_fee'])}")
    totals.append(f'<span class="t">{e(tr("Tổng"))}: {m(r["total"])}</span>')
    if r["vat_pct"]:
        totals.append(f"<small>{e(tr('Đã gồm VAT {0}%', r['vat_pct']))}: {m(r['vat_amount'])}</small>")
    paid = f"{e(tr('Đã trả'))}: {m(r['paid'])}" + (
        f" · <b>{e(tr('Còn lại'))}: {m(r['due'])}</b>" if r["due"] else ""
    )
    bank = f"<p>{e(tr('Chuyển khoản'))}: {e(shop['bank_info'])}</p>" if shop["bank_info"] and r["due"] else ""
    tax = f" · {e(tr('MST'))} {e(shop['tax_code'])}" if shop["tax_code"] else ""
    head = "".join(f"<td>{e(x)}</td>" for x in (tr("Sản phẩm"), tr("SL"), tr("Đơn giá"), tr("Thành tiền")))
    button = f'<p class="c"><button onclick="print()">{e(tr("In"))}</button></p>' if printable else ""
    return (
        f'<!doctype html><html lang="{lang}"{" dir=rtl" if lang in RTL else ""}><head><meta charset="utf-8">'
        f'<title>{e(r["code"])}</title><meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<style>{RECEIPT_STYLE}</style></head><body>"
        f'<div class="c"><h1>{e(shop["shop_name"] or tr("Hoá đơn bán hàng"))}</h1><div>{e(shop["shop_address"])}</div>'
        f"<div>{e(shop['shop_phone'])}{tax}</div>"
        f"<p><b>{e(r['code'])}</b> · {e(r['created'][:16].replace('T', ' '))}<br>{e(r['customer_name'])} {e(r['phone'])}</p></div>"
        f"<table><tr>{head}</tr>{rows}</table><p>{'<br>'.join(totals)}</p>{pays}<p>{paid}</p>{bank}"
        f'<p class="c">{e(shop["receipt_footer"])}</p>{button}</body></html>'
    )


async def email_invoice(office: Office, oid: int, to: str | None = None) -> str:
    """Send the invoice of a sale by email (to the given address, the order's or the customer's)."""
    order = office.inventory.order(oid)
    address = (to or order.get("email") or "").strip()
    if not address and order.get("contact_id"):
        contact = office.hub.crm.contact(int(order["contact_id"])) or {}
        address = contact.get("email") or ""
    if not address:
        raise InventoryError(tr("Khách chưa có email"))
    with use_language(None):  # for the customer: the shop's language
        shop = office.inventory.settings()["shop_name"] or tr("Cửa hàng")
        subject, text = tr("{0} – Hoá đơn {1}", shop, order["code"]), office.inventory.receipt_text(oid)
        page = receipt_html(office, oid, printable=False)
    await office.mailer.send(address, subject, text, page)
    if not order.get("email"):
        office.inventory.db.execute("UPDATE inv_orders SET email=? WHERE id=?", (address, oid))
    return address


def install_auto_invoice(office: Office) -> None:
    """With 'auto_invoice' on, a sale's invoice is emailed when it is completed."""

    def on_event(event: str, data: dict[str, Any]) -> None:
        if event != "order_completed" or not office.mailer.settings().get("auto_invoice"):
            return
        order = data["order"]

        async def send() -> None:
            try:
                await email_invoice(office, int(order["id"]))
            except InventoryError as e:
                log.info("invoice %s not emailed: %s", order["code"], e)

        office.hub.spawn(send())

    office.inventory.listeners.append(on_event)

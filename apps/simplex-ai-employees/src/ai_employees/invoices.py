"""Invoices / receipts as a web page (printing), as an email, and as a chat message."""

from __future__ import annotations

import html
import logging
from typing import TYPE_CHECKING, Any

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
    cur = "đ" if r["shop"]["currency"] == "VND" else r["shop"]["currency"]
    return lambda v: html.escape(f"{v:,}".replace(",", ".") + f" {cur}")


def receipt_html(office: Office, oid: int, printable: bool = True) -> str:
    r = office.inventory.receipt(oid)
    e, m, shop = html.escape, _money(r), r["shop"]
    rows = "".join(
        f"<tr><td>{e(i['name'])}<br><small>{e(i['sku'])}{' · ' + e(i['promo']) if i['promo'] else ''}</small></td>"
        f"<td>{i['qty']}</td><td>{m(i['unit_price'])}</td><td>{m(i['line_total'])}</td></tr>"
        for i in r["items"]
    )
    pays = "".join(
        f"<div>{e(PAY.get(p['method'], p['method']))}: {m(p['amount'])}"
        f"{' · tiền thối ' + m(p['change']) if p['change'] else ''}</div>"
        for p in r["payments"]
    )
    discount = r["discount"] + r["voucher_discount"]
    return f"""<!doctype html><html lang="vi"><head><meta charset="utf-8"><title>{e(r["code"])}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>body{{font-family:system-ui,sans-serif;max-width:420px;margin:12px auto;font-size:13px;color:#111}}table{{width:100%;border-collapse:collapse}}
td{{padding:3px 2px;vertical-align:top;border-bottom:1px dashed #ccc}}td:nth-child(n+2){{text-align:right;white-space:nowrap}}
h1{{font-size:16px;margin:0}}.c{{text-align:center}}.t{{font-weight:700;font-size:15px}}@media print{{button{{display:none}}}}</style></head>
<body><div class="c"><h1>{e(shop["shop_name"] or "Hoá đơn bán hàng")}</h1><div>{e(shop["shop_address"])}</div>
<div>{e(shop["shop_phone"])}{" · MST " + e(shop["tax_code"]) if shop["tax_code"] else ""}</div>
<p><b>{e(r["code"])}</b> · {e(r["created"][:16].replace("T", " "))}<br>{e(r["customer_name"])} {e(r["phone"])}</p></div>
<table><tr><td>Sản phẩm</td><td>SL</td><td>Đơn giá</td><td>Thành tiền</td></tr>{rows}</table>
<p>Tạm tính: {m(r["subtotal"])}<br>{"Giảm giá: -" + m(discount) + "<br>" if discount else ""}
{"Phí giao hàng: " + m(r["shipping_fee"]) + "<br>" if r["shipping_fee"] else ""}<span class="t">Tổng: {m(r["total"])}</span>
{"<br><small>Đã gồm VAT " + str(r["vat_pct"]) + "%: " + m(r["vat_amount"]) + "</small>" if r["vat_pct"] else ""}</p>
{pays}<p>Đã trả: {m(r["paid"])}{" · <b>Còn lại: " + m(r["due"]) + "</b>" if r["due"] else ""}</p>
{"<p>Chuyển khoản: " + e(shop["bank_info"]) + "</p>" if shop["bank_info"] and r["due"] else ""}
<p class="c">{e(shop["receipt_footer"])}</p>{'<p class="c"><button onclick="print()">In</button></p>' if printable else ""}</body></html>"""


async def email_invoice(office: Office, oid: int, to: str | None = None) -> str:
    """Send the invoice of a sale by email (to the given address, the order's or the customer's)."""
    order = office.inventory.order(oid)
    address = (to or order.get("email") or "").strip()
    if not address and order.get("contact_id"):
        contact = office.hub.crm.contact(int(order["contact_id"])) or {}
        address = contact.get("email") or ""
    if not address:
        raise InventoryError("Khách chưa có email")
    shop = office.inventory.settings()["shop_name"] or "Cửa hàng"
    await office.mailer.send(
        address,
        f"{shop} – Hoá đơn {order['code']}",
        office.inventory.receipt_text(oid),
        receipt_html(office, oid, printable=False),
    )
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

"""Online payments for the web shop: VNPay, MoMo and ZaloPay (and bank transfer details).

Settings (Kho hàng -> Cửa hàng & tích điểm -> Thanh toán online) hold the merchant ids
and the *names* of the environment variables with the secrets (never the secrets), and
whether the sandbox is used. A customer pays what is still due on a confirmed order: a
*payment intent* (table pay_intents) is made with a unique reference, the customer is
sent to the gateway, and the gateway's server-to-server notification (IPN / callback),
verified with the secret, records the payment in the inventory, once, whatever the
number of retries. The return page (where the customer lands afterwards) shows the
result; for VNPay and MoMo it is signed too and records the payment as well, for ZaloPay
only the callback does.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from datetime import UTC, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any
from urllib.parse import quote_plus

import httpx2

from .i18n import number, tr, use_language
from .inventory import InventoryError, order_code

if TYPE_CHECKING:
    from .employee import Office

log = logging.getLogger(__name__)

KEY = "payment_settings"
GATEWAYS = ("vnpay", "momo", "zalopay")
NAMES = {"vnpay": "VNPay", "momo": "MoMo", "zalopay": "ZaloPay"}
METHODS = {"vnpay": "transfer", "momo": "wallet", "zalopay": "wallet"}
# the environment variables each gateway needs (settings key -> default name)
SECRETS = {
    "vnpay": {"secret_env": "VNPAY_SECRET"},
    "momo": {"secret_env": "MOMO_SECRET"},
    "zalopay": {"key1_env": "ZALOPAY_KEY1", "key2_env": "ZALOPAY_KEY2"},
}
IDS = {"vnpay": ("tmn_code",), "momo": ("partner_code", "access_key"), "zalopay": ("app_id",)}
URLS = {
    "vnpay": ("https://sandbox.vnpayment.vn/paymentv2/vpcpay.html", "https://pay.vnpay.vn/vpcpay.html"),
    "momo": (
        "https://test-payment.momo.vn/v2/gateway/api/create",
        "https://payment.momo.vn/v2/gateway/api/create",
    ),
    "zalopay": ("https://sb-openapi.zalopay.vn/v2/create", "https://openapi.zalopay.vn/v2/create"),
}
EXPIRE_MINUTES = 15  # a payment page's validity (VNPay's vnp_ExpireDate; the others are similar)
VN = timezone(timedelta(hours=7))
_ENV = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,79}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS pay_intents (
  id {id}, order_id {int} NOT NULL, gateway TEXT NOT NULL, ref TEXT NOT NULL UNIQUE, amount {int} NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending', created TEXT NOT NULL, paid_at TEXT, txn TEXT NOT NULL DEFAULT '',
  raw TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS pay_intents_order ON pay_intents (order_id, id)
"""


def _hmac(key: str, data: str, algo: Any = hashlib.sha256) -> str:
    return hmac.new(key.encode(), data.encode(), algo).hexdigest()


def _same(a: str, b: str) -> bool:
    return hmac.compare_digest((a or "").lower().encode(), (b or "").lower().encode())


def _ascii(text: str, limit: int = 200) -> str:
    """Order descriptions the gateways accept everywhere: ASCII letters, digits, spaces."""
    import unicodedata

    plain = unicodedata.normalize("NFKD", text.replace("đ", "d").replace("Đ", "D"))
    plain = "".join(c for c in plain if not unicodedata.combining(c))
    return re.sub(r"[^A-Za-z0-9 .:_-]+", " ", plain).strip()[:limit]


def _utc() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def payments_of(office: Office) -> Payments:
    """The office's Payments (made on first use when employee.py does not wire it)."""
    found = getattr(office, "payments", None)
    if found is None:
        found = Payments(office)
        office.payments = found  # type: ignore[attr-defined]
    return found


class Payments:
    def __init__(self, office: Office):
        self.office = office
        self.inv = office.inventory
        self.db = self.inv.db
        self.db.script(SCHEMA)

    # ------------------------------------------------------------------ #
    # settings

    def settings(self) -> dict[str, Any]:
        s = self.office.docs.get(KEY) or {}
        out: dict[str, Any] = {"sandbox": bool(s.get("sandbox", True))}
        for g in GATEWAYS:
            c = s.get(g) or {}
            out[g] = {"enabled": bool(c.get("enabled"))}
            for k in IDS[g]:
                out[g][k] = str(c.get(k) or "")
            for k, default in SECRETS[g].items():
                out[g][k] = str(c.get(k) or default)
        bank = s.get("bank") or {}
        out["bank"] = {"enabled": bool(bank.get("enabled")), "info": str(bank.get("info") or "")}
        return out

    def _secret(self, gateway: str, key: str = "secret_env") -> str:
        return os.environ.get(self.settings()[gateway].get(key) or "", "")

    def configured(self, gateway: str) -> bool:
        """Enabled, with its ids and every secret present in the environment."""
        c = self.settings()[gateway]
        return bool(
            c["enabled"]
            and all(c[k] for k in IDS[gateway])
            and all(os.environ.get(c[k] or "") for k in SECRETS[gateway])
        )

    def public(self) -> dict[str, Any]:
        """The settings for the admin UI: names of variables and whether they are set,
        never a secret."""
        s = self.settings()
        for g in GATEWAYS:
            s[g]["configured"] = self.configured(g)
            s[g]["env_set"] = {k: bool(os.environ.get(s[g][k] or "")) for k in SECRETS[g]}
        s["gateways"] = self.enabled_gateways()
        s["available"] = self.available()
        s["public_url"] = getattr(getattr(self.office, "storefront", None), "public_url", "") or ""
        return s

    def save(self, data: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise InventoryError(tr("Cài đặt thanh toán không hợp lệ"))
        clean: dict[str, Any] = {}
        if "sandbox" in data:
            clean["sandbox"] = bool(data["sandbox"])
        for g in GATEWAYS:
            if g not in data:
                continue
            c = data[g] if isinstance(data[g], dict) else {}
            entry: dict[str, Any] = {"enabled": bool(c.get("enabled"))}
            for k in IDS[g]:
                entry[k] = str(c.get(k) or "").strip()[:80]
                if entry["enabled"] and not entry[k]:
                    raise InventoryError(tr("{0}: thiếu {1}", NAMES[g], k))
            for k, default in SECRETS[g].items():
                name = str(c.get(k) or "").strip() or default
                if not _ENV.match(name):
                    raise InventoryError(tr("{0}: tên biến môi trường không hợp lệ ({1})", NAMES[g], name))
                entry[k] = name
            clean[g] = entry
        if "bank" in data:
            b = data["bank"] if isinstance(data["bank"], dict) else {}
            clean["bank"] = {
                "enabled": bool(b.get("enabled")),
                "info": str(b.get("info") or "").strip()[:300],
            }
            if clean["bank"]["enabled"] and not clean["bank"]["info"]:
                raise InventoryError(tr("Chuyển khoản: nhập thông tin tài khoản ngân hàng"))
        self.office.docs.update(KEY, lambda d: d.update(clean), {})
        return self.public()

    def enabled_gateways(self) -> list[str]:
        """The gateways a customer can pay with now (enabled and their secrets set)."""
        return [g for g in GATEWAYS if self.configured(g)]

    def bank_info(self) -> str:
        b = self.settings()["bank"]
        return b["info"] if b["enabled"] else ""

    def available(self) -> bool:
        """Whether the "pay online" page has anything to offer."""
        return bool(self.enabled_gateways() or self.bank_info())

    # ------------------------------------------------------------------ #
    # links

    @property
    def shop(self) -> Any:
        shop = getattr(self.office, "storefront", None)
        if shop is None or not shop.public_url:
            raise InventoryError(tr("Website bán hàng chưa được cấu hình (storefront.public_url)"))
        return shop

    def pay_link(self, order: dict[str, Any] | int) -> str:
        """The customer's private link to pay an order (signed like the order's tracking link)."""
        code = order_code(int(order["id"])) if isinstance(order, dict) else order_code(int(order))
        return f"{self.shop.public_url}/pay/{code}?t={self.shop.sign('order', code)}"

    def payable(self, order: dict[str, Any]) -> bool:
        return bool(order.get("status") == "confirmed" and order.get("due") and self.available())

    # ------------------------------------------------------------------ #
    # intents

    def _due(self, order: dict[str, Any]) -> int:
        return max(0, self.inv.minor(order["total"]) - self.inv.minor(order["paid"]))

    def intent(self, ref: str) -> dict[str, Any] | None:
        return self.db.row("SELECT * FROM pay_intents WHERE ref=?", (ref,))

    def intents(self, order_id: int) -> list[dict[str, Any]]:
        return self.db.rows("SELECT * FROM pay_intents WHERE order_id=? ORDER BY id DESC", (order_id,))

    def _expire(self) -> None:
        cutoff = (datetime.now(UTC) - timedelta(minutes=EXPIRE_MINUTES * 4)).isoformat(timespec="seconds")
        self.db.execute(
            "UPDATE pay_intents SET status='expired' WHERE status='pending' AND created<?", (cutoff,)
        )

    def _mark(self, ref: str, status: str, txn: str = "", raw: Any = None) -> None:
        self.db.execute(
            "UPDATE pay_intents SET status=?, txn=?, raw=?, paid_at=CASE WHEN ?='paid' THEN ? ELSE paid_at END "
            "WHERE ref=?",
            (
                status,
                str(txn or "")[:80],
                json.dumps(raw, ensure_ascii=False)[:4000] if raw is not None else "",
                status,
                _utc(),
                ref,
            ),
        )

    async def create_intent(
        self, order_id: int, gateway: str, return_url: str = "", ip: str = ""
    ) -> dict[str, Any]:
        """Start a payment of what is still due: the URL to send the customer to."""
        if gateway not in GATEWAYS:
            raise InventoryError(tr("Cổng thanh toán không hợp lệ"))
        if not self.configured(gateway):
            raise InventoryError(tr("Cổng {0} chưa được bật", NAMES[gateway]))
        if self.inv.settings()["currency"] != "VND":
            raise InventoryError(tr("Cổng thanh toán chỉ hỗ trợ VND"))
        order = self.inv.order(order_id)
        if order["status"] != "confirmed":
            raise InventoryError(tr("Chỉ thanh toán online cho đơn đã xác nhận và chưa giao"))
        due = self._due(order)
        if due <= 0:
            raise InventoryError(tr("Đơn đã thanh toán đủ"))
        self._expire()
        shop = self.shop
        return_url = return_url or f"{shop.public_url}/pay/return/{gateway}"
        ipn_url = f"{shop.public_url}/pay/ipn/{gateway}"
        vnd = int(self.inv.major(due))
        code = order["code"]
        unique = f"{code}{secrets.token_hex(4).upper()}"
        ref = f"{datetime.now(VN):%y%m%d}_{unique}" if gateway == "zalopay" else unique
        info = _ascii(f"Thanh toan don {code}") or code
        self.db.execute(
            "INSERT INTO pay_intents (order_id, gateway, ref, amount, status, created) VALUES (?, ?, ?, ?, 'pending', ?)",
            (order_id, gateway, ref, due, _utc()),
        )
        try:
            if gateway == "vnpay":
                url = self._vnpay_url(ref, vnd, info, return_url, ip or "127.0.0.1")
            elif gateway == "momo":
                url = await self._momo_create(ref, vnd, info, return_url, ipn_url)
            else:
                url = await self._zalopay_create(ref, vnd, info, return_url, ipn_url, order)
        except InventoryError:
            self._mark(ref, "failed")
            raise
        return {"url": url, "ref": ref, "amount": self.inv.major(due), "gateway": gateway}

    def _record(self, intent: dict[str, Any], txn: str, raw: Any) -> str:
        """Record the paid intent in the inventory (once): "paid", or "already" when the order
        needs no more money (a retry, or the customer paid twice: the shop is told)."""
        if intent["status"] == "paid":
            return "already"
        gateway = intent["gateway"]
        oid = int(intent["order_id"])
        result = "paid"
        try:
            self.inv.add_payment(
                oid,
                METHODS[gateway],
                amount=self.inv.major(int(intent["amount"])),
                ref=f"{NAMES[gateway]} {txn}"[:120],
                idempotency_key=f"{gateway}:{intent['ref']}",
            )
        except InventoryError as e:  # more than is due, or the order was cancelled meanwhile
            log.warning("payments: %s %s for order %s not booked: %s", gateway, intent["ref"], oid, e)
            result = "already"
            self.office.hub.spawn(self._announce(intent, txn, extra=str(e)))
        else:
            self.office.hub.spawn(self._announce(intent, txn))
        self._mark(intent["ref"], "paid", txn, raw)
        return result

    async def _announce(self, intent: dict[str, Any], txn: str, extra: str = "") -> None:
        code = order_code(int(intent["order_id"]))
        amount = number(self.inv.major(int(intent["amount"])))
        name = NAMES[intent["gateway"]]

        def text() -> str:
            base = tr(
                "💳 Đã nhận thanh toán online {0} đ cho đơn {1} qua {2} (mã GD {3}).", amount, code, name, txn
            )
            if extra:
                base += tr(" ⚠️ Chưa ghi vào đơn: {0}. Kiểm tra và hoàn tiền nếu cần.", extra)
            return base

        try:
            employee = next(iter(self.office.employees.values()), None)
            if employee is not None:
                with use_language(None):
                    await employee.notify_admins(text())
            await self.office.staff_links.notify("pos", text)
        except Exception:  # telling the staff is a convenience
            log.exception("payments: staff not told about %s", intent["ref"])

    # ------------------------------------------------------------------ #
    # VNPay

    @staticmethod
    def _vnpay_query(params: dict[str, Any]) -> str:
        return "&".join(
            f"{k}={quote_plus(str(params[k]))}" for k in sorted(params) if params[k] not in ("", None)
        )

    def _vnpay_url(self, ref: str, vnd: int, info: str, return_url: str, ip: str) -> str:
        s = self.settings()
        now = datetime.now(VN)
        params = {
            "vnp_Version": "2.1.0",
            "vnp_Command": "pay",
            "vnp_TmnCode": s["vnpay"]["tmn_code"],
            "vnp_Amount": vnd * 100,
            "vnp_CreateDate": now.strftime("%Y%m%d%H%M%S"),
            "vnp_ExpireDate": (now + timedelta(minutes=EXPIRE_MINUTES)).strftime("%Y%m%d%H%M%S"),
            "vnp_CurrCode": "VND",
            "vnp_IpAddr": ip[:45],
            "vnp_Locale": "vn",
            "vnp_OrderInfo": info,
            "vnp_OrderType": "other",
            "vnp_ReturnUrl": return_url,
            "vnp_TxnRef": ref[:100],
        }
        query = self._vnpay_query(params)
        secure = _hmac(self._secret("vnpay"), query, hashlib.sha512)
        return f"{URLS['vnpay'][0 if s['sandbox'] else 1]}?{query}&vnp_SecureHash={secure}"

    def _vnpay_valid(self, params: dict[str, Any]) -> bool:
        given = str(params.get("vnp_SecureHash") or "")
        data = {
            k: v
            for k, v in params.items()
            if k.startswith("vnp_") and k not in ("vnp_SecureHash", "vnp_SecureHashType")
        }
        secret = self._secret("vnpay")
        return bool(secret and given) and _same(_hmac(secret, self._vnpay_query(data), hashlib.sha512), given)

    def _vnpay_ipn(self, params: dict[str, Any]) -> tuple[int, dict[str, str]]:
        def answer(code: str, message: str) -> tuple[int, dict[str, str]]:
            return 200, {"RspCode": code, "Message": message}

        if not self._vnpay_valid(params):
            return answer("97", "Invalid signature")
        intent = self.intent(str(params.get("vnp_TxnRef") or ""))
        if intent is None or intent["gateway"] != "vnpay":
            return answer("01", "Order not found")
        if str(params.get("vnp_Amount")) != str(int(self.inv.major(int(intent["amount"]))) * 100):
            return answer("04", "Invalid amount")
        if intent["status"] != "pending":
            return answer("02", "Order already confirmed")
        txn = str(params.get("vnp_TransactionNo") or "")
        if params.get("vnp_ResponseCode") == "00" and params.get("vnp_TransactionStatus") == "00":
            self._record(intent, txn, params)
        else:
            self._mark(intent["ref"], "failed", txn, params)
        return answer("00", "Confirm Success")

    # ------------------------------------------------------------------ #
    # MoMo

    async def _momo_create(self, ref: str, vnd: int, info: str, return_url: str, ipn_url: str) -> str:
        s = self.settings()
        c = s["momo"]
        shop = self.inv.settings()["shop_name"] or "Shop"
        fields = {
            "accessKey": c["access_key"],
            "amount": str(vnd),
            "extraData": "",
            "ipnUrl": ipn_url,
            "orderId": ref,
            "orderInfo": info,
            "partnerCode": c["partner_code"],
            "redirectUrl": return_url,
            "requestId": ref,
            "requestType": "payWithMethod",
        }
        raw = "&".join(f"{k}={fields[k]}" for k in sorted(fields))
        body = {
            "partnerCode": c["partner_code"],
            "partnerName": shop[:80],
            "storeId": _ascii(shop, 40) or "Shop",
            "requestId": ref,
            "amount": vnd,
            "orderId": ref,
            "orderInfo": info,
            "redirectUrl": return_url,
            "ipnUrl": ipn_url,
            "lang": "vi",
            "requestType": "payWithMethod",
            "autoCapture": True,
            "extraData": "",
            "signature": _hmac(self._secret("momo"), raw),
        }
        try:
            r = await self.office.http_client.post(
                URLS["momo"][0 if s["sandbox"] else 1], json=body, timeout=20
            )
            data = r.json()
        except (httpx2.HTTPError, ValueError) as e:  # network, or not JSON
            log.warning("payments: MoMo create failed: %s", e)
            raise InventoryError(tr("Không kết nối được với {0}, vui lòng thử lại", "MoMo")) from None
        if not isinstance(data, dict) or data.get("resultCode") != 0 or not data.get("payUrl"):
            log.warning("payments: MoMo refused %s: %s", ref, data)
            raise InventoryError(tr("{0} từ chối giao dịch: {1}", "MoMo", (data or {}).get("message", "?")))
        return str(data["payUrl"])

    def _momo_valid(self, params: dict[str, Any]) -> bool:
        keys = (
            "amount",
            "extraData",
            "message",
            "orderId",
            "orderInfo",
            "orderType",
            "partnerCode",
            "payType",
            "requestId",
            "responseTime",
            "resultCode",
            "transId",
        )
        raw = f"accessKey={self.settings()['momo']['access_key']}&" + "&".join(
            f"{k}={'' if params.get(k) is None else params.get(k)}" for k in keys
        )
        secret = self._secret("momo")
        given = str(params.get("signature") or "")
        return bool(secret and given) and _same(_hmac(secret, raw), given)

    def _momo_ipn(self, params: dict[str, Any]) -> tuple[int, None]:
        if not self._momo_valid(params):
            return 400, None
        intent = self.intent(str(params.get("orderId") or ""))
        if intent is None or intent["gateway"] != "momo":
            return 404, None
        if str(params.get("amount")) != str(int(self.inv.major(int(intent["amount"])))):
            log.warning("payments: MoMo %s amount %s differs", intent["ref"], params.get("amount"))
            return 400, None
        if intent["status"] == "pending":
            txn = str(params.get("transId") or "")
            if str(params.get("resultCode")) == "0":
                self._record(intent, txn, params)
            else:
                self._mark(intent["ref"], "failed", txn, params)
        return 204, None

    # ------------------------------------------------------------------ #
    # ZaloPay

    async def _zalopay_create(
        self, ref: str, vnd: int, info: str, return_url: str, ipn_url: str, order: dict[str, Any]
    ) -> str:
        s = self.settings()
        c = s["zalopay"]
        app_user = f"c{order['contact_id']}" if order.get("contact_id") else "guest"
        embed = json.dumps({"redirecturl": return_url}, separators=(",", ":"))
        app_time = int(time.time() * 1000)
        fields = {
            "app_id": c["app_id"],
            "app_user": app_user,
            "app_time": str(app_time),
            "amount": str(vnd),
            "app_trans_id": ref,
            "embed_data": embed,
            "item": "[]",
            "description": info,
            "bank_code": "",
            "callback_url": ipn_url,
        }
        fields["mac"] = _hmac(
            self._secret("zalopay", "key1_env"),
            f"{c['app_id']}|{ref}|{app_user}|{vnd}|{app_time}|{embed}|[]",
        )
        try:
            r = await self.office.http_client.post(
                URLS["zalopay"][0 if s["sandbox"] else 1], data=fields, timeout=20
            )
            data = r.json()
        except (httpx2.HTTPError, ValueError) as e:
            log.warning("payments: ZaloPay create failed: %s", e)
            raise InventoryError(tr("Không kết nối được với {0}, vui lòng thử lại", "ZaloPay")) from None
        if not isinstance(data, dict) or data.get("return_code") != 1 or not data.get("order_url"):
            log.warning("payments: ZaloPay refused %s: %s", ref, data)
            raise InventoryError(
                tr("{0} từ chối giao dịch: {1}", "ZaloPay", (data or {}).get("return_message", "?"))
            )
        return str(data["order_url"])

    def _zalopay_ipn(self, params: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        def answer(code: int, message: str) -> tuple[int, dict[str, Any]]:
            return 200, {"return_code": code, "return_message": message}

        data_str = params.get("data")
        given = str(params.get("mac") or "")
        key2 = self._secret("zalopay", "key2_env")
        if not isinstance(data_str, str) or not (key2 and given) or not _same(_hmac(key2, data_str), given):
            return answer(-1, "mac not equal")
        try:
            data = json.loads(data_str)
            assert isinstance(data, dict)
        except (ValueError, AssertionError):
            return answer(2, "bad data")
        intent = self.intent(str(data.get("app_trans_id") or ""))
        if intent is None or intent["gateway"] != "zalopay":
            return answer(2, "order not found")
        if str(data.get("amount")) != str(int(self.inv.major(int(intent["amount"])))):
            log.warning("payments: ZaloPay %s amount %s differs", intent["ref"], data.get("amount"))
            return answer(2, "amount mismatch")
        if intent["status"] == "pending":
            self._record(intent, str(data.get("zp_trans_id") or ""), data)
            return answer(1, "success")
        return answer(
            1 if intent["status"] == "paid" else 2, "success" if intent["status"] == "paid" else "closed"
        )

    def _zalopay_return_valid(self, params: dict[str, Any]) -> bool:
        """The redirect's checksum (key2 over appid|apptransid|pmcid|bankcode|amount|discountamount|status)."""
        key2 = self._secret("zalopay", "key2_env")
        given = str(params.get("checksum") or "")
        raw = "|".join(
            str(params.get(k) or "")
            for k in ("appid", "apptransid", "pmcid", "bankcode", "amount", "discountamount", "status")
        )
        return bool(key2 and given) and _same(_hmac(key2, raw), given)

    # ------------------------------------------------------------------ #
    # what the storefront calls

    async def handle_ipn(self, gateway: str, request_data: dict[str, Any]) -> tuple[int, Any]:
        """The gateway's notification: (HTTP status, JSON body or None), exactly as each
        gateway expects; the payment is recorded once whatever the retries."""
        if gateway == "vnpay":
            return self._vnpay_ipn(request_data)
        if gateway == "momo":
            return self._momo_ipn(request_data)
        if gateway == "zalopay":
            return self._zalopay_ipn(request_data)
        return 404, {"error": "unknown gateway"}

    def verify_return(self, gateway: str, params: dict[str, Any]) -> dict[str, Any] | None:
        """The intent the customer comes back for, with its current status (None when the
        parameters are not the gateway's). VNPay's and MoMo's results are signed and are
        recorded here too; ZaloPay's page only reports what its callback has recorded."""
        if gateway == "vnpay":
            if not self._vnpay_valid(params):
                return None
            intent = self.intent(str(params.get("vnp_TxnRef") or ""))
            ok = params.get("vnp_ResponseCode") == "00" and params.get("vnp_TransactionStatus") == "00"
            txn, amount = str(params.get("vnp_TransactionNo") or ""), str(params.get("vnp_Amount"))
            expected = lambda i: str(int(self.inv.major(int(i["amount"]))) * 100)
        elif gateway == "momo":
            if not self._momo_valid(params):
                return None
            intent = self.intent(str(params.get("orderId") or ""))
            ok = str(params.get("resultCode")) == "0"
            txn, amount = str(params.get("transId") or ""), str(params.get("amount"))
            expected = lambda i: str(int(self.inv.major(int(i["amount"]))))
        elif gateway == "zalopay":
            if not self._zalopay_return_valid(params):
                return None
            intent = self.intent(str(params.get("apptransid") or ""))
            return self._with_order(intent) if intent else None
        else:
            return None
        if intent is None or intent["gateway"] != gateway:
            return None
        if intent["status"] == "pending" and amount == expected(intent):
            if ok:
                self._record(intent, txn, params)
            else:
                self._mark(intent["ref"], "failed", txn, params)
            intent = self.intent(intent["ref"])
        return self._with_order(intent)

    def _with_order(self, intent: dict[str, Any] | None) -> dict[str, Any] | None:
        if intent is None:
            return None
        out = {
            **intent,
            "code": order_code(int(intent["order_id"])),
            "amount": self.inv.major(int(intent["amount"])),
        }
        out.pop("raw", None)
        return out

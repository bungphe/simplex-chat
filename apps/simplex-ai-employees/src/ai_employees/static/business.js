"use strict";
// Point of sale, delivery, marketing and reports. Uses the helpers of admin.js and
// inventory.js (h, api, run, put, render, views, toast, go, me, tile, money, pillOf...).

const PAY = { cash: tr("Tiền mặt"), card: tr("Thẻ"), transfer: tr("Chuyển khoản"), wallet: tr("Ví (MoMo, ZaloPay, VNPay)"), cod: tr("Thu hộ (COD)"), other: tr("Khác"), refund: tr("Hoàn tiền") };
const PAY_STATUS = { unpaid: ["bad", tr("chưa trả")], partial: ["warn", tr("đã cọc")], paid: ["ok", tr("đã trả đủ")], refunded: ["neutral", tr("đã hoàn tiền")] };
const ORDER_ST = { confirmed: ["warn", tr("đã xác nhận")], completed: ["ok", tr("đã giao")], cancelled: ["neutral", tr("đã huỷ")], returned: ["neutral", tr("đã trả hàng")] };
const BOOK_ST = { booked: ["neutral", tr("chờ xếp xe")], assigned: ["warn", tr("đã xếp chuyến")], shipping: ["warn", tr("đang giao")], done: ["ok", tr("đã giao")], comeback: ["bad", tr("quay về")], cancelled: ["neutral", tr("đã huỷ")] };
const ROUTE_ST = { planned: ["neutral", tr("chưa chạy")], in_progress: ["warn", tr("đang chạy")], completed: ["ok", tr("xong")], cancelled: ["neutral", tr("đã huỷ")] };
const isManager = () => me && (me.role === "admin" || me.role === "manager");
const store = { get: (k, d) => { try { return JSON.parse(localStorage.getItem(k)) ?? d; } catch (_) { return d; } }, set: (k, v) => { try { localStorage.setItem(k, JSON.stringify(v)); } catch (_) { /* private mode */ } } };
// YYYY-MM-DD of the staff member's own calendar (toISOString would give the UTC date)
const localDate = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
const today = () => localDate(new Date());
const moneyOf = (cur) => (n) => (n === null || n === undefined ? "—" : `${Number(n).toLocaleString(LOCALE)} ${cur === "VND" ? tr("đ") : cur}`);
// The shop's currency comes with /api/inventory (invMeta, once the Kho hàng page has loaded).
// Roles that may read it fetch it once; the others (delivery) show amounts in VND.
let currencyLoad = null;
async function shopCurrency() {
  const canRead = me && (me.role === "admin" || ["manager", "warehouse", "marketing"].includes(me.role));
  if (!invMeta.today && canRead) {
    currencyLoad = currencyLoad || api("GET", "/api/inventory", undefined, { keep: true })
      .then((d) => { if (!invMeta.today) invMeta = d; })
      .catch(() => { currencyLoad = null; });
    await currencyLoad;
  }
  return invMeta.settings.currency || "VND";
}
// A button that posts money or stock: disabled while its request runs, so a double click
// cannot send it twice.
const once = (fn) => async (ev) => {
  const btn = ev && ev.currentTarget;
  if (btn) { if (btn.disabled) return; btn.disabled = true; }
  try { await fn(); } finally { if (btn) btn.disabled = false; }
};

// ---------------------------------------------------------------- start-of-day notices

async function showNotices() {
  let r;
  try { r = await api("GET", "/api/notices", undefined, { keep: true }); } catch (_) { return; }
  for (const n of r.pending) {
    const box = h("div", { class: "modal" }, h("div", { class: "card modal-card" },
      h("h2", {}, "📢 ", n.title), h("p", { class: "pre" }, n.body), h("p", { class: "muted" }, `${n.created_by} · ${fmtTime(n.created)}`),
      h("div", { class: "row" },
        h("button", { class: "primary", onclick: () => run(async () => { await api("POST", `/api/notices/${n.id}/done`, {}); box.remove(); }) }, tr("Đã xác nhận / xử lý")),
        h("button", { onclick: () => run(async () => { await api("POST", `/api/notices/${n.id}/skip`, {}); box.remove(); }) }, tr("Bỏ qua hôm nay")))));
    document.body.append(box);
  }
}

// ---------------------------------------------------------------- point of sale

// Several bills open at once (multi-tab billing); kept in this browser, for each account,
// until paid or the staff member logs out.
const posKeys = () => [`pos-tabs:${me.username}`, `pos-active:${me.username}`];
let posTabs = [];
let posActive = 1;
let posUser = null;
const loadTabs = () => {
  if (posUser === me.username) return;
  posUser = me.username;
  try { localStorage.removeItem("pos-tabs"); localStorage.removeItem("pos-active"); } catch (_) { /* shared by every account: older versions */ }
  const [tabsKey, activeKey] = posKeys();
  posTabs = store.get(tabsKey, [{ id: 1, name: tr("Đơn 1"), lines: [], contact: null, kind: "now", voucher: "", order: null }]);
  posActive = store.get(activeKey, 1);
};
const saveTabs = () => { const [tabsKey, activeKey] = posKeys(); store.set(tabsKey, posTabs); store.set(activeKey, posActive); };
// on logout (admin.js): the next person on this browser must not see these bills
function clearPosDrafts() {
  if (!me) return;
  try { for (const k of posKeys()) localStorage.removeItem(k); } catch (_) { /* private mode */ }
  posUser = null;
  posTabs = [];
}

views.pos = async () => {
  loadTabs();
  const search = h("input", { type: "search", placeholder: tr("Tìm sản phẩm: tên, SKU (F2)") });
  const results = h("div", { class: "pos-results" });
  const cart = h("div", { class: "card pos-cart" });
  const mine = h("div", { class: "card section table-wrap" });  // scrolls sideways on phones
  let meta = { warehouses: [], combos: [], currency: "VND" };
  let m = moneyOf("VND");
  const tab = () => posTabs.find((t) => t.id === posActive) || posTabs[0];

  const loadProducts = async () => {
    const t = tab();
    const q = new URLSearchParams({ q: search.value.trim(), ...(t.contact ? { contact: t.contact.id } : {}) });
    meta = await api("GET", `/api/pos/products?${q}`);
    m = moneyOf(meta.currency);
    put(results,
      meta.products.map((p) => h("button", { class: "pos-item", onclick: () => addLine(p) },
        h("div", { class: "row spread" }, h("b", {}, p.name), h("span", { class: "mono muted" }, p.sku)),
        h("div", { class: "row" }, h("b", {}, m(p.price)), p.price !== p.list_price && p.list_price ? h("s", { class: "muted" }, m(p.list_price)) : null,
          p.promo ? h("span", { class: "pill warn" }, p.promo) : null, p.vip ? h("span", { class: "pill ok" }, "VIP") : null),
        h("div", { class: "muted" }, p.available > 0 ? tr("Còn {0}", p.available) : tr("Hết hàng"),
          p.incoming.length ? tr(
            " · sắp về {0}",
            p.incoming.map((x) => `${x.qty - x.preordered} (${x.eta || "?"})`).join(", ")
          ) : ""))),
      meta.combos.length ? h("div", { class: "pos-combos" }, h("h3", {}, "Combo"), meta.combos.map((c) => h("button", { class: "pos-item", onclick: () => addCombo(c) },
        h("b", {}, c.name), h("div", {}, m(c.price), " ", h("span", { class: "muted" }, tr("tiết kiệm {0} · còn {1} bộ", m(c.saving), c.available)))))) : null);
  };
  const addLine = (p) => {
    const t = tab();
    if (t.order) { toast(tr("Đơn này đã tạo: mở đơn mới")); return; }
    const same = t.lines.find((l) => l.product_id === p.id && !l.combo);
    if (same) same.qty += 1;
    else t.lines.push({ product_id: p.id, sku: p.sku, name: p.name, price: p.price, qty: 1, discount_pct: "", unit_price: "", incoming: p.incoming });
    saveTabs();
    drawCart();
  };
  const addCombo = (c) => {
    const t = tab();
    if (t.order) return;
    t.lines.push({ combo: c.code, name: `Combo ${c.name}`, price: c.price, qty: 1 });
    saveTabs();
    drawCart();
  };

  const drawTabs = () => h("div", { class: "tabs" }, posTabs.map((t) => h("button", {
    class: t.id === posActive ? "active" : "", onclick: () => { posActive = t.id; saveTabs(); drawCart(); run(loadProducts); },
  }, t.order ? `${t.name} · ${t.order.code}` : t.name, t.lines.length && !t.order ? ` (${t.lines.length})` : "")),
  h("button", { onclick: () => { const id = Math.max(0, ...posTabs.map((x) => x.id)) + 1; posTabs.push({ id, name: tr("Đơn {0}", id), lines: [], contact: null, kind: "now", voucher: "", order: null }); posActive = id; saveTabs(); drawCart(); } }, tr("+ Đơn mới")));

  const closeTab = () => {
    posTabs = posTabs.filter((x) => x.id !== posActive);
    if (!posTabs.length) posTabs = [{ id: 1, name: tr("Đơn 1"), lines: [], contact: null, kind: "now", voucher: "", order: null }];
    posActive = posTabs[0].id;
    saveTabs();
    drawCart();
    run(loadMine);
  };

  const customerBox = (t) => {
    const q = h("input", { type: "search", placeholder: tr("SĐT hoặc tên khách") });
    const out = h("div", {});
    q.addEventListener("change", () => run(async () => {
      const r = await api("GET", `/api/pos/customers?q=${encodeURIComponent(q.value.trim())}`);
      put(out, r.customers.map((c) => h("button", { class: "small", onclick: () => { t.contact = c; saveTabs(); drawCart(); run(loadProducts); } },
        tr(
          "{0} · {1}{2} · {3} điểm",
          c.name || "?",
          c.phone || "",
          c.vip ? " ⭐VIP" : "",
          c.points
        ))),
      h("button", { class: "small", onclick: () => run(async () => {
        const phone = /^[\d+ .-]+$/.test(q.value.trim()) ? q.value.trim() : "";
        const name = prompt(tr("Tên khách:"), phone ? "" : q.value.trim());
        if (name === null) return;
        t.contact = await api("POST", "/api/pos/customers", { name, phone });
        saveTabs(); drawCart();
      }, tr("Đã thêm khách")) }, tr("+ Khách mới")));
    }));
    return t.contact
      ? h("div", { class: "row" }, h("b", {}, t.contact.name || t.contact.phone), t.contact.vip ? h("span", { class: "pill ok" }, "VIP") : null,
        h("span", { class: "muted" }, tr("{0} · {1} điểm", t.contact.phone || "", t.contact.points || 0)),
        !t.order ? h("button", { class: "small", onclick: () => { t.contact = null; saveTabs(); drawCart(); run(loadProducts); } }, tr("Đổi")) : null)
      : h("div", {}, q, out);
  };

  const drawCart = () => {
    const t = tab();
    if (t.order) return drawPayment(t);
    const total = t.lines.reduce((a, l) => a + (Number(l.unit_price) || (l.price || 0) * (1 - (Number(l.discount_pct) || 0) / 100)) * l.qty, 0);
    const kind = h("select", {}, h("option", { value: "now", selected: t.kind === "now" }, tr("Lấy hàng ngay")), h("option", { value: "preorder", selected: t.kind === "preorder" }, tr("Đặt trước (hàng sắp về)")));
    kind.addEventListener("change", () => { t.kind = kind.value; saveTabs(); });
    const wh = h("select", {}, meta.warehouses.map((w) => h("option", { value: w.id, selected: w.id === t.warehouse_id }, w.name)));
    wh.addEventListener("change", () => { t.warehouse_id = Number(wh.value); saveTabs(); });
    const voucher = h("input", { value: t.voucher || "", placeholder: tr("Mã voucher") });
    voucher.addEventListener("change", () => { t.voucher = voucher.value.trim(); saveTabs(); });
    const discount = h("input", { value: t.discount || "", placeholder: tr("Giảm thêm (số tiền)"), class: "narrow" });
    discount.addEventListener("change", () => { t.discount = discount.value; saveTabs(); });
    const create = () => run(async () => {
      if (t.order) return;
      if (!t.lines.length) throw new Error(tr("Chưa có sản phẩm"));
      const items = t.lines.map((l) => l.combo ? { combo: l.combo, qty: l.qty }
        : { product_id: l.product_id, qty: l.qty, ...(l.unit_price ? { unit_price: l.unit_price } : {}), ...(l.discount_pct ? { discount_pct: l.discount_pct } : {}) });
      t.order = await api("POST", "/api/pos/orders", { items, kind: t.kind, warehouse_id: t.warehouse_id || (meta.warehouses[0] || {}).id,
        contact_id: t.contact ? t.contact.id : null, voucher: t.voucher, discount: t.discount || 0 });
      saveTabs();
      drawCart();
      run(loadMine);
    }, tr("Đã tạo đơn, hàng đã được giữ"));
    put(cart, drawTabs(),
      h("h3", {}, tr("Khách hàng")), customerBox(t),
      h("h3", {}, tr("Sản phẩm")),
      t.lines.length ? h("table", {}, h("tbody", {}, t.lines.map((l, i) => {
        const qty = h("input", { value: l.qty, class: "narrow" });
        qty.addEventListener("change", () => { l.qty = Math.max(1, parseInt(qty.value, 10) || 1); saveTabs(); drawCart(); });
        const pct = h("input", { value: l.discount_pct, class: "narrow", placeholder: tr("giảm %") });
        const special = h("input", { value: l.unit_price, class: "price", placeholder: tr("giá riêng") });
        // two-way: a special price shows its % off, a % off shows the price
        pct.addEventListener("change", () => { l.discount_pct = pct.value; l.unit_price = ""; saveTabs(); drawCart(); });
        special.addEventListener("change", () => { l.unit_price = special.value; l.discount_pct = ""; saveTabs(); drawCart(); });
        const eff = Number(l.unit_price) || (l.price || 0) * (1 - (Number(l.discount_pct) || 0) / 100);
        return h("tr", {}, h("td", {}, h("b", {}, l.name), h("div", { class: "muted" }, l.sku || l.combo || "",
          l.unit_price && l.price ? tr(" · giảm {0}%", Math.round(100 - (100 * l.unit_price) / l.price)) : "")),
        h("td", {}, qty), h("td", {}, l.combo ? "" : h("div", { class: "row" }, pct, special)), h("td", {}, m(eff * l.qty)),
        h("td", {}, h("button", { class: "small danger", onclick: () => { t.lines.splice(i, 1); saveTabs(); drawCart(); } }, "✕")));
      }))) : h("p", { class: "muted" }, tr("Bấm sản phẩm bên trái để thêm.")),
      h("div", { class: "two" }, field(tr("Hình thức"), kind), field(tr("Kho xuất"), wh)),
      h("div", { class: "row" }, voucher, discount),
      h("div", { class: "row spread" }, h("span", { class: "pos-total" }, tr("Tạm tính: {0}", m(Math.round(total)))),
        h("div", { class: "row" }, h("button", { class: "danger", onclick: () => confirm(tr("Bỏ đơn này?")) && closeTab() }, tr("Bỏ")),
          h("button", { class: "primary", onclick: once(create) }, tr("Tạo đơn")))));
  };

  const drawPayment = (t) => run(async () => {
    const o = await api("GET", `/api/pos/orders/${t.order.id}`);
    t.order = o;
    saveTabs();
    const method = h("select", {}, Object.entries(PAY).filter(([k]) => k !== "refund").map(([k, v]) => h("option", { value: k }, v)));
    const amount = h("input", { value: o.due || "", class: "price", placeholder: tr("số tiền") });
    const tendered = h("input", { class: "price", placeholder: tr("khách đưa (tiền mặt)") });
    const change = h("b", {});
    const showChange = () => { const d = Number(tendered.value) - Number(amount.value || o.due); change.textContent = tendered.value && d >= 0 ? tr("Tiền thối: {0}", m(d)) : ""; };
    tendered.addEventListener("input", showChange);
    amount.addEventListener("input", showChange);
    // one key per payment form: a retry after a lost answer is not recorded twice;
    // the form drawn after a successful payment has a new key
    const payKey = `${o.id}-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
    const pay = () => run(async () => {
      await api("POST", `/api/pos/orders/${o.id}/payments`, { method: method.value, amount: amount.value || null,
        tendered: method.value === "cash" && tendered.value ? tendered.value : null, idempotency_key: payKey });
      drawPayment(t);
    }, tr("Đã ghi nhận thanh toán"));
    const step = (s, msg, body) => run(async () => { await api("POST", `/api/pos/orders/${o.id}/${s}`, body || {}); drawPayment(t); run(loadMine); }, msg);
    put(cart, drawTabs(),
      h("div", { class: "row spread" }, h("h2", {}, o.code), h("div", { class: "row" }, pillOf(ORDER_ST, o.status), pillOf(PAY_STATUS, o.payment_status))),
      h("p", { class: "muted" }, `${o.customer_name || tr("Khách lẻ")} ${o.phone || ""} · ${o.kind === "preorder" ? tr("đặt trước") : tr("lấy ngay")}`),
      h("table", {}, h("tbody", {}, o.items.map((i) => h("tr", {}, h("td", {}, i.name, i.promo ? h("span", { class: "pill warn" }, i.promo) : null,
        i.status === "awaiting" ? h("div", { class: "muted" }, tr("chờ hàng về {0}", i.eta || "")) : null), h("td", {}, `x${i.qty}`), h("td", {}, m(i.line_total)))))),
      o.discount || o.voucher_discount ? h("p", {}, tr(
        "Giảm giá: -{0}{1}",
        m(o.discount + o.voucher_discount),
        o.voucher ? ` (voucher ${o.voucher})` : ""
      )) : null,
      o.shipping_fee ? h("p", {}, tr("Phí giao hàng: {0}", m(o.shipping_fee))) : null,
      h("p", { class: "pos-total" }, tr("Tổng: {0} · đã trả {1} · còn {2}", m(o.total), m(o.paid), m(o.due))),
      o.payments.length ? h("p", { class: "muted" }, o.payments.map((p) => `${PAY[p.method] || p.method} ${m(p.amount)}${p.change ? tr(" (thối {0})", m(p.change)) : ""}`).join(" · ")) : null,
      o.due && ["confirmed", "completed"].includes(o.status) ? h("div", { class: "section" },
        h("div", { class: "row" }, method, amount, tendered, change),
        o.change_hints.length ? h("div", { class: "row" }, h("span", { class: "muted" }, tr("Khách đưa:")), o.change_hints.map((x) =>
          h("button", { class: "small", onclick: () => { method.value = "cash"; tendered.value = x; showChange(); } }, m(x)))) : null,
        h("button", { class: "primary", onclick: once(pay) }, tr("Thu tiền"))) : null,
      h("div", { class: "row section" },
        o.status === "confirmed" && !o.items.some((i) => i.status === "awaiting") ? h("button", { class: "primary", onclick: once(() => step("complete", tr("Đã giao hàng, trừ kho"))) }, tr("Đã giao (xuất kho)")) : null,
        h("a", { class: "button", href: `/api/pos/orders/${o.id}/receipt`, target: "_blank", rel: "noopener" }, tr("In hoá đơn")),
        o.contact_id || o.conversation_id ? h("button", { onclick: () => step("send-receipt", tr("Đã gửi hoá đơn cho khách")) }, tr("Gửi hoá đơn qua chat")) : null,
        h("button", { onclick: () => { const to = prompt(tr("Gửi hoá đơn tới email (để trống: email của khách):"), o.email || ""); if (to !== null) run(async () => {
          const r = await api("POST", `/api/pos/orders/${o.id}/email-invoice`, { to }); toast(tr("Đã gửi hoá đơn tới {0}", r.sent_to)); drawPayment(t); }); } }, tr("Gửi hoá đơn qua email")),
        // the customer's private link to pay online (copied to send by any channel)
        o.due && o.status === "confirmed" ? h("button", { onclick: () => run(async () => {
          const r = await api("POST", `/api/pos/orders/${o.id}/paylink`, {});
          if (!r.payable) toast(tr("Chưa bật cổng thanh toán online (Kho hàng → Cửa hàng & tích điểm)"));
          let copied = false;
          try { await navigator.clipboard.writeText(r.url); copied = true; } catch (_) { /* no clipboard: the prompt below */ }
          if (copied) toast(tr("Đã sao chép link thanh toán")); prompt(tr("Link thanh toán của khách:"), r.url); }) }, tr("Link thanh toán")) : null,
        isManager() && o.status === "confirmed" ? h("button", { class: "danger", onclick: once(() => confirm(tr("Huỷ {0}?", o.code)) && step("cancel", tr("Đã huỷ"))) }, tr("Huỷ đơn")) : null,
        isManager() && o.status === "completed" ? h("button", { class: "danger", onclick: once(() => { const why = prompt(tr("Lý do trả hàng:")); return why !== null && step("return", tr("Đã nhận trả hàng"), { reason: why }); }) }, tr("Trả hàng / hoàn tác")) : null,
        h("button", { onclick: closeTab }, tr("Xong, đóng đơn"))));
  });

  const loadMine = async () => {
    const r = await api("GET", "/api/pos/orders");
    put(mine, h("h2", {}, isManager() ? tr("Đơn gần đây") : tr("Đơn của tôi hôm nay")),
      r.orders.length ? h("table", {}, h("tbody", {}, r.orders.slice(0, 50).map((o) => h("tr", { class: "clickable", onclick: () => {
        let t = posTabs.find((x) => x.order && x.order.id === o.id);
        if (!t) { const id = Math.max(0, ...posTabs.map((x) => x.id)) + 1; t = { id, name: tr("Đơn {0}", id), lines: [], contact: null, kind: o.kind, order: o }; posTabs.push(t); }
        posActive = t.id; saveTabs(); drawCart();
      } }, h("td", { class: "mono" }, o.code), h("td", {}, fmtTime(o.created)), h("td", {}, o.customer_name || tr("Khách lẻ")), h("td", {}, m(o.total)),
      h("td", {}, pillOf(ORDER_ST, o.status)), h("td", {}, pillOf(PAY_STATUS, o.payment_status)))))) : h("p", { class: "muted" }, tr("Chưa có đơn.")));
  };

  // room sets within a budget
  const setTemplate = h("select", {});
  const setBudget = h("input", { placeholder: tr("ngân sách"), class: "price" });
  const setOut = h("div", {});
  const suggest = () => run(async () => {
    const t = tab();
    const r = await api("GET", `/api/pos/sets?template=${encodeURIComponent(setTemplate.value)}&budget=${encodeURIComponent(setBudget.value || 0)}${t.contact ? `&contact=${t.contact.id}` : ""}`);
    put(setOut, r.missing.length ? h("p", { class: "muted" }, tr("Chưa có hàng cho: {0}", r.missing.join(", ")))
      : r.sets.length ? r.sets.map((s, n) => h("div", { class: "card section" }, h("div", { class: "row spread" }, h("b", {}, tr("Gợi ý {0}: {1}", n + 1, m(s.total))),
        h("button", { class: "small", onclick: () => { for (const i of s.items) addLine({ id: i.product_id, sku: i.sku, name: i.name, price: i.unit_price, incoming: [] }); for (const i of s.items) { const l = tab().lines.find((x) => x.product_id === i.product_id); if (l) l.qty = i.qty; } saveTabs(); drawCart(); } }, tr("Thêm vào đơn"))),
      h("div", { class: "muted" }, s.items.map((i) => `${i.name} x${i.qty}${i.ready === "now" ? "" : tr(" (về {0})", i.ready)}`).join(" · "))))
        : h("p", { class: "muted" }, tr("Không bộ nào vừa ngân sách; thấp nhất {0}.", m(r.cheapest))));
  });
  let timer;
  search.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(() => run(loadProducts), 250); });
  document.onkeydown = (ev) => { if (ev.key === "F2" && current === "pos") { ev.preventDefault(); search.focus(); } };
  render(h("div", { class: "row spread" }, h("h1", {}, tr("Bán hàng tại quầy")), h("span", { class: "muted" }, tr("Nhiều đơn cùng lúc · F2 để tìm"))),
    h("div", { class: "pos" }, h("div", {}, search, results,
      h("details", { class: "card section" }, h("summary", {}, tr("Gợi ý bộ sản phẩm theo ngân sách")), h("div", { class: "row" }, setTemplate, setBudget, h("button", { onclick: suggest }, tr("Gợi ý"))), setOut)),
    cart), mine);
  const tmpl = await api("GET", "/api/pos/sets").catch(() => ({ templates: [] }));
  put(setTemplate, tmpl.templates.map((t) => h("option", { value: t.id }, t.name)));
  await run(loadProducts);
  drawCart();
  await run(loadMine);
};

// ---------------------------------------------------------------- delivery

let dlDate = today();
views.delivery = async (arg) => {
  if (arg) dlDate = arg;
  const d = await run(() => api("GET", `/api/delivery?date=${dlDate}&month=${dlDate.slice(0, 7)}`));
  if (!d) return;
  const m = moneyOf(await shopCurrency());
  const reload = (day) => go("delivery", day || dlDate);
  // month calendar
  const first = new Date(`${d.month}-01T00:00:00`);
  const daysIn = new Date(first.getFullYear(), first.getMonth() + 1, 0).getDate();
  const byDay = Object.fromEntries(d.calendar.map((x) => [x.date, x]));
  const cells = [];
  for (let i = 0; i < (first.getDay() + 6) % 7; i++) cells.push(h("div", { class: "cal-cell empty" }));
  for (let n = 1; n <= daysIn; n++) {
    const day = `${d.month}-${String(n).padStart(2, "0")}`;
    const info = byDay[day];
    cells.push(h("button", { class: "cal-cell" + (day === dlDate ? " active" : ""), onclick: () => reload(day) },
      h("b", {}, n), info ? h("div", {}, tr("{0} đơn · {1} kiện", info.bookings, info.boxes), info.special ? h("span", { class: "pill warn" }, `⚠ ${info.special}`) : null) : null));
  }
  const shift = (months) => reload(localDate(new Date(first.getFullYear(), first.getMonth() + months, 1)));
  const calendar = h("div", { class: "card section" },
    h("div", { class: "row spread" }, h("button", { onclick: () => shift(-1) }, "‹"), h("h2", {}, tr("Lịch giao tháng {0}", d.month)), h("button", { onclick: () => shift(1) }, "›")),
    h("div", { class: "cal" }, [tr("T2"), tr("T3"), tr("T4"), tr("T5"), tr("T6"), tr("T7"), tr("CN")].map((x) => h("div", { class: "cal-head" }, x)), cells));

  // book an order
  const f = { order: h("select", {}, d.open_orders.map((o) => h("option", { value: o.id }, `${o.code} · ${o.customer_name || "?"} · ${m(o.total)}`))),
    date: h("input", { type: "date", value: dlDate }), slot: h("select", {}, Object.entries(d.slots).map(([k, v]) => h("option", { value: k }, v))),
    window: h("input", { placeholder: tr("giờ cố định, vd. 15-16h") }), address: h("input", { placeholder: tr("địa chỉ (để trống: theo đơn)") }),
    floors: h("input", { value: 0, class: "narrow" }), assembling: h("input", { type: "checkbox" }), notes: h("input", { placeholder: tr("ghi chú") }),
    lat: h("input", { placeholder: tr("vĩ độ"), class: "narrow" }), lng: h("input", { placeholder: tr("kinh độ"), class: "narrow" }) };
  const book = () => run(async () => {
    await api("POST", "/api/delivery/bookings", { order_id: Number(f.order.value), delivery_date: f.date.value, slot: f.slot.value, window: f.window.value,
      address: f.address.value, floors: f.floors.value, assembling: f.assembling.checked, notes: f.notes.value, lat: f.lat.value, lng: f.lng.value });
    reload(f.date.value);
  }, tr("Đã đặt lịch giao"));

  // the day's bookings and trips
  const pick = new Set();
  const booked = d.bookings.filter((b) => b.status === "booked");
  const carrierSel = h("select", {}, d.carriers.filter((c) => c.active).map((c) => h("option", { value: c.id }, c.name)));
  const driverSel = h("select", {}, h("option", { value: "" }, tr("— tài xế —")), d.drivers.filter((x) => x.active).map((x) => h("option", { value: x.id }, `${x.name} (${x.carrier_name})`)));
  const originSel = h("select", {}, d.warehouses.map((w) => h("option", { value: w.id }, w.name)));
  const makeRoute = () => run(async () => {
    await api("POST", "/api/delivery/routes", { delivery_date: dlDate, carrier_id: Number(carrierSel.value), driver_id: driverSel.value ? Number(driverSel.value) : null,
      origin_wh: Number(originSel.value), booking_ids: [...pick] });
    reload();
  }, tr("Đã tạo chuyến"));
  const bookingRow = (b, extra) => h("tr", {},
    extra, h("td", {}, h("b", {}, b.customer_name), h("div", { class: "muted" }, `${b.phone} · ${b.address}`)),
    h("td", {}, h("span", { class: "mono" }, b.order_code), h("div", { class: "muted" }, b.items.map((i) => `${i.name} x${i.qty}`).join(", "))),
    h("td", {}, b.slot_name, b.window ? h("div", { class: "pill warn" }, b.window) : null),
    h("td", {}, tr("{0} kiện", b.boxes), b.assembling ? h("div", { class: "pill warn" }, tr("Lắp ráp")) : null, b.floors ? h("div", { class: "pill warn" }, tr("Vác {0} tầng", b.floors)) : null),
    h("td", {}, b.surcharge ? m(b.surcharge) : "", b.due ? h("div", { class: "muted" }, tr("thu {0}", m(b.due))) : null), h("td", {}, pillOf(BOOK_ST, b.status)),
    h("td", {}, h("div", { class: "row" },
      !b.lat && d.settings.maps ? h("button", { class: "small", onclick: () => run(async () => { await api("POST", `/api/delivery/bookings/${b.id}/geocode`, {}); reload(); }, tr("Đã tìm toạ độ")) }, tr("Tìm toạ độ")) : null,
      ["booked", "assigned"].includes(b.status) ? h("button", { class: "small danger", onclick: () => confirm(tr("Huỷ lịch giao?")) && run(async () => { await api("POST", `/api/delivery/bookings/${b.id}/cancel`, {}); reload(); }) }, tr("Huỷ")) : null)));
  const routeCard = (r) => {
    const act = (step, body, msg) => run(async () => { await api("POST", `/api/delivery/routes/${r.id}/${step}`, body || {}); reload(); }, msg);
    const move = (i, dir) => { const ids = r.stops.map((s) => s.booking_id); [ids[i], ids[i + dir]] = [ids[i + dir], ids[i]]; act("reorder", { booking_ids: ids }); };
    return h("div", { class: "card section" },
      h("div", { class: "row spread" }, h("h3", {}, `${r.code} · ${r.carrier_name}${r.driver ? ` · ${r.driver.name} ${r.driver.vehicle}` : ""}`), pillOf(ROUTE_ST, r.status)),
      h("p", { class: "muted" }, tr(
        "Từ {0} lúc {1} · {2} điểm · {3} kiện · {4} km · {5} phút · chi phí {6}{7}",
        r.origin,
        r.start_time,
        r.stops.length,
        r.boxes,
        r.distance_km,
        r.duration_min,
        m(r.cost),
        r.optimized_by ? tr(
          " · xếp theo {0}",
          r.optimized_by === "google" ? "Google Maps" : r.optimized_by === "manual" ? tr("tay") : tr("khoảng cách")
        ) : ""
      )),
      h("table", {}, h("tbody", {}, r.stops.map((s, i) => bookingRow(s.booking, h("td", {}, h("b", {}, `${s.seq}`), h("div", { class: "muted" }, s.eta),
        r.status === "planned" ? h("div", { class: "row" }, i ? h("button", { class: "small", onclick: () => move(i, -1) }, "↑") : null, i < r.stops.length - 1 ? h("button", { class: "small", onclick: () => move(i, 1) }, "↓") : null) : null,
        r.status === "in_progress" && s.status === "pending" ? h("div", { class: "row" },
          h("button", { class: "small primary", onclick: () => act("result", { booking_id: s.booking_id, result: "done" }, tr("Đã giao")) }, tr("Đã giao")),
          h("button", { class: "small danger", onclick: () => { const why = prompt(tr("Lý do quay về:")); if (why !== null) act("result", { booking_id: s.booking_id, result: "comeback", note: why }, tr("Đã ghi quay về")); } }, tr("Quay về"))) : null))))),
      h("div", { class: "row" },
        r.status === "planned" ? h("button", { onclick: () => act("optimize", {}, tr("Đã sắp xếp đường ngắn nhất")) }, tr("Tối ưu lộ trình")) : null,
        r.status === "planned" ? h("button", { class: "primary", onclick: () => act("start", {}, tr("Xe đã chạy; khách được báo")) }, tr("Xuất phát")) : null,
        h("a", { class: "button", href: `/api/delivery/routes/${r.id}/list.csv` }, tr("Phiếu giao hàng (CSV)")),
        r.status === "planned" ? h("button", { class: "danger", onclick: () => confirm(tr("Huỷ chuyến?")) && act("cancel") }, tr("Huỷ chuyến")) : null));
  };
  const day = h("div", { class: "card section" }, h("h2", {}, tr("Ngày {0}", dlDate)),
    booked.length ? h("div", {}, h("h3", {}, tr("Chờ xếp chuyến")), h("table", {}, h("tbody", {}, booked.map((b) => {
      const box = h("input", { type: "checkbox" });
      box.addEventListener("change", () => (box.checked ? pick.add(b.id) : pick.delete(b.id)));
      return bookingRow(b, h("td", {}, box));
    }))), h("div", { class: "row" }, carrierSel, driverSel, originSel, h("button", { class: "primary", onclick: makeRoute }, tr("Tạo chuyến với các đơn đã chọn")))) : h("p", { class: "muted" }, tr("Không có đơn chờ xếp chuyến.")),
    d.routes.map(routeCard),
    d.bookings.filter((b) => !["booked"].includes(b.status) && !d.routes.some((r) => r.stops.some((s) => s.booking_id === b.id))).length
      ? h("table", {}, h("tbody", {}, d.bookings.filter((b) => !["booked"].includes(b.status) && !d.routes.some((r) => r.stops.some((s) => s.booking_id === b.id))).map((b) => bookingRow(b, h("td", {}))))) : null);

  // carriers, drivers, fees
  const c = { code: h("input", { placeholder: tr("mã") }), name: h("input", { placeholder: tr("tên đơn vị") }), phone: h("input", { placeholder: tr("điện thoại") }),
    trip: h("input", { placeholder: tr("giá / chuyến"), class: "price" }), stop: h("input", { placeholder: tr("giá / điểm"), class: "price" }), internal: h("input", { type: "checkbox" }) };
  const dr = { carrier: h("select", {}, d.carriers.map((x) => h("option", { value: x.id }, x.name))), name: h("input", { placeholder: tr("tên tài xế") }),
    phone: h("input", { placeholder: tr("điện thoại") }), vehicle: h("input", { placeholder: tr("xe, biển số") }) };
  const fees = { floor_fee: h("input", { value: d.settings.floor_fee, class: "price" }), assembly_fee: h("input", { value: d.settings.assembly_fee, class: "price" }) };
  const setup = h("details", { class: "card section" }, h("summary", {}, tr("Đơn vị vận chuyển, tài xế, phụ phí, toạ độ kho")),
    h("table", {}, h("tbody", {}, d.carriers.map((x) => h("tr", {}, h("td", {}, h("b", {}, x.name), ` ${x.code}${x.internal ? tr(" (xe nhà)") : ""}`), h("td", {}, tr("{0}/chuyến · {1}/điểm", m(x.rate_per_trip), m(x.rate_per_stop))))))),
    h("div", { class: "row" }, c.code, c.name, c.phone, c.trip, c.stop, h("label", { class: "check" }, c.internal, h("span", {}, tr("xe nhà"))),
      h("button", { onclick: () => run(async () => { await api("POST", "/api/delivery/carriers", { code: c.code.value, name: c.name.value, phone: c.phone.value, rate_per_trip: c.trip.value || 0, rate_per_stop: c.stop.value || 0, internal: c.internal.checked }); reload(); }, tr("Đã thêm")) }, tr("Thêm đơn vị"))),
    h("table", {}, h("tbody", {}, d.drivers.map((x) => h("tr", {}, h("td", {}, h("b", {}, x.name), ` ${x.phone}`), h("td", {}, x.vehicle), h("td", {}, x.carrier_name))))),
    d.carriers.length ? h("div", { class: "row" }, dr.carrier, dr.name, dr.phone, dr.vehicle,
      h("button", { onclick: () => run(async () => { await api("POST", "/api/delivery/drivers", { carrier_id: Number(dr.carrier.value), name: dr.name.value, phone: dr.phone.value, vehicle: dr.vehicle.value }); reload(); }, tr("Đã thêm")) }, tr("Thêm tài xế"))) : null,
    h("div", { class: "row" }, field(tr("Phí vác lầu / tầng"), fees.floor_fee), field(tr("Phí lắp ráp"), fees.assembly_fee),
      h("button", { onclick: () => run(async () => { await api("POST", "/api/delivery/settings", { floor_fee: fees.floor_fee.value, assembly_fee: fees.assembly_fee.value }); reload(); }, tr("Đã lưu")) }, tr("Lưu phụ phí"))),
    h("p", { class: "muted" }, d.settings.maps ? tr(
      "Đã có GOOGLE_MAPS_API_KEY: tìm toạ độ và thời gian chạy xe qua Google Maps."
    ) : tr(
      "Chưa có GOOGLE_MAPS_API_KEY: nhập toạ độ tay (lấy từ Google Maps) để tối ưu lộ trình theo khoảng cách."
    )),
    h("table", {}, h("tbody", {}, d.warehouses.map((w) => { const la = h("input", { value: w.lat || "", class: "narrow" }); const lo = h("input", { value: w.lng || "", class: "narrow" });
      return h("tr", {}, h("td", {}, w.name), h("td", {}, h("div", { class: "row" }, la, lo, h("button", { class: "small", onclick: () => run(async () => { await api("PUT", `/api/delivery/warehouses/${w.id}`, { lat: la.value, lng: lo.value }); }, tr("Đã lưu toạ độ")) }, tr("Lưu"))))); }))));

  render(h("h1", {}, tr("Giao hàng")), calendar, day,
    h("div", { class: "card section" }, h("h2", {}, tr("Đặt lịch giao")),
      d.open_orders.length ? h("div", {}, h("div", { class: "two" }, field(tr("Đơn hàng"), f.order), field(tr("Ngày giao"), f.date), field(tr("Khung giờ"), f.slot), field(tr("Giờ cố định"), f.window),
        field(tr("Địa chỉ"), f.address), h("div", { class: "row" }, field(tr("Số tầng vác"), f.floors), h("label", { class: "check" }, f.assembling, h("span", {}, tr("Lắp ráp tại nhà")))),
        field(tr("Ghi chú"), f.notes), h("div", { class: "row" }, field(tr("Vĩ độ"), f.lat), field(tr("Kinh độ"), f.lng))),
      h("button", { class: "primary", onclick: book }, tr("Đặt lịch"))) : h("p", { class: "muted" }, tr("Không có đơn nào đang chờ giao."))),
    setup);
};

// ---------------------------------------------------------------- marketing

views.marketing = async () => {
  const d = await run(() => api("GET", "/api/marketing"));
  if (!d) return;
  const m = moneyOf(await shopCurrency());
  const products = (await api("GET", "/api/inventory/products")).products;
  const reload = () => go("marketing");
  const save = (kind, body, msg) => run(async () => { await api("POST", `/api/marketing/${kind}`, body); reload(); }, msg);
  const del = (kind, id) => confirm(tr("Ngừng / xoá?")) && run(async () => { await api("DELETE", `/api/marketing/${kind}/${id}`); reload(); });
  const now = today();
  const productPicker = () => h("select", { multiple: true, size: 5 }, products.map((p) => h("option", { value: p.id }, `${p.sku} · ${p.name}`)));

  const pr = { name: h("input", { placeholder: tr("Tên chương trình") }), badge: h("input", { placeholder: tr("Nhãn: Hot Deal, Xả kho…") }),
    type: h("select", {}, h("option", { value: "pct" }, tr("Giảm %")), h("option", { value: "amount" }, tr("Giảm số tiền"))), value: h("input", { class: "price", placeholder: tr("mức giảm") }),
    starts: h("input", { type: "date", value: now }), ends: h("input", { type: "date", value: now }), category: h("input", { placeholder: tr("hoặc danh mục") }),
    all: h("input", { type: "checkbox" }), products: productPicker() };
  const vo = { code: h("input", { placeholder: tr("MÃ") }), type: h("select", {}, h("option", { value: "pct" }, "%"), h("option", { value: "amount" }, tr("số tiền"))),
    value: h("input", { class: "price", placeholder: tr("mức giảm") }), min: h("input", { class: "price", placeholder: tr("đơn tối thiểu") }),
    max: h("input", { class: "price", placeholder: tr("giảm tối đa") }), uses: h("input", { class: "narrow", placeholder: tr("số lượt") }), ends: h("input", { type: "date" }) };
  const co = { code: h("input", { placeholder: tr("MÃ COMBO") }), name: h("input", { placeholder: tr("Tên combo") }), price: h("input", { class: "price", placeholder: tr("giá combo") }),
    badge: h("input", { placeholder: tr("nhãn") }), products: productPicker() };
  const ad = { campaign: h("input", { placeholder: tr("Chiến dịch") }), platform: h("select", {}, d.platforms.map((p) => h("option", { value: p }, p))),
    amount: h("input", { class: "price", placeholder: tr("chi phí") }), start: h("input", { type: "date", value: now }), end: h("input", { type: "date", value: now }) };
  const seg = { kind: h("select", {}, h("option", { value: "top" }, tr("Mua nhiều nhất")), h("option", { value: "vip" }, tr("Khách VIP"))),
    channel: h("select", {}, h("option", { value: "" }, tr("mọi kênh")), d.channel_types.map((c) => h("option", { value: c }, c))), min: h("input", { class: "narrow", value: 1 }),
    near: h("select", {}, h("option", { value: "" }, tr("mọi nơi")), d.showrooms.map((w) => h("option", { value: w.id, disabled: !w.located }, tr("quanh {0}{1}", w.name, w.located ? "" : tr(" (chưa có toạ độ)"))))),
    km: h("input", { class: "narrow", value: 30, type: "number", min: 1 }) };
  const segQuery = () => `kind=${seg.kind.value}&channel=${seg.channel.value}&min_orders=${seg.min.value || 0}` + (seg.near.value ? `&near_wh=${seg.near.value}&radius_km=${seg.km.value || 30}` : "");
  const segOut = h("div", {});
  const located = h("p", { class: "muted" }, tr("{0}/{1} khách đã có toạ độ", d.located.located, d.located.n) + (d.located.to_geocode ? tr(" · {0} khách có địa chỉ chưa tìm toạ độ", d.located.to_geocode) : ""),
    d.located.to_geocode && isManager() ? h("button", { class: "small", onclick: () => run(async () => { const r = await api("POST", "/api/crm/geocode", {});
      toast(tr(
        "Đã tìm toạ độ {0} khách{1}",
        r.located,
        r.failed ? tr(", {0} địa chỉ không tìm được", r.failed) : ""
      )); go("marketing"); }) }, tr("Tìm toạ độ từ địa chỉ")) : null);
  const weekly = h("div", {});
  const selected = (sel) => [...sel.selectedOptions].map((o) => Number(o.value));
  const setsText = h("textarea", { rows: 8 }, JSON.stringify(d.sets, null, 1));

  render(h("h1", {}, "Marketing"),
    h("div", { class: "card section" }, h("h2", {}, tr("Chương trình khuyến mại")),
      h("p", { class: "muted" }, tr(
        "Giảm % đi theo giá tự động theo giai đoạn: giá gốc đổi thì giá sale đổi theo. Khách luôn được mức tốt nhất (khuyến mại, VIP, giai đoạn), không cộng dồn."
      )),
      d.promotions.length ? h("table", {}, h("tbody", {}, d.promotions.map((p) => h("tr", {}, h("td", {}, h("b", {}, p.name), p.badge ? h("span", { class: "pill warn" }, p.badge) : null),
        h("td", {}, p.discount_type === "pct" ? `-${p.value}%` : `-${m(p.value)}`), h("td", {}, `${p.starts.slice(0, 10)} → ${p.ends.slice(0, 10)}`),
        h("td", {}, p.all_products ? tr("mọi sản phẩm") : p.category || tr("{0} sản phẩm", p.product_ids.length)), h("td", {}, p.active ? pillOf({ 1: ["ok", tr("bật")] }, 1) : pillOf({ 0: ["neutral", tr("tắt")] }, 0)),
        h("td", {}, p.active ? h("button", { class: "small danger", onclick: () => del("promotions", p.id) }, tr("Tắt")) : null))))) : null,
      h("div", { class: "two section" }, pr.name, pr.badge, h("div", { class: "row" }, pr.type, pr.value), h("div", { class: "row" }, pr.starts, pr.ends),
        pr.category, h("label", { class: "check" }, pr.all, h("span", {}, tr("Mọi sản phẩm")))), field(tr("Sản phẩm (giữ Ctrl để chọn nhiều)"), pr.products),
      h("button", { class: "primary", onclick: () => save("promotions", { name: pr.name.value, badge: pr.badge.value, discount_type: pr.type.value, value: pr.value.value,
        starts: pr.starts.value, ends: pr.ends.value, category: pr.category.value, all_products: pr.all.checked, product_ids: selected(pr.products) }, tr("Đã tạo chương trình")) }, tr("Tạo chương trình"))),
    h("div", { class: "card section" }, h("h2", {}, "Voucher"),
      d.vouchers.length ? h("table", {}, h("tbody", {}, d.vouchers.map((v) => h("tr", {}, h("td", { class: "mono" }, v.code), h("td", {}, v.discount_type === "pct" ? `-${v.value}%` : `-${m(v.value)}`),
        h("td", {}, tr("đã dùng {0}{1}", v.used, v.max_uses ? `/${v.max_uses}` : "")), h("td", {}, v.ends ? tr("đến {0}", v.ends.slice(0, 10)) : ""), h("td", {}, v.active ? h("button", { class: "small danger", onclick: () => del("vouchers", v.id) }, tr("Tắt")) : tr("tắt")))))) : null,
      h("div", { class: "row section" }, vo.code, vo.type, vo.value, vo.min, vo.max, vo.uses, vo.ends,
        h("button", { class: "primary", onclick: () => save("vouchers", { code: vo.code.value, discount_type: vo.type.value, value: vo.value.value, min_order: vo.min.value || 0,
          max_discount: vo.max.value || 0, max_uses: vo.uses.value || 0, ends: vo.ends.value || null }, tr("Đã tạo voucher")) }, tr("Tạo voucher")))),
    h("div", { class: "card section" }, h("h2", {}, tr("Combo (gói sản phẩm)")),
      d.combos.length ? h("table", {}, h("tbody", {}, d.combos.map((c) => h("tr", {}, h("td", {}, h("b", {}, c.name), ` ${c.code}`), h("td", {}, c.items.map((i) => `${i.sku} x${i.qty}`).join(", ")),
        h("td", {}, m(c.price), h("div", { class: "muted" }, tr("mua lẻ {0}", m(c.separate_price)))), h("td", {}, tr("còn {0} bộ", c.available)), h("td", {}, c.live ? h("button", { class: "small danger", onclick: () => del("combos", c.id) }, tr("Tắt")) : tr("tắt")))))) : null,
      h("div", { class: "row section" }, co.code, co.name, co.price, co.badge), field(tr("Sản phẩm trong combo (mỗi thứ 1 cái)"), co.products),
      h("button", { class: "primary", onclick: () => save("combos", { code: co.code.value, name: co.name.value, price: co.price.value, badge: co.badge.value,
        items: selected(co.products).map((id) => ({ product_id: id, qty: 1 })) }, tr("Đã tạo combo")) }, tr("Tạo combo"))),
    h("div", { class: "card section" }, h("h2", {}, tr("Chi phí quảng cáo")),
      d.ads.length ? h("table", {}, h("tbody", {}, d.ads.map((a) => h("tr", {}, h("td", {}, a.campaign), h("td", {}, a.platform), h("td", {}, m(a.amount)), h("td", {}, `${a.start_date} → ${a.end_date}`),
        h("td", {}, h("button", { class: "small danger", onclick: () => del("ads", a.id) }, tr("Xoá"))))))) : null,
      h("div", { class: "row section" }, ad.campaign, ad.platform, ad.amount, ad.start, ad.end,
        h("button", { class: "primary", onclick: () => save("ads", { campaign: ad.campaign.value, platform: ad.platform.value, amount: ad.amount.value, start_date: ad.start.value, end_date: ad.end.value }, tr("Đã ghi chi phí")) }, tr("Ghi chi phí")))),
    h("div", { class: "card section" }, h("h2", {}, tr("Tập khách cho remarketing")),
      h("div", { class: "row" }, seg.kind, seg.channel, field(tr("mua từ (đơn)"), seg.min), seg.near, field(tr("trong bán kính (km)"), seg.km),
        h("button", { onclick: () => run(async () => { const r = await api("GET", `/api/marketing/segment?${segQuery()}`);
          put(segOut, h("p", { class: "muted" }, tr("{0} khách", r.customers.length)), h("table", {}, h("tbody", {}, r.customers.slice(0, 30).map((c) => h("tr", {}, h("td", {}, c.name), h("td", {}, c.phone), h("td", {}, c.email), h("td", {}, m(c.total_spent)), h("td", {}, c.vip ? "VIP" : ""),
            h("td", {}, c.distance_km != null ? `${c.distance_km} km` : "")))))); }) }, tr("Xem")),
        h("button", { onclick: () => { window.location.href = `/api/marketing/segment?${segQuery()}&format=csv`; } }, tr("Xuất CSV"))), located, segOut),
    h("div", { class: "card section" }, h("h2", {}, tr("Biến động giá tuần này")),
      h("button", { onclick: () => run(async () => { const r = await api("GET", "/api/marketing/weekly-prices");
        put(weekly, r.products.length ? h("table", {}, h("thead", {}, h("tr", {}, ["SKU", tr("Sản phẩm"), tr("Giá đầu tuần"), tr("Giá nay"), tr("Đã bán"), tr("Doanh thu"), tr("Lãi"), tr("Còn"), tr("% còn của lô")].map((x) => h("th", {}, x)))),
          h("tbody", {}, r.products.map((p) => h("tr", {}, h("td", { class: "mono" }, p.sku), h("td", {}, p.name), h("td", {}, m(p.price_before)), h("td", {}, m(p.price_now), tr(" GĐ{0}", p.stage)),
            h("td", {}, p.sold), h("td", {}, m(p.revenue)), h("td", {}, m(p.profit)), h("td", {}, p.available), h("td", {}, p.remaining_pct ?? "—"))))) : h("p", { class: "muted" }, tr("Tuần này chưa đổi giá sản phẩm nào."))); }) }, tr("Xem báo cáo")), weekly),
    h("details", { class: "card section" }, h("summary", {}, tr("Bộ sản phẩm gợi ý theo phòng (cho bán hàng và nhân viên AI)")),
      h("p", { class: "muted" }, tr(
        "Mỗi bộ gồm các món; món khớp sản phẩm có các chữ trong tên/danh mục/nhóm (match)."
      )), setsText,
      h("button", { onclick: () => run(async () => { await api("POST", "/api/marketing/sets", { templates: JSON.parse(setsText.value) }); }, tr("Đã lưu")) }, tr("Lưu"))));
};

// ---------------------------------------------------------------- reports

let rpStart = (() => { const d = new Date(); d.setDate(d.getDate() - 29); return localDate(d); })();
let rpEnd = today();
views.reports = async () => {
  const m = moneyOf(await shopCurrency());
  const start = h("input", { type: "date", value: rpStart });
  const end = h("input", { type: "date", value: rpEnd });
  const box = h("div", {});
  const load = async () => {
    rpStart = start.value; rpEnd = end.value;
    const q = `start=${rpStart}&end=${rpEnd}`;
    const [p, com, shifts, exp, st, notices] = await Promise.all([api("GET", `/api/reports/pnl?${q}`), api("GET", `/api/reports/commissions?${q}`),
      api("GET", `/api/reports/shifts?${q}`), api("GET", `/api/reports/expenses?${q}`), api("GET", "/api/reports/settings"), api("GET", "/api/notices")]);
    const sh = { user: h("select", {}, shifts.users.map((u) => h("option", { value: u.username }, u.name))), date: h("input", { type: "date", value: today() }), hours: h("input", { class: "narrow", placeholder: tr("giờ") }) };
    const ex = { name: h("input", { placeholder: tr("khoản chi") }), category: h("select", {}, exp.categories.map((c) => h("option", { value: c }, c))), amount: h("input", { class: "price", placeholder: tr("số tiền") }),
      date: h("input", { type: "date", value: today() }), taxable: h("input", { type: "checkbox", checked: true }) };
    const cs = { target: h("input", { class: "price", value: st.target_per_hour }), rate: h("input", { class: "narrow", value: st.rate_pct }), contrib: h("input", { class: "narrow", value: st.contribution_pct }),
      payroll: h("input", { class: "narrow", value: st.payroll_contribution_pct }) };
    const no = { title: h("input", { placeholder: tr("Tiêu đề") }), body: h("textarea", { rows: 2, placeholder: tr("Nội dung") }) };
    const row = (label, value, strong) => h("tr", {}, h("td", {}, label), h("td", {}, strong ? h("b", {}, m(value || 0)) : m(value || 0)));
    put(box,
      h("div", { class: "tiles" }, tile(m(p.revenue), tr("doanh thu · {0} đơn", p.orders)), tile(m(p.gross_profit), tr("lãi gộp · {0}%", p.gross_margin_pct)), tile(m(p.net_profit), tr("lãi ròng"))),
      h("div", { class: "card section" }, h("h2", {}, tr("Lãi lỗ (P&L)")), h("table", {}, h("tbody", {},
        row(tr("Doanh thu hàng (đã giao)"), p.revenue), row(tr("Giá vốn (FIFO)"), -p.cogs), row(tr("Lãi gộp"), p.gross_profit, true), row(tr("Thu phí giao hàng"), p.shipping_income),
        row(tr("Quảng cáo"), -p.ads), ...Object.entries(p.expenses).map(([k, v]) => row(tr("Chi phí: {0}", k), -v)), row(tr("Bảo hiểm trên lương"), -p.payroll_contribution),
        row(tr("Hoa hồng (đã chốt)"), -p.commissions), row(tr("Giao hàng"), -p.delivery_costs), row(tr("Lãi ròng"), p.net_profit, true))),
        h("p", { class: "muted" }, tr(
          "Đã giảm giá cho khách: {0}. Lợi nhuận thực tế tính khi hàng được giao (theo giá vốn lô FIFO), huỷ/trả hàng thì về 0.",
          m(p.discounts)
        ))),
      h("div", { class: "card section" }, h("h2", {}, tr("Hoa hồng nhân viên")),
        h("p", { class: "muted" }, tr(
          "Hoa hồng = (doanh số đơn đã giao - số giờ × định mức/giờ) × % hoa hồng. Phần đóng góp của chủ (bảo hiểm…) cộng vào chi phí, không trừ vào hoa hồng."
        )),
        h("div", { class: "row" }, field(tr("Định mức doanh số / giờ"), cs.target), field(tr("% hoa hồng"), cs.rate), field(tr("% đóng góp của chủ"), cs.contrib), field(tr("% bảo hiểm trên lương (P&L)"), cs.payroll),
          isManager() ? h("button", { onclick: () => run(async () => { await api("PUT", "/api/reports/settings", { target_per_hour: cs.target.value, rate_pct: cs.rate.value, contribution_pct: cs.contrib.value, payroll_contribution_pct: cs.payroll.value }); await load(); }, tr("Đã lưu")) }, tr("Lưu")) : null),
        h("table", {}, h("thead", {}, h("tr", {}, [tr("Nhân viên"), tr("Đơn"), tr("Doanh số"), tr("Giờ"), tr("Định mức"), tr("Vượt"), tr("Hoa hồng"), tr("Chủ đóng")].map((x) => h("th", {}, x)))),
          h("tbody", {}, com.preview.map((c) => h("tr", {}, h("td", {}, c.username), h("td", {}, c.orders), h("td", {}, m(c.sales)), h("td", {}, c.hours), h("td", {}, m(c.target)), h("td", {}, m(c.excess)), h("td", {}, h("b", {}, m(c.commission))), h("td", {}, m(c.contribution)))))),
        isManager() ? h("button", { class: "primary", onclick: () => run(async () => { await api("POST", "/api/reports/commissions", { start: rpStart, end: rpEnd, save: true }); await load(); }, tr("Đã lưu bảng hoa hồng")) }, tr("Lưu bảng hoa hồng kỳ này")) : null,
        com.saved.length ? h("table", {}, h("tbody", {}, com.saved.map((c) => h("tr", {}, h("td", {}, c.username), h("td", {}, `${c.period_start} → ${c.period_end}`), h("td", {}, m(c.commission)), h("td", {}, c.status),
          h("td", {}, isManager() && c.status !== "paid" ? h("button", { class: "small", onclick: () => run(async () => { await api("POST", `/api/reports/commissions/${c.id}/status`, { status: c.status === "draft" ? "finalized" : "paid" }); await load(); }) }, c.status === "draft" ? tr("Chốt") : tr("Đã trả")) : null))))) : null),
      h("div", { class: "card section" }, h("h2", {}, tr("Ca làm việc")), h("div", { class: "row" }, sh.user, sh.date, sh.hours,
        h("button", { onclick: () => run(async () => { await api("POST", "/api/reports/shifts", { username: sh.user.value, work_date: sh.date.value, hours: sh.hours.value }); await load(); }, tr("Đã ghi ca")) }, tr("Ghi ca"))),
        h("table", {}, h("tbody", {}, shifts.shifts.slice(0, 50).map((x) => h("tr", {}, h("td", {}, x.username), h("td", {}, x.work_date), h("td", {}, tr("{0} giờ", x.hours)),
          h("td", {}, h("button", { class: "small danger", onclick: () => run(async () => { await api("DELETE", `/api/reports/shifts/${x.id}`); await load(); }) }, tr("Xoá")))))))),
      h("div", { class: "card section" }, h("h2", {}, tr("Chi phí")), h("div", { class: "row" }, ex.name, ex.category, ex.amount, ex.date, h("label", { class: "check" }, ex.taxable, h("span", {}, tr("có hoá đơn"))),
        h("button", { onclick: () => run(async () => { await api("POST", "/api/reports/expenses", { name: ex.name.value, category: ex.category.value, amount: ex.amount.value, date: ex.date.value, taxable: ex.taxable.checked }); await load(); }, tr("Đã ghi")) }, tr("Ghi chi phí"))),
        h("table", {}, h("tbody", {}, exp.expenses.map((x) => h("tr", {}, h("td", {}, x.expense_date), h("td", {}, x.name), h("td", {}, x.category), h("td", {}, m(x.amount)),
          h("td", {}, h("button", { class: "small danger", onclick: () => run(async () => { await api("DELETE", `/api/reports/expenses/${x.id}`); await load(); }) }, tr("Xoá")))))))),
      isManager() ? h("div", { class: "card section" }, h("h2", {}, tr("Thông báo đầu ngày cho nhân viên")),
        h("div", { class: "row" }, no.title, h("button", { class: "primary", onclick: () => run(async () => { await api("POST", "/api/notices", { title: no.title.value, body: no.body.value }); await load(); }, tr("Đã đăng")) }, tr("Đăng"))), no.body,
        h("table", {}, h("tbody", {}, notices.all.map((n) => h("tr", {}, h("td", {}, h("b", {}, n.title), h("div", { class: "muted" }, n.body)), h("td", {}, tr("{0} đã xác nhận", n.acks.filter((a) => a.action === "done").length)),
          h("td", {}, n.active ? h("button", { class: "small", onclick: () => run(async () => { await api("POST", `/api/notices/${n.id}/close`, {}); await load(); }) }, tr("Gỡ")) : tr("đã gỡ"))))))) : null);
  };
  render(h("div", { class: "row spread" }, h("h1", {}, tr("Báo cáo")), h("div", { class: "row" }, start, end, h("button", { onclick: () => run(load) }, tr("Xem")))), box);
  await run(load);
};

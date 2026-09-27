"use strict";
// Kho hàng: products, purchasing with landed cost, automatic 5-stage pricing, sales
// orders, transfers and reorder suggestions. Uses the helpers of admin.js (h, api, run...).

const LEVEL = {
  empty: ["bad", "Hết hàng"], reorder: ["bad", "Cần đặt hàng"], low: ["warn", "Tồn thấp"],
  medium: ["neutral", "Tồn vừa"], good: ["ok", "Tồn tốt"],
};
const PO_STATUS = {
  draft: ["neutral", "nháp"], ordered: ["warn", "đã đặt"], shipping: ["warn", "đang về"], arrived: ["warn", "đã cập cảng"],
  partial: ["warn", "nhận một phần"], received: ["ok", "đã nhận đủ"], cancelled: ["neutral", "đã huỷ"],
};
const ORDER_STATUS = { confirmed: ["warn", "đã xác nhận, giữ hàng"], completed: ["ok", "đã giao"], cancelled: ["neutral", "đã huỷ"] };
const TRANSFER_STATUS = { requested: ["neutral", "chờ xuất"], in_transit: ["warn", "đang chuyển"], received: ["ok", "đã nhận"], cancelled: ["neutral", "đã huỷ"] };
const MOVE = {
  stock_in: "Nhập hàng", opening: "Tồn đầu kỳ", sale_out: "Bán", reserve: "Giữ hàng", release: "Trả giữ hàng", adjust: "Kiểm kho",
  transfer_out: "Chuyển đi", transfer_in: "Chuyển đến", transfer_loss: "Thiếu khi chuyển", damaged: "Hỏng khi nhận",
};
const TRIGGER = { system: "tự động", admin: "cập nhật ngay", manual: "sửa tay", activate: "lô mới" };
const INV_TABS = [["products", "Sản phẩm"], ["purchase", "Nhập hàng"], ["orders", "Đơn bán"], ["transfers", "Chuyển kho"],
  ["reorder", "Đặt hàng lại"], ["pricing", "Định giá"], ["setup", "Kho & nhà cung cấp"], ["shop", "Cửa hàng & tích điểm"], ["marketplaces", "Sàn TMĐT"]];

let invMeta = { settings: { currency: "VND" }, warehouses: [], suppliers: [] };
let invTab = "products";
const pillOf = (map, key) => { const [cls, label] = map[key] || ["neutral", key]; return h("span", { class: `pill ${cls}` }, label); };
const money = (n) => (n === null || n === undefined || n === "" ? "—" : `${Number(n).toLocaleString("vi-VN")} ${invMeta.settings.currency === "VND" ? "đ" : invMeta.settings.currency}`);
const num = (el) => (el.value.trim() === "" ? null : Number(el.value));
const whName = (id) => (invMeta.warehouses.find((w) => w.id === id) || {}).name || `#${id}`;
const field = (label, el) => h("label", {}, label, el);
const ask = (msg, dflt = "") => { const v = prompt(msg, dflt); return v === null ? null : v.trim(); };

views.inventory = async (tab) => {
  if (tab) invTab = tab;
  const data = await run(() => api("GET", "/api/inventory"));
  if (!data) return;
  invMeta = data;
  const body = h("div", {});
  const t = data.today, m = data.month;
  render(
    h("div", { class: "row spread" }, h("h1", {}, "Kho hàng"),
      h("span", { class: "muted" }, `Giá trị tồn kho (giá vốn): ${money(m.stock_value)}`)),
    h("div", { class: "tiles" },
      tile(t.stock_in, "nhập hôm nay"), tile(t.sold, "bán hôm nay"),
      tile(money(m.revenue), "doanh thu 30 ngày"), tile(money(m.profit), "lãi gộp 30 ngày")),
    h("div", { class: "tabs" }, INV_TABS.map(([k, label]) => h("button", {
      class: k === invTab ? "active" : "", onclick: () => go("inventory", k),
    }, label))),
    body,
  );
  if (!data.warehouses.length && invTab !== "setup") {
    put(body, h("div", { class: "card" }, h("p", {}, "Chưa có kho nào. Thêm kho và nhà cung cấp trước."),
      h("button", { class: "primary", onclick: () => go("inventory", "setup") }, "Thêm kho")));
    return;
  }
  await run(() => INV_VIEWS[invTab](body));
};

const INV_VIEWS = {};

// ---------------------------------------------------------------- products

INV_VIEWS.products = async (box) => {
  const search = h("input", { type: "search", placeholder: "Tìm SKU, tên, danh mục" });
  const levelSel = h("select", {}, h("option", { value: "" }, "Mọi mức tồn"), Object.entries(LEVEL).map(([k, [, l]]) => h("option", { value: k }, l)));
  const list = h("div", { class: "card table-wrap" });
  const detail = h("div", {});
  const load = async () => {
    const q = new URLSearchParams({ q: search.value.trim(), level: levelSel.value });
    const r = await api("GET", `/api/inventory/products?${q}`);
    put(list, r.products.length ? h("table", {},
      h("thead", {}, h("tr", {}, ["SKU", "Sản phẩm", "Có thể bán", "Đang giữ", "Sắp về", "Giá bán", "Giá vốn", "Tồn"].map((x) => h("th", {}, x)))),
      h("tbody", {}, r.products.map((p) => h("tr", { class: "clickable", onclick: () => run(() => showProduct(detail, p.id, load)) },
        h("td", { class: "mono" }, p.sku), h("td", {}, h("b", {}, p.name), p.group_name ? h("div", { class: "muted" }, p.group_name) : null),
        h("td", {}, p.available), h("td", {}, p.reserved || ""), h("td", {}, p.incoming ? `${p.incoming}${p.next_eta ? " · " + p.next_eta : ""}` : ""),
        h("td", {}, p.price !== null ? h("span", {}, money(p.price), " ", h("span", { class: "muted" }, `GĐ${p.stage}`)) : "—"),
        h("td", {}, money(p.landed_cost)), h("td", {}, pillOf(LEVEL, p.level)))))) : h("p", { class: "muted" }, "Chưa có sản phẩm."));
  };
  const f = { sku: h("input", { placeholder: "vd. SOFA-01" }), name: h("input", { placeholder: "Tên sản phẩm" }),
    category: h("input", { placeholder: "Danh mục" }), group_name: h("input", { placeholder: "Nhóm biến thể (tuỳ chọn)" }),
    unit: h("input", { placeholder: "cái, bộ…" }), cbm: h("input", { placeholder: "CBM / sản phẩm", inputmode: "decimal" }) };
  const add = () => run(async () => {
    const p = await api("POST", "/api/inventory/products", Object.fromEntries(Object.entries(f).map(([k, el]) => [k, el.value.trim()])));
    for (const el of Object.values(f)) el.value = "";
    await load();
    await showProduct(detail, p.id, load);
  }, "Đã thêm sản phẩm");
  const file = h("input", { type: "file", accept: ".csv,text/csv", hidden: true });
  file.addEventListener("change", () => run(async () => {
    if (!file.files.length) return;
    const text = await file.files[0].text();
    const r = await fetch("/api/inventory/products/import", { method: "POST", body: text, credentials: "same-origin",
      headers: { "X-Requested-With": "ai-employees", "Content-Type": "text/csv" } });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
    toast(`Thêm ${d.created}, cập nhật ${d.updated}${d.errors.length ? `, lỗi: ${d.errors.slice(0, 3).join("; ")}` : ""}`);
    file.value = "";
    await load();
  }));
  let timer;
  search.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(() => run(load), 300); });
  levelSel.addEventListener("change", () => run(load));
  put(box,
    h("div", { class: "row spread" }, h("div", { class: "row" }, search, levelSel),
      h("div", { class: "row" }, h("a", { class: "button", href: "/api/inventory/products.csv" }, "Xuất Excel (CSV)"),
        h("button", { onclick: () => file.click() }, "Nhập từ CSV"), file)),
    list, detail,
    h("div", { class: "card section" }, h("h2", {}, "Thêm sản phẩm"),
      h("div", { class: "two" }, field("Mã SKU", f.sku), field("Tên", f.name), field("Danh mục", f.category),
        field("Nhóm biến thể", f.group_name), field("Đơn vị", f.unit), field("Thể tích (CBM)", f.cbm)),
      h("button", { class: "primary", onclick: add }, "Thêm sản phẩm"),
      h("p", { class: "muted" }, "CSV: các cột sku, name, category, group_name, unit, cbm, weight_kg, vip_price, safety_stock, description. Dòng có SKU đã có thì được cập nhật.")),
  );
  await load();
};

async function showProduct(box, id, reload) {
  const p = await api("GET", `/api/inventory/products/${id}`);
  const refresh = async (msg) => { if (msg) toast(msg); await reload(); await showProduct(box, id, reload); };
  const post = (path, body, msg) => run(async () => { await api("POST", `/api/inventory/products/${id}/${path}`, body); await refresh(msg); });
  const e = { name: h("input", { value: p.name }), category: h("input", { value: p.category }), group_name: h("input", { value: p.group_name }),
    unit: h("input", { value: p.unit }), cbm: h("input", { value: p.cbm }), weight_kg: h("input", { value: p.weight_kg }),
    vip_price: h("input", { value: p.vip_price ?? "", placeholder: "để trống: giá giai đoạn kế tiếp" }),
    safety_stock: h("input", { value: p.safety_stock_setting >= 0 ? p.safety_stock_setting : "", placeholder: `tự tính (${p.safety_stock})` }),
    supplier_id: h("select", {}, h("option", { value: "" }, "— theo đơn nhập gần nhất —"), invMeta.suppliers.map((s) => h("option", { value: s.id, selected: s.id === p.supplier_id }, s.name))),
    auto_pricing: h("input", { type: "checkbox", checked: !!p.auto_pricing }), active: h("input", { type: "checkbox", checked: !!p.active }) };
  const save = () => run(async () => {
    await api("PATCH", `/api/inventory/products/${id}`, { name: e.name.value, category: e.category.value, group_name: e.group_name.value,
      unit: e.unit.value, cbm: e.cbm.value || "0", weight_kg: e.weight_kg.value || "0", vip_price: e.vip_price.value,
      safety_stock: e.safety_stock.value, supplier_id: e.supplier_id.value ? Number(e.supplier_id.value) : null,
      auto_pricing: e.auto_pricing.checked, active: e.active.checked });
    await refresh("Đã lưu sản phẩm");
  });
  const whSel = () => h("select", {}, invMeta.warehouses.filter((w) => w.active).map((w) => h("option", { value: w.id }, w.name)));
  const op = { wh: whSel(), qty: h("input", { placeholder: "số lượng" }), cost: h("input", { placeholder: "giá vốn / cái" }), margin: h("input", { placeholder: `lãi % (mặc định ${invMeta.settings.default_margin_pct})` }) };
  const cnt = { wh: whSel(), counted: h("input", { placeholder: "số đếm được" }), reason: h("input", { placeholder: "lý do" }) };
  const lotRow = (l) => {
    const inputs = l.prices.map((x) => h("input", { value: x, class: "price" }));
    const vip = h("input", { value: l.vip_price ?? "", class: "price", placeholder: "VIP" });
    const editable = l.status !== "exhausted";
    return h("tr", {},
      h("td", {}, `#${l.id}`, h("div", { class: "muted" }, l.received_date)), h("td", {}, pillOf({ active: ["ok", "đang bán"], queued: ["neutral", "chờ bán"], exhausted: ["neutral", "đã hết"] }, l.status)),
      h("td", {}, `${l.remaining_qty}/${l.initial_qty}`, l.reserved_qty ? h("div", { class: "muted" }, `giữ ${l.reserved_qty}`) : null, h("div", { class: "muted" }, `còn ${l.remaining_pct}%`)),
      h("td", {}, money(l.landed_cost)), h("td", {}, `GĐ${l.stage}`),
      h("td", {}, editable ? h("div", { class: "row" }, inputs, vip,
        h("button", { class: "small", onclick: () => run(async () => {
          await api("PUT", `/api/inventory/lots/${l.id}/prices`, { prices: inputs.map((x) => x.value), vip_price: vip.value });
          await refresh("Đã lưu giá của lô");
        }) }, "Lưu")) : l.prices.map(money).join(" · ")));
  };
  put(box, h("div", { class: "card section" },
    h("div", { class: "row spread" }, h("h2", {}, `${p.sku} · ${p.name}`), pillOf(LEVEL, p.level)),
    h("div", { class: "tiles" }, tile(p.available, "có thể bán"), tile(p.incoming, `sắp về${p.next_eta ? " · " + p.next_eta : ""}`),
      tile(p.price !== null ? money(p.price) : "—", `giá hiện tại${p.stage ? " · giai đoạn " + p.stage : ""}`),
      tile(`${p.daily_sales}/ngày`, `tốc độ bán · điểm đặt lại ${p.reorder_point}`)),
    p.suggest_order ? h("p", { class: "pill bad" }, `Nên đặt thêm ${p.suggest_order} (NCC giao trong ${p.lead_time_days} ngày)`) : null,
    h("h3", {}, "Tồn theo kho"),
    p.by_warehouse.length ? h("div", { class: "row" }, p.by_warehouse.map((w) => h("span", { class: "pill neutral" }, `${w.code}: ${w.on_hand}${w.reserved ? ` (giữ ${w.reserved})` : ""}`))) : h("p", { class: "muted" }, "Chưa có hàng trong kho."),
    p.incoming_orders.length ? h("p", { class: "muted" }, "Đang về: ", p.incoming_orders.map((x) => `${x.po_number} ${x.qty} cái (${x.eta || "chưa có ETA"}${x.preordered ? `, khách đặt trước ${x.preordered}` : ""})`).join(" · ")) : null,
    h("h3", {}, "Lô hàng (FIFO) và bảng giá 5 giai đoạn"),
    p.lots.length ? h("div", { class: "table-wrap" }, h("table", {}, h("thead", {}, h("tr", {}, ["Lô", "Trạng thái", "Còn", "Giá vốn", "Giai đoạn", "Giá GĐ1 → GĐ5, VIP"].map((x) => h("th", {}, x)))),
      h("tbody", {}, p.lots.map(lotRow)))) : h("p", { class: "muted" }, "Chưa có lô hàng: nhập hàng qua đơn nhập, hoặc khai tồn đầu kỳ bên dưới."),
    p.price !== null ? h("div", { class: "row" }, h("span", { class: "muted" }, "Đặt giai đoạn giá:"),
      [1, 2, 3, 4, 5].map((s) => h("button", { class: "small" + (s === p.stage ? " active" : ""), onclick: () => { const why = ask(`Chuyển sang giai đoạn ${s}. Lý do:`, "Quản lý điều chỉnh"); if (why !== null) post("stage", { stage: s, reason: why }, "Đã đổi giai đoạn giá"); } }, `GĐ${s}`)),
      h("button", { class: "small", onclick: () => run(async () => { const r = await api("POST", "/api/inventory/pricing/run", { product_id: id }); await refresh(r.changes.length ? "Đã cập nhật giá" : "Chưa đến lúc đổi giá"); }) }, "Cập nhật giá ngay")) : null,
    h("details", {}, h("summary", {}, "Sửa thông tin sản phẩm"),
      h("div", { class: "two section" }, field("Tên", e.name), field("Danh mục", e.category), field("Nhóm biến thể", e.group_name), field("Đơn vị", e.unit),
        field("CBM / sản phẩm", e.cbm), field("Cân nặng (kg)", e.weight_kg), field("Giá VIP riêng", e.vip_price), field("Tồn an toàn", e.safety_stock), field("Nhà cung cấp", e.supplier_id)),
      h("label", { class: "check" }, e.auto_pricing, h("span", {}, "Tự động định giá (giảm dần theo tồn và thời gian)")),
      h("label", { class: "check" }, e.active, h("span", {}, "Đang bán")),
      h("button", { class: "primary", onclick: save }, "Lưu")),
    h("details", {}, h("summary", {}, "SKU trên các sàn"), (() => {
      const mk = h("input", { placeholder: "mã sàn, vd. amazon-au" });
      const sku = h("input", { placeholder: "SKU trên sàn, hoặc - để không bán" });
      return h("div", { class: "row section" }, mk, sku, h("button", { onclick: () => run(async () => {
        await api("POST", `/api/inventory/products/${id}/external-sku`, { marketplace: mk.value.trim(), sku: sku.value });
      }, "Đã lưu SKU sàn") }, "Lưu"));
    })()),
    h("details", {}, h("summary", {}, "Tồn đầu kỳ (hàng có sẵn trước khi dùng hệ thống)"),
      h("div", { class: "row section" }, op.wh, op.qty, op.cost, op.margin,
        h("button", { onclick: () => post("opening", { warehouse_id: Number(op.wh.value), qty: op.qty.value, unit_cost: op.cost.value, margin_pct: op.margin.value || null }, "Đã thêm tồn đầu kỳ") }, "Thêm"))),
    h("details", {}, h("summary", {}, "Kiểm kho (đặt lại số lượng thực tế)"),
      h("div", { class: "row section" }, cnt.wh, cnt.counted, cnt.reason,
        h("button", { onclick: () => post("adjust", { warehouse_id: Number(cnt.wh.value), counted: cnt.counted.value, reason: cnt.reason.value }, "Đã điều chỉnh tồn") }, "Lưu"))),
    h("details", {}, h("summary", {}, `Lịch sử giá (${p.price_log.length})`),
      h("table", {}, h("tbody", {}, p.price_log.map((x) => h("tr", {}, h("td", {}, fmtTime(x.ts)), h("td", {}, TRIGGER[x.trigger] || x.trigger),
        h("td", {}, x.old_stage ? `GĐ${x.old_stage} ${money(x.old_price)}` : "—", " → ", x.new_stage ? `GĐ${x.new_stage} ${money(x.new_price)}` : "—"),
        h("td", {}, x.reason, x.actor ? h("span", { class: "muted" }, ` · ${x.actor}`) : null)))))),
    h("details", {}, h("summary", {}, `Sổ kho (${p.moves.length})`),
      h("table", {}, h("tbody", {}, p.moves.map((x) => h("tr", {}, h("td", {}, fmtTime(x.ts)), h("td", {}, x.warehouse_code), h("td", {}, MOVE[x.kind] || x.kind),
        h("td", {}, x.qty > 0 ? `+${x.qty}` : x.qty || ""), h("td", {}, x.note, x.actor ? h("span", { class: "muted" }, ` · ${x.actor}`) : null)))))),
  ));
  box.scrollIntoView({ behavior: "smooth", block: "start" });
}

// ---------------------------------------------------------------- purchase orders

INV_VIEWS.purchase = async (box) => {
  const r = await api("GET", "/api/inventory/purchase-orders");
  const detail = h("div", {});
  put(box,
    h("div", { class: "row spread" }, h("p", { class: "muted" }, "Mỗi đơn nhập là một container / lô hàng từ nhà cung cấp. Giá vốn = giá mua × tỷ giá + cước và thuế chia theo thể tích (CBM)."),
      h("button", { class: "primary", onclick: () => poForm(detail, null) }, "Tạo đơn nhập")),
    h("div", { class: "card table-wrap" }, r.purchase_orders.length ? h("table", {},
      h("thead", {}, h("tr", {}, ["Số đơn", "Nhà cung cấp", "Kho nhận", "Số lượng", "Giá trị (giá vốn)", "Dự kiến về", "Trạng thái"].map((x) => h("th", {}, x)))),
      h("tbody", {}, r.purchase_orders.map((o) => h("tr", { class: "clickable", onclick: () => run(() => poDetail(detail, o.id)) },
        h("td", { class: "mono" }, o.po_number, o.container_code ? h("div", { class: "muted" }, o.container_code) : null), h("td", {}, o.supplier_name),
        h("td", {}, o.warehouse_name), h("td", {}, o.qty || 0), h("td", {}, money(o.value)), h("td", {}, o.eta || "—"), h("td", {}, pillOf(PO_STATUS, o.status)))))) : h("p", { class: "muted" }, "Chưa có đơn nhập.")),
    detail);
};

async function poForm(box, po) {
  const products = (await api("GET", "/api/inventory/products?all=1")).products;
  const hd = {
    supplier_id: h("select", {}, invMeta.suppliers.filter((s) => s.active).map((s) => h("option", { value: s.id, selected: po && s.id === po.supplier_id }, `${s.name} (${s.lead_time_days} ngày)`))),
    warehouse_id: h("select", {}, invMeta.warehouses.filter((w) => w.active).map((w) => h("option", { value: w.id, selected: po && w.id === po.warehouse_id }, w.name))),
    po_number: h("input", { value: po ? po.po_number : "", placeholder: "tự đánh số nếu để trống" }),
    container_code: h("input", { value: po ? po.container_code : "", placeholder: "số container" }),
    eta: h("input", { type: "date", value: po ? po.eta : "" }),
    currency: h("input", { value: po ? po.currency : "USD", class: "narrow" }),
    exchange_rate: h("input", { value: po ? po.exchange_rate : "25000", inputmode: "decimal" }),
    freight: h("input", { value: po ? po.freight : "0", inputmode: "decimal" }),
    customs: h("input", { value: po ? po.customs : "0", inputmode: "decimal" }),
  };
  if (!invMeta.suppliers.length) { put(box, h("div", { class: "card" }, "Thêm nhà cung cấp trước (tab Kho & nhà cung cấp).")); return; }
  const rows = [];
  const tbody = h("tbody", {});
  const preview = h("div", {});
  const addRow = (it = {}) => {
    const row = {
      product: h("select", {}, products.map((p) => h("option", { value: p.id, selected: p.id === it.product_id }, `${p.sku} · ${p.name}`))),
      qty: h("input", { value: it.qty_ordered || it.qty || "", class: "narrow" }),
      cost: h("input", { value: it.unit_cost_foreign || "", class: "narrow", placeholder: "giá mua" }),
      cbm: h("input", { value: it.unit_cbm || "", class: "narrow", placeholder: "CBM" }),
      margin: h("input", { value: it.margin_pct || "", class: "narrow", placeholder: `lãi %` }),
      prices: it.prices || null,
    };
    row.tr = h("tr", {}, h("td", {}, row.product), h("td", {}, row.qty), h("td", {}, row.cost), h("td", {}, row.cbm), h("td", {}, row.margin),
      h("td", {}, h("button", { class: "small danger", onclick: () => { rows.splice(rows.indexOf(row), 1); row.tr.remove(); } }, "✕")));
    rows.push(row);
    tbody.append(row.tr);
  };
  (po ? po.items : [{}]).forEach(addRow);
  const items = () => rows.map((r) => ({ product_id: Number(r.product.value), qty: r.qty.value, unit_cost_foreign: r.cost.value,
    unit_cbm: r.cbm.value || null, margin_pct: r.margin.value || null, ...(r.prices ? { prices: r.prices } : {}) }));
  const header = () => ({ supplier_id: Number(hd.supplier_id.value), warehouse_id: Number(hd.warehouse_id.value), po_number: hd.po_number.value,
    container_code: hd.container_code.value, eta: hd.eta.value, currency: hd.currency.value, exchange_rate: hd.exchange_rate.value,
    freight: hd.freight.value || 0, customs: hd.customs.value || 0 });
  const calc = () => run(async () => {
    const withCbm = items().map((it) => ({ ...it, unit_cbm: it.unit_cbm ?? products.find((p) => p.id === it.product_id)?.cbm ?? 0 }));
    const r = await api("POST", "/api/inventory/calc", { ...header(), items: withCbm });
    put(preview, h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, ["SKU", "Giá mua quy đổi", "Cước", "Thuế", "Giá vốn", "Lãi", "Giá GĐ1 → GĐ5 (sửa được)"].map((x) => h("th", {}, x)))),
      h("tbody", {}, r.items.map((c, i) => {
        const inputs = c.prices.map((x) => h("input", { value: x, class: "price" }));
        inputs.forEach((inp) => inp.addEventListener("change", () => { rows[i].prices = inputs.map((x) => x.value); }));
        rows[i].prices = null;
        return h("tr", {}, h("td", {}, products.find((p) => p.id === Number(rows[i].product.value))?.sku),
          h("td", {}, money(c.unit_cost)), h("td", {}, money(c.unit_freight)), h("td", {}, money(c.unit_tax)), h("td", {}, h("b", {}, money(c.landed_cost))),
          h("td", {}, `${c.margin_pct}% · ${money(c.profit)}`), h("td", {}, h("div", { class: "row" }, inputs),
            c.below_cost.length ? h("div", { class: "pill bad" }, `GĐ${c.below_cost.join(", ")} thấp hơn giá vốn`) : null));
      })))));
  });
  const save = () => run(async () => {
    const out = await api(po ? "PUT" : "POST", po ? `/api/inventory/purchase-orders/${po.id}` : "/api/inventory/purchase-orders", { ...header(), items: items() });
    await poDetail(box, out.id);
  }, "Đã lưu đơn nhập");
  put(box, h("div", { class: "card section" }, h("h2", {}, po ? `Sửa đơn nhập ${po.po_number}` : "Tạo đơn nhập"),
    h("div", { class: "two" }, field("Nhà cung cấp", hd.supplier_id), field("Kho nhận", hd.warehouse_id), field("Số đơn", hd.po_number),
      field("Container", hd.container_code), field("Ngày dự kiến về (ETA)", hd.eta),
      h("div", { class: "row" }, field("Tiền tệ", hd.currency), field(`Tỷ giá → ${invMeta.settings.currency}`, hd.exchange_rate)),
      field(`Cước cả container (${invMeta.settings.currency})`, hd.freight), field(`Thuế, phí hải quan (${invMeta.settings.currency})`, hd.customs)),
    h("div", { class: "table-wrap" }, h("table", {}, h("thead", {}, h("tr", {}, ["Sản phẩm", "Số lượng", "Giá mua (ngoại tệ)", "CBM/cái", "Lãi mục tiêu %", ""].map((x) => h("th", {}, x)))), tbody)),
    h("div", { class: "row" }, h("button", { onclick: () => addRow() }, "+ Dòng"), h("button", { onclick: calc }, "Tính giá vốn & giá bán"), h("button", { class: "primary", onclick: save }, "Lưu đơn nhập")),
    h("p", { class: "muted" }, "Để trống CBM thì lấy CBM của sản phẩm. Để trống lãi thì dùng lãi mặc định. Sau khi tính, có thể sửa từng giá trước khi lưu."),
    preview));
  box.scrollIntoView({ behavior: "smooth", block: "start" });
}

async function poDetail(box, id) {
  const po = await api("GET", `/api/inventory/purchase-orders/${id}`);
  const status = (s, msg) => run(async () => { await api("POST", `/api/inventory/purchase-orders/${id}/status`, { status: s }); await poDetail(box, id); }, msg);
  const receiving = ["ordered", "shipping", "arrived", "partial"].includes(po.status);
  const inputs = po.items.map((it) => {
    const left = it.qty_ordered - it.qty_received - it.qty_damaged;
    return { it, left, good: h("input", { value: left, class: "narrow" }), damaged: h("input", { value: 0, class: "narrow" }) };
  });
  const receive = () => run(async () => {
    const r = await api("POST", `/api/inventory/purchase-orders/${id}/receive`, { items: inputs.filter((x) => x.left > 0).map((x) => ({ item_id: x.it.id, qty: x.good.value, damaged: x.damaged.value })) });
    toast(r.served_preorders.length ? `Đã nhập kho; phục vụ ${r.served_preorders.length} đơn đặt trước` : "Đã nhập kho");
    await poDetail(box, id);
  });
  put(box, h("div", { class: "card section" },
    h("div", { class: "row spread" }, h("h2", {}, `Đơn nhập ${po.po_number}`), pillOf(PO_STATUS, po.status)),
    h("p", { class: "muted" }, `${po.supplier_name} → ${po.warehouse_name} · ETA ${po.eta || "—"}${po.arrived ? ` · về ${po.arrived}` : ""} · ${po.total_cbm} CBM · cước ${money(po.freight)} · thuế ${money(po.customs)} · tỷ giá ${po.exchange_rate} ${po.currency}`),
    h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, ["SKU", "Đặt", "Đã nhận", "Hỏng", "Khách đặt trước", "Giá vốn", "Giá GĐ1 → GĐ5", ...(receiving ? ["Nhận đợt này", "Hỏng"] : [])].map((x) => h("th", {}, x)))),
      h("tbody", {}, inputs.map(({ it, left, good, damaged }) => h("tr", {},
        h("td", {}, h("b", {}, it.sku), h("div", { class: "muted" }, it.name)), h("td", {}, it.qty_ordered), h("td", {}, it.qty_received), h("td", {}, it.qty_damaged || ""),
        h("td", {}, it.qty_preordered || ""), h("td", {}, money(it.landed_cost)),
        h("td", {}, it.prices.map(money).join(" · "), it.below_cost.length ? h("div", { class: "pill bad" }, `GĐ${it.below_cost.join(", ")} dưới giá vốn`) : null),
        ...(receiving ? [h("td", {}, left > 0 ? good : "đủ"), h("td", {}, left > 0 ? damaged : "")] : [])))))),
    h("div", { class: "row" },
      ["draft", "ordered"].includes(po.status) ? h("button", { onclick: () => run(() => poForm(box, po)) }, "Sửa") : null,
      po.status === "draft" ? h("button", { class: "primary", onclick: () => status("ordered", "Đã đặt hàng") }, "Đặt hàng") : null,
      po.status === "ordered" ? h("button", { onclick: () => status("shipping", "Đang về") }, "Đã lên tàu") : null,
      ["ordered", "shipping"].includes(po.status) ? h("button", { onclick: () => status("arrived", "Đã cập cảng") }, "Đã cập cảng") : null,
      receiving ? h("button", { class: "primary", onclick: receive }, "Nhập kho số thực nhận") : null,
      ["draft", "ordered"].includes(po.status) ? h("button", { class: "danger", onclick: () => confirm("Huỷ đơn nhập?") && status("cancelled", "Đã huỷ") }, "Huỷ") : null),
  ));
  box.scrollIntoView({ behavior: "smooth", block: "start" });
}

// ---------------------------------------------------------------- sales orders

INV_VIEWS.orders = async (box) => {
  const r = await api("GET", "/api/inventory/orders");
  const products = (await api("GET", "/api/inventory/products")).products;
  const step = (o, s, msg) => run(async () => { await api("POST", `/api/inventory/orders/${o.id}/${s}`, {}); await INV_VIEWS.orders(box); }, msg);
  const lines = h("div", {});
  const line = () => {
    const sel = h("select", {}, products.map((p) => h("option", { value: p.id }, `${p.sku} · ${p.name} · còn ${p.available}${p.incoming ? ` · về ${p.incoming}` : ""} · ${money(p.price)}`)));
    const qty = h("input", { value: 1, class: "narrow" });
    const price = h("input", { class: "narrow", placeholder: "giá riêng" });
    const el = h("div", { class: "row" }, sel, qty, price, h("button", { class: "small danger", onclick: () => el.remove() }, "✕"));
    el.data = () => ({ product_id: Number(sel.value), qty: qty.value, unit_price: price.value || null });
    lines.append(el);
  };
  line();
  const f = { kind: h("select", {}, h("option", { value: "now" }, "Có sẵn (giữ hàng trong kho)"), h("option", { value: "preorder" }, "Đặt trước (hàng sắp về)")),
    wh: h("select", {}, invMeta.warehouses.filter((w) => w.active).map((w) => h("option", { value: w.id }, w.name))),
    name: h("input", { placeholder: "Tên khách" }), phone: h("input", { placeholder: "Điện thoại" }), address: h("input", { placeholder: "Địa chỉ giao" }),
    discount: h("input", { placeholder: "Giảm giá (số tiền)" }) };
  const create = () => run(async () => {
    await api("POST", "/api/inventory/orders", { kind: f.kind.value, warehouse_id: Number(f.wh.value), customer_name: f.name.value, phone: f.phone.value,
      address: f.address.value, discount: f.discount.value || 0, items: [...lines.children].map((x) => x.data()) });
    await INV_VIEWS.orders(box);
  }, "Đã tạo đơn, hàng đã được giữ");
  put(box,
    h("div", { class: "card table-wrap" }, r.orders.length ? h("table", {},
      h("thead", {}, h("tr", {}, ["Mã", "Khách", "Loại", "Kho", "Tổng", "Lãi", "Trạng thái", ""].map((x) => h("th", {}, x)))),
      h("tbody", {}, r.orders.map((o) => h("tr", {},
        h("td", { class: "mono" }, o.code, h("div", { class: "muted" }, fmtTime(o.created))), h("td", {}, o.customer_name || "—", o.phone ? h("div", { class: "muted" }, o.phone) : null),
        h("td", {}, o.kind === "preorder" ? "đặt trước" : "có sẵn", o.source.startsWith("ai:") ? h("div", { class: "muted" }, "AI tạo") : null),
        h("td", {}, o.warehouse_code), h("td", {}, money(o.total)), h("td", {}, o.status === "completed" ? money(o.profit) : "—"), h("td", {}, pillOf(ORDER_STATUS, o.status)),
        h("td", {}, o.status === "confirmed" ? h("div", { class: "row" },
          h("button", { class: "small primary", onclick: () => step(o, "complete", "Đã giao, trừ kho") }, "Đã giao"),
          h("button", { class: "small danger", onclick: () => confirm(`Huỷ ${o.code}? Hàng giữ được trả về kho.`) && step(o, "cancel", "Đã huỷ") }, "Huỷ")) : null))))) : h("p", { class: "muted" }, "Chưa có đơn bán.")),
    h("div", { class: "card section" }, h("h2", {}, "Tạo đơn bán"),
      h("p", { class: "muted" }, "Giá lấy theo giai đoạn hiện tại của lô đang bán (hoặc nhập giá riêng). Đơn giữ hàng ngay; bấm Đã giao để trừ kho và tính lãi. Nhân viên AI tạo đơn qua hành động stock_order (cần duyệt)."),
      h("div", { class: "two" }, field("Loại đơn", f.kind), field("Kho xuất", f.wh), field("Khách", f.name), field("Điện thoại", f.phone), field("Địa chỉ", f.address), field("Giảm giá", f.discount)),
      h("h3", {}, "Sản phẩm"), lines, h("div", { class: "row" }, h("button", { onclick: line }, "+ Sản phẩm"), h("button", { class: "primary", onclick: create }, "Tạo đơn"))));
};

// ---------------------------------------------------------------- transfers

INV_VIEWS.transfers = async (box) => {
  const r = await api("GET", "/api/inventory/transfers");
  const products = (await api("GET", "/api/inventory/products")).products;
  const act = (t, s, body, msg) => run(async () => { await api("POST", `/api/inventory/transfers/${t.id}/${s}`, body || {}); await INV_VIEWS.transfers(box); }, msg);
  const receive = (t) => {
    const received = {};
    for (const it of t.items) {
      const v = ask(`${it.sku}: đã xuất ${it.qty}. Số nhận được:`, String(it.qty));
      if (v === null) return;
      received[it.id] = v;
    }
    act(t, "receive", { received }, "Đã ký nhận");
  };
  const wh = () => h("select", {}, invMeta.warehouses.filter((w) => w.active).map((w) => h("option", { value: w.id }, `${w.name}${w.kind === "store" ? " (cửa hàng)" : ""}`)));
  const from = wh(), to = wh();
  if (to.options.length > 1) to.selectedIndex = 1;
  const lines = h("div", {});
  const line = () => {
    const sel = h("select", {}, products.map((p) => h("option", { value: p.id }, `${p.sku} · ${p.name}`)));
    const qty = h("input", { value: 1, class: "narrow" });
    const el = h("div", { class: "row" }, sel, qty, h("button", { class: "small danger", onclick: () => el.remove() }, "✕"));
    el.data = () => ({ product_id: Number(sel.value), qty: qty.value });
    lines.append(el);
  };
  line();
  const note = h("input", { placeholder: "Ghi chú" });
  put(box,
    h("div", { class: "card table-wrap" }, r.transfers.length ? h("table", {},
      h("thead", {}, h("tr", {}, ["Phiếu", "Từ", "Đến", "Hàng", "Trạng thái", ""].map((x) => h("th", {}, x)))),
      h("tbody", {}, r.transfers.map((t) => h("tr", {},
        h("td", { class: "mono" }, t.code, h("div", { class: "muted" }, fmtTime(t.created))), h("td", {}, t.from_name), h("td", {}, t.to_name),
        h("td", {}, t.items.map((i) => `${i.sku} ×${i.qty}${t.status === "received" && i.qty_received !== i.qty ? ` (nhận ${i.qty_received})` : ""}`).join(", ")),
        h("td", {}, pillOf(TRANSFER_STATUS, t.status)),
        h("td", {}, h("div", { class: "row" },
          t.status === "requested" ? h("button", { class: "small primary", onclick: () => act(t, "ship", null, "Đã xuất kho") }, "Xuất kho") : null,
          t.status === "in_transit" ? h("button", { class: "small primary", onclick: () => receive(t) }, "Ký nhận") : null,
          ["requested", "in_transit"].includes(t.status) ? h("button", { class: "small danger", onclick: () => confirm("Huỷ phiếu?") && act(t, "cancel", null, "Đã huỷ") }, "Huỷ") : null)))))) : h("p", { class: "muted" }, "Chưa có phiếu chuyển kho.")),
    h("div", { class: "card section" }, h("h2", {}, "Tạo phiếu chuyển kho"),
      h("div", { class: "two" }, field("Từ kho", from), field("Đến kho / cửa hàng", to)), lines,
      h("div", { class: "row" }, h("button", { onclick: line }, "+ Sản phẩm"), note,
        h("button", { class: "primary", onclick: () => run(async () => {
          await api("POST", "/api/inventory/transfers", { from_wh: Number(from.value), to_wh: Number(to.value), note: note.value, items: [...lines.children].map((x) => x.data()) });
          await INV_VIEWS.transfers(box);
        }, "Đã tạo phiếu") }, "Tạo phiếu")),
      h("p", { class: "muted" }, "Xuất kho: trừ tồn kho đi. Cửa hàng kiểm đếm và ký nhận: cộng tồn kho đến; hàng thiếu được ghi vào sổ kho.")));
};

// ---------------------------------------------------------------- reorder

INV_VIEWS.reorder = async (box) => {
  const r = await api("GET", "/api/inventory/reorder");
  put(box, h("div", { class: "card table-wrap" },
    h("p", { class: "muted" }, `Điểm đặt hàng lại = tốc độ bán ${invMeta.settings.velocity_days} ngày × thời gian NCC giao + tồn an toàn. Gợi ý đặt đủ bán thêm ${invMeta.settings.cover_days} ngày sau khi hàng về.`),
    r.products.length ? h("table", {},
      h("thead", {}, h("tr", {}, ["SKU", "Sản phẩm", "Có thể bán", "Sắp về", "Bán/ngày", "NCC giao", "Điểm đặt lại", "Nên đặt", "Mức"].map((x) => h("th", {}, x)))),
      h("tbody", {}, r.products.map((p) => h("tr", {},
        h("td", { class: "mono" }, p.sku), h("td", {}, p.name), h("td", {}, p.available), h("td", {}, p.incoming - p.preordered),
        h("td", {}, p.daily_sales), h("td", {}, `${p.lead_time_days} ngày`), h("td", {}, p.reorder_point), h("td", {}, h("b", {}, p.suggest_order || "—")), h("td", {}, pillOf(LEVEL, p.level)))))) : h("p", { class: "muted" }, "Không sản phẩm nào cần đặt thêm.")));
};

// ---------------------------------------------------------------- pricing

INV_VIEWS.pricing = async (box) => {
  const s = invMeta.settings;
  const log = (await api("GET", "/api/inventory/pricing/log")).log;
  const f = { default_margin_pct: h("input", { value: s.default_margin_pct, class: "narrow" }), round_to: h("input", { value: s.round_to, class: "narrow" }),
    price_hour: h("input", { value: s.price_hour, class: "narrow" }), discounts: s.stage_discounts.map((d) => h("input", { value: d, class: "narrow" })) };
  f.discounts[0].disabled = true;
  const rules = Object.fromEntries(["1", "2", "3", "4"].map((k) => [k, {
    target_pct: h("input", { value: s.rules[k].target_pct, class: "narrow" }), min_days: h("input", { value: s.rules[k].min_days, class: "narrow" }),
    max_days: h("input", { value: s.rules[k].max_days, class: "narrow" }) }]));
  const save = () => run(async () => {
    invMeta.settings = await api("PUT", "/api/inventory/settings", {
      default_margin_pct: num(f.default_margin_pct), round_to: num(f.round_to), price_hour: num(f.price_hour),
      stage_discounts: f.discounts.map((x) => num(x) ?? 0),
      rules: Object.fromEntries(Object.entries(rules).map(([k, r]) => [k, { target_pct: num(r.target_pct), min_days: num(r.min_days), max_days: num(r.max_days) }])) });
  }, "Đã lưu cài đặt định giá");
  const runNow = () => run(async () => {
    const r = await api("POST", "/api/inventory/pricing/run", {});
    toast(r.changes.length ? `Đổi giá ${r.changes.length} sản phẩm` : "Chưa sản phẩm nào đến lúc đổi giá");
    await INV_VIEWS.pricing(box);
  });
  put(box,
    h("div", { class: "card section" }, h("h2", {}, "Định giá tự động 5 giai đoạn"),
      h("p", { class: "muted" }, "Mỗi đêm (giờ bên dưới) hệ thống xét từng sản phẩm đang tự định giá: lô đang bán đã hết thì lô kế tiếp lên bán ở giá giai đoạn 1 (hàng mới về); nếu chưa, lô chuyển sang giai đoạn kế tiếp khi hàng còn lại đã xuống dưới ngưỡng VÀ đã giữ giá đủ số ngày tối thiểu, HOẶC đã giữ giá quá số ngày tối đa (hàng bán chậm). Khách VIP được giá VIP riêng, hoặc giá của giai đoạn kế tiếp."),
      h("div", { class: "row" }, h("button", { class: "primary", onclick: runNow }, "Cập nhật giá ngay"))),
    h("div", { class: "card section" }, h("h2", {}, "Cài đặt"),
      h("div", { class: "row" }, field("Lãi mục tiêu mặc định (%)", f.default_margin_pct), field(`Làm tròn giá tới (${s.currency})`, f.round_to), field("Giờ chạy mỗi đêm (0-23)", f.price_hour)),
      h("h3", {}, "Mức giảm so với giá giai đoạn 1 (%)"),
      h("div", { class: "row" }, f.discounts.map((x, i) => field(`GĐ${i + 1}`, x))),
      h("h3", {}, "Quy tắc chuyển giai đoạn"),
      h("table", {}, h("thead", {}, h("tr", {}, ["Chuyển", "Hàng còn ≤ (%)", "Giữ giá tối thiểu (ngày)", "Giữ giá tối đa (ngày)"].map((x) => h("th", {}, x)))),
        h("tbody", {}, Object.entries(rules).map(([k, r]) => h("tr", {}, h("td", {}, `GĐ${k} → GĐ${Number(k) + 1}`), h("td", {}, r.target_pct), h("td", {}, r.min_days), h("td", {}, r.max_days))))),
      h("p", { class: "muted" }, "Từng sản phẩm có thể tắt tự định giá, hoặc được quản lý chuyển giai đoạn bằng tay trong trang sản phẩm."),
      h("button", { class: "primary", onclick: save }, "Lưu cài đặt")),
    h("div", { class: "card section table-wrap" }, h("h2", {}, "Lịch sử đổi giá"),
      log.length ? h("table", {}, h("tbody", {}, log.map((x) => h("tr", {}, h("td", {}, fmtTime(x.ts)), h("td", { class: "mono" }, x.sku), h("td", {}, TRIGGER[x.trigger] || x.trigger),
        h("td", {}, x.old_stage ? `GĐ${x.old_stage} ${money(x.old_price)}` : "—", " → ", x.new_stage ? `GĐ${x.new_stage} ${money(x.new_price)}` : "—"), h("td", {}, x.reason))))) : h("p", { class: "muted" }, "Chưa đổi giá lần nào.")));
};

// ---------------------------------------------------------------- warehouses and suppliers

INV_VIEWS.setup = async (box) => {
  const reload = () => go("inventory", "setup");
  const w = { code: h("input", { placeholder: "mã, vd. KHO1" }), name: h("input", { placeholder: "Tên kho / cửa hàng" }),
    kind: h("select", {}, h("option", { value: "warehouse" }, "Kho tổng"), h("option", { value: "store" }, "Cửa hàng (showroom)")), address: h("input", { placeholder: "Địa chỉ" }) };
  const s = { name: h("input", { placeholder: "Tên nhà cung cấp" }), country: h("input", { placeholder: "Quốc gia" }), contact_name: h("input", { placeholder: "Người liên hệ" }),
    phone: h("input", { placeholder: "Điện thoại" }), email: h("input", { placeholder: "Email" }), lead_time_days: h("input", { value: 30, class: "narrow" }),
    payment_terms: h("input", { placeholder: "Điều khoản thanh toán" }) };
  const vals = (o) => Object.fromEntries(Object.entries(o).map(([k, el]) => [k, el.value.trim()]));
  put(box,
    h("div", { class: "card section" }, h("h2", {}, "Kho và cửa hàng"),
      h("p", { class: "muted" }, "Mỗi cửa hàng là một kho con: hàng trưng bày và có sẵn tại chỗ. Tạm dừng một kho thì không bán, không nhập vào kho đó."),
      invMeta.warehouses.length ? h("table", {}, h("tbody", {}, invMeta.warehouses.map((x) => h("tr", {},
        h("td", { class: "mono" }, x.code), h("td", {}, h("b", {}, x.name), x.address ? h("div", { class: "muted" }, x.address) : null), h("td", {}, x.kind === "store" ? "cửa hàng" : "kho tổng"),
        h("td", {}, h("button", { class: "small", onclick: () => run(async () => { await api("PATCH", `/api/inventory/warehouses/${x.id}`, { active: !x.active }); reload(); }) }, x.active ? "Tạm dừng" : "Mở lại")))))) : null,
      h("div", { class: "two section" }, w.code, w.name, w.kind, w.address),
      h("button", { class: "primary", onclick: () => run(async () => { await api("POST", "/api/inventory/warehouses", vals(w)); reload(); }, "Đã thêm kho") }, "Thêm kho")),
    h("div", { class: "card section" }, h("h2", {}, "Nhà cung cấp"),
      invMeta.suppliers.length ? h("table", {}, h("tbody", {}, invMeta.suppliers.map((x) => h("tr", {},
        h("td", {}, h("b", {}, x.name), h("div", { class: "muted" }, [x.country, x.contact_name, x.phone, x.email].filter(Boolean).join(" · "))),
        h("td", {}, `giao ${x.lead_time_days} ngày`), h("td", {}, x.payment_terms),
        h("td", {}, h("div", { class: "row" },
          h("button", { class: "small", onclick: () => { const d = ask("Thời gian giao hàng (ngày):", String(x.lead_time_days)); if (d) run(async () => { await api("PATCH", `/api/inventory/suppliers/${x.id}`, { lead_time_days: d }); reload(); }); } }, "Đổi số ngày"),
          h("button", { class: "small", onclick: () => run(async () => { await api("PATCH", `/api/inventory/suppliers/${x.id}`, { active: !x.active }); reload(); }) }, x.active ? "Ngừng" : "Dùng lại"))))))) : null,
      h("div", { class: "two section" }, s.name, s.country, s.contact_name, s.phone, s.email, s.payment_terms, field("Thời gian giao hàng (ngày)", s.lead_time_days)),
      h("button", { class: "primary", onclick: () => run(async () => { await api("POST", "/api/inventory/suppliers", vals(s)); reload(); }, "Đã thêm nhà cung cấp") }, "Thêm nhà cung cấp")));
};

// ---------------------------------------------------------------- shop details, receipts, loyalty

INV_VIEWS.shop = async (box) => {
  const s = invMeta.settings;
  const f = Object.fromEntries(["shop_name", "shop_address", "shop_phone", "tax_code", "bank_info", "receipt_footer"].map((k) => [k, h("input", { value: s[k] || "" })]));
  const n = Object.fromEntries(["vat_pct", "undo_hours", "points_per", "vip_points", "set_eta_days"].map((k) => [k, h("input", { value: s[k], class: "price" })]));
  put(box, h("div", { class: "card section" }, h("h2", {}, "Thông tin trên hoá đơn"),
    h("div", { class: "two" }, field("Tên cửa hàng", f.shop_name), field("Địa chỉ", f.shop_address), field("Điện thoại", f.shop_phone), field("Mã số thuế", f.tax_code),
      field("Tài khoản ngân hàng (in trên hoá đơn còn nợ)", f.bank_info), field("Lời cảm ơn cuối hoá đơn", f.receipt_footer), field("VAT đã gồm trong giá (%)", n.vat_pct),
      field("Được hoàn tác đơn đã giao trong (giờ)", n.undo_hours)),
    h("h2", {}, "Tích điểm và VIP tự động"),
    h("div", { class: "two" }, field(`Mỗi điểm = số tiền mua (${s.currency})`, n.points_per), field("Đủ số điểm này thì lên VIP (0: không tự động)", n.vip_points),
      field("Gợi ý bộ: hàng về trong (ngày)", n.set_eta_days)),
    h("p", { class: "muted" }, "Điểm được cộng khi đơn đã giao; trả hàng thì trừ lại. Lên VIP: khách nhận lời chúc mừng kèm số thẻ trên kênh chat, quản lý được báo, và từ đơn sau được giá VIP."),
    h("button", { class: "primary", onclick: () => run(async () => {
      invMeta.settings = await api("PUT", "/api/inventory/settings", { ...Object.fromEntries(Object.entries(f).map(([k, el]) => [k, el.value])),
        ...Object.fromEntries(Object.entries(n).map(([k, el]) => [k, Number(el.value || 0)])) });
    }, "Đã lưu") }, "Lưu")));
};

// ---------------------------------------------------------------- marketplaces

INV_VIEWS.marketplaces = async (box) => {
  const r = await api("GET", "/api/inventory/marketplaces");
  const reload = () => go("inventory", "marketplaces");
  const kind = h("select", {}, h("option", { value: "amazon" }, "Amazon (SP-API)"), h("option", { value: "webhook" }, "Webhook (website của bạn, n8n…)"));
  const f = { id: h("input", { placeholder: "mã, vd. amazon-au" }), name: h("input", { placeholder: "tên" }), seller_id: h("input", { placeholder: "Seller ID" }),
    marketplace_id: h("input", { placeholder: "Marketplace ID, vd. A39IBJ37TRP1C6 (AU)" }), region: h("select", {}, ["fe", "eu", "na"].map((x) => h("option", { value: x }, x))),
    currency: h("input", { value: "AUD", class: "narrow" }), price_rate: h("input", { placeholder: `1 ${invMeta.settings.currency} = ? ngoại tệ` }),
    client_id_env: h("input", { value: "AMAZON_LWA_CLIENT_ID" }), client_secret_env: h("input", { value: "AMAZON_LWA_CLIENT_SECRET" }), refresh_token_env: h("input", { value: "AMAZON_REFRESH_TOKEN" }),
    pull_orders: h("input", { type: "checkbox", checked: true }), warehouse_id: h("select", {}, invMeta.warehouses.map((w) => h("option", { value: w.id }, w.name))),
    url: h("input", { placeholder: "https://…" }), secret_env: h("input", { placeholder: "tên biến môi trường chứa khoá ký" }) };
  const amazonFields = h("div", { class: "two" }, field("Seller ID", f.seller_id), field("Marketplace ID", f.marketplace_id), field("Vùng", f.region), field("Tiền tệ trên Amazon", f.currency),
    field("Tỷ giá quy đổi", f.price_rate), field("Kho giao đơn Amazon", f.warehouse_id), field("Biến môi trường: LWA client id", f.client_id_env),
    field("Biến môi trường: LWA client secret", f.client_secret_env), field("Biến môi trường: refresh token", f.refresh_token_env),
    h("label", { class: "check" }, f.pull_orders, h("span", {}, "Lấy đơn Amazon về (giữ hàng, giao xong thì trừ kho)")));
  const hookFields = h("div", { class: "two", hidden: true }, field("URL nhận dữ liệu", f.url), field("Biến môi trường chứa khoá ký HMAC", f.secret_env));
  kind.addEventListener("change", () => { amazonFields.hidden = kind.value !== "amazon"; hookFields.hidden = kind.value !== "webhook"; });
  const save = () => run(async () => {
    const v = (el) => (el.type === "checkbox" ? el.checked : el.value);
    await api("POST", "/api/inventory/marketplaces", { type: kind.value, ...Object.fromEntries(Object.entries(f).map(([k, el]) => [k, v(el)])) });
    reload();
  }, "Đã lưu sàn");
  put(box,
    h("div", { class: "card section" }, h("h2", {}, "Sàn đã kết nối"),
      h("p", { class: "muted" }, "Mỗi khi giá (định giá tự động, khuyến mại) hoặc tồn kho thay đổi, sản phẩm được đẩy lên các sàn trong vòng 1 phút. Khoá bí mật chỉ đặt trong biến môi trường; ở đây chỉ lưu tên biến."),
      r.marketplaces.length ? h("table", {}, h("tbody", {}, r.marketplaces.map((x) => h("tr", {},
        h("td", {}, h("b", {}, x.name), ` ${x.type}`, h("div", { class: "muted" }, x.type === "amazon" ? `${x.seller_id} · ${x.marketplace_id} · ${x.currency}` : x.url)),
        h("td", {}, Object.entries(x.env_set).map(([k, ok]) => h("span", { class: `pill ${ok ? "ok" : "bad"}` }, `${x[k]} ${ok ? "✓" : "chưa đặt"}`))),
        h("td", {}, `${x.queued} chờ đẩy`, x.last ? h("div", { class: "muted" }, `${fmtTime(x.last.ts)} · ${x.last.ok ? "ổn" : "lỗi"}: ${x.last.detail}`) : null),
        h("td", {}, h("div", { class: "row" },
          h("button", { class: "small", onclick: () => run(async () => { const s = await api("POST", `/api/inventory/marketplaces/${x.id}/sync`, {}); toast(`Đã đẩy ${s.pushed}/${s.queued}`); reload(); }) }, "Đồng bộ tất cả"),
          h("button", { class: "small danger", onclick: () => confirm("Gỡ sàn này?") && run(async () => { await api("DELETE", `/api/inventory/marketplaces/${x.id}`); reload(); }) }, "Gỡ"))))))) : h("p", { class: "muted" }, "Chưa kết nối sàn nào.")),
    h("div", { class: "card section" }, h("h2", {}, "Kết nối sàn"), h("div", { class: "two" }, field("Loại", kind), field("Mã", f.id), field("Tên", f.name)), amazonFields, hookFields,
      h("p", { class: "muted" }, "Amazon: tạo ứng dụng SP-API trong Seller Central, lấy LWA client id/secret và refresh token của người bán, đặt vào biến môi trường. Mỗi sản phẩm có thể dùng SKU khác trên sàn, hoặc '-' để không bán trên sàn đó (trong trang sản phẩm)."),
      h("button", { class: "primary", onclick: save }, "Lưu")),
    h("div", { class: "card section table-wrap" }, h("h2", {}, "Nhật ký đồng bộ"),
      r.log.length ? h("table", {}, h("tbody", {}, r.log.map((x) => h("tr", {}, h("td", {}, fmtTime(x.ts)), h("td", {}, x.marketplace), h("td", { class: "mono" }, x.sku),
        h("td", {}, x.ok ? pillOf({ 1: ["ok", "ổn"] }, 1) : pillOf({ 0: ["bad", "lỗi"] }, 0)), h("td", {}, x.detail))))) : h("p", { class: "muted" }, "Chưa có.")));
};

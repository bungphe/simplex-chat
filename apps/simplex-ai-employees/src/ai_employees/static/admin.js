"use strict";
// Admin UI for AI employees. No framework; every value from the server is rendered
// with textContent (never innerHTML), so chat content cannot inject markup.

const $ = (sel) => document.querySelector(sel);

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "value") el.value = v;
    else if (k === "checked") el.checked = !!v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

async function api(method, path, body) {
  const opts = { method, headers: { "X-Requested-With": "ai-employees" }, credentials: "same-origin" };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(path, opts);
  let data = {};
  try { data = await r.json(); } catch (_) { /* empty body */ }
  if (r.status === 401 && path !== "/api/login") { showLogin(); throw new Error("Hết phiên đăng nhập"); }
  if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
  return data;
}

let toastTimer;
function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, 3500);
}

async function run(fn, okMsg) {
  try {
    const res = await fn();
    if (okMsg) toast(okMsg);
    return res;
  } catch (e) {
    toast("Lỗi: " + e.message);
  }
}

const fmtTime = (iso) => (iso ? new Date(iso).toLocaleString("vi-VN", { dateStyle: "short", timeStyle: "short" }) : "—");
const STATUS = {
  ok: ["ok", "thành công"], busy: ["warn", "bận"], refused: ["warn", "từ chối"], step_limit: ["warn", "quá bước"],
  error: ["bad", "lỗi"], queued: ["neutral", "chờ duyệt"], rejected: ["neutral", "bị từ chối"], skipped: ["neutral", "bỏ qua"],
  pending: ["warn", "chờ duyệt"], executing: ["neutral", "đang chạy"], done: ["ok", "đã làm"], failed: ["bad", "thất bại"],
};
const KIND = { reply: "trả lời", consult: "hỏi đồng nghiệp", routine: "lịch làm việc", action: "hành động", suggest: "gợi ý trả lời", memory: "tóm tắt trí nhớ", translate: "dịch", summary: "tóm tắt hội thoại" };
function pill(status) {
  const [cls, label] = STATUS[status] || ["neutral", status || "—"];
  return h("span", { class: `pill ${cls}` }, label);
}

// --------------------------------------------------------------------------
// Login and navigation

function showLogin() {
  $("#app").hidden = true;
  $("#login").hidden = false;
  $("#login-password").focus();
}

$("#login-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  $("#login-error").textContent = "";
  try {
    await api("POST", "/api/login", { username: $("#login-username").value.trim(), password: $("#login-password").value });
    $("#login-password").value = "";
    await start();
  } catch (e) {
    $("#login-error").textContent = e.message;
  }
});

$("#logout").addEventListener("click", async () => {
  await run(() => api("POST", "/api/logout", {}));
  clearInterval(refreshTimer);
  me = null;
  showLogin();
});

const views = {};
let current = "overview";
let refreshTimer;

function go(view, arg) {
  current = view;
  for (const b of document.querySelectorAll("#nav button")) b.classList.toggle("active", b.dataset.view === view);
  clearInterval(refreshTimer);
  views[view](arg);
  refreshBadge();
}

for (const b of document.querySelectorAll("#nav button")) b.addEventListener("click", () => go(b.dataset.view));

async function refreshBadge() {
  try {
    if (me && me.role === "admin") {
      const { pending } = await api("GET", "/api/approvals");
      $("#badge").hidden = pending.length === 0;
      $("#badge").textContent = pending.length;
    }
    const { channels } = await api("GET", "/api/channels");
    const unread = channels.reduce((a, c) => a + ((c.stats || {}).unread || 0), 0);
    $("#inbox-badge").hidden = unread === 0;
    $("#inbox-badge").textContent = unread;
  } catch (_) { /* shown elsewhere */ }
}
setInterval(() => !$("#app").hidden && refreshBadge(), 20000);

// The logged-in account: {username, name, role: admin|agent, channels}
let me = null;
const ROLE = { admin: "Quản trị", agent: "Nhân viên bán hàng" };
$("#me").addEventListener("click", () => go("me"));

// replaceChildren would print "null" for a skipped optional node, so drop them first.
function put(el, ...nodes) {
  el.replaceChildren(...nodes.flat().filter((n) => n !== null && n !== undefined && n !== false));
}

function render(...nodes) {
  put($("#view"), ...nodes);
}

// --------------------------------------------------------------------------
// Overview

views.overview = async () => {
  const load = async () => {
    const { employees } = await api("GET", "/api/overview");
    const sum = (f) => employees.reduce((a, e) => a + f(e), 0);
    const errors = sum((e) => (e.stats.by_status || {}).error || 0) + sum((e) => (e.stats.by_status || {}).busy || 0);
    const tiles = h("div", { class: "tiles" },
      tile(sum((e) => (e.paused ? 0 : 1)) + "/" + employees.length, "nhân viên đang làm việc"),
      tile(sum((e) => e.stats.total || 0), "việc đã xử lý (24 giờ)"),
      tile(sum((e) => e.pending), "yêu cầu chờ duyệt"),
      tile(errors, "lỗi hoặc bận (24 giờ)"),
    );
    const cards = employees.map((e) => {
      const next = e.routines.filter((r) => r.next && !r.paused).sort((a, b) => a.next.localeCompare(b.next))[0];
      // routine times are shown in the employee's own time zone, as the server formats them
      return h("div", { class: "card" },
        h("div", { class: "row spread" }, h("h2", {}, e.display_name), e.paused ? pill("skipped") : h("span", { class: "pill ok" }, "đang làm")),
        e.short_descr && h("p", { class: "muted" }, e.short_descr),
        h("p", {}, "Model: ", h("b", {}, e.model), h("span", { class: "muted" }, " · " + e.model_desc)),
        h("p", {}, `24 giờ: ${e.stats.total || 0} việc · ${e.contacts} khách · ${e.admins} quản trị viên`),
        e.pending ? h("p", {}, h("span", { class: "pill warn" }, `${e.pending} chờ duyệt`)) : null,
        e.memory_pending ? h("p", {}, h("span", { class: "pill warn" }, `${e.memory_pending} ghi nhớ chờ duyệt`)) : null,
        next && h("p", { class: "muted" }, `Lịch tới: ${next.id} lúc ${next.next_local}`),
        e.address && h("details", {}, h("summary", { class: "muted" }, "Địa chỉ liên hệ SimpleX"), h("p", { class: "mono" }, e.address),
          h("button", { onclick: () => navigator.clipboard.writeText(e.address).then(() => toast("Đã chép địa chỉ")) }, "Chép")),
        h("div", { class: "row" }, h("button", { onclick: () => go("employees", e.id) }, "Quản lý")),
      );
    });
    render(h("h1", {}, "Tổng quan"), tiles, h("div", { class: "grid" }, cards));
  };
  await run(load);
  refreshTimer = setInterval(() => current === "overview" && run(load), 15000);
};

function tile(num, label) {
  return h("div", { class: "card tile" }, h("div", { class: "num" }, num), h("div", { class: "lbl" }, label));
}

// --------------------------------------------------------------------------
// Employees

let selectedEmployee = null;

views.employees = async (id) => {
  const { employees } = await run(() => api("GET", "/api/overview")) || { employees: [] };
  if (!employees.length) return render(h("p", {}, "Chưa có nhân viên."));
  selectedEmployee = id || selectedEmployee || employees[0].id;
  const list = h("div", { class: "list" }, employees.map((e) =>
    h("button", { class: e.id === selectedEmployee ? "active" : "", onclick: () => go("employees", e.id) }, e.display_name)));
  const panel = h("div", {}, h("p", { class: "muted" }, "Đang tải…"));
  render(h("h1", {}, "Nhân viên"), h("div", { class: "split" }, list, panel));
  const d = await run(() => api("GET", `/api/employees/${encodeURIComponent(selectedEmployee)}`));
  if (d) put(panel, employeeForm(d));
};

function employeeForm(d) {
  const base = `/api/employees/${encodeURIComponent(d.id)}`;
  const patch = (body, msg) => run(async () => { await api("PATCH", base, body); go("employees", d.id); }, msg);

  const prompt = h("textarea", { rows: 10 }, d.system_prompt);
  const model = h("select", {}, d.models.map((m) => h("option", { value: m, selected: m === d.model }, m)));
  if (!d.models.includes(d.model)) model.append(h("option", { value: d.model, selected: true }, d.model));
  const effort = h("select", {}, h("option", { value: "" }, "mặc định / tắt"),
    d.effort_levels.map((l) => h("option", { value: l, selected: l === d.effort }, l)));

  const skillBoxes = d.available_skills.map((s) => {
    const box = h("input", { type: "checkbox", value: s.name, checked: d.skills.includes(s.name) });
    return { box, el: h("label", { class: "check" }, box, h("span", {}, s.name, h("small", {}, s.description))) };
  });
  const releaseBoxes = d.actions.map((a) => {
    const box = h("input", { type: "checkbox", value: a, checked: d.releases.includes(a) });
    return { box, el: h("label", { class: "check" }, box, h("span", {}, a, h("small", {}, "Tự thực hiện, không cần duyệt"))) };
  });
  const correction = h("input", { placeholder: "Ví dụ: Không báo giá lõi lọc qua chat" });

  return h("div", {},
    h("div", { class: "card" },
      h("div", { class: "row spread" },
        h("div", {}, h("h2", {}, d.display_name), h("div", { class: "muted" }, `${d.id} · ${d.model_desc}`)),
        h("div", { class: "row" },
          d.paused ? pill("skipped") : h("span", { class: "pill ok" }, "đang làm"),
          h("button", { onclick: () => patch({ paused: !d.paused }, d.paused ? "Đã bật lại" : "Đã tạm dừng") }, d.paused ? "Bật lại" : "Tạm dừng"),
        ),
      ),
      h("div", { class: "two section" },
        h("label", {}, "Model AI", model),
        h("label", {}, "Mức suy nghĩ (chỉ Claude)", effort),
      ),
      h("button", { class: "primary", onclick: () => patch({ model: model.value, effort: effort.value || null }, "Đã lưu model") }, "Lưu model"),
      h("h3", {}, "Vai trò (system prompt)"),
      prompt,
      h("div", { class: "row section" }, h("button", { class: "primary", onclick: () => patch({ system_prompt: prompt.value }, "Đã lưu vai trò") }, "Lưu vai trò")),
    ),

    h("div", { class: "card section" },
      h("h2", {}, "Quy tắc sửa sai"),
      h("p", { class: "muted" }, "Quy tắc có ngày, được ưu tiên hơn vai trò. Có hiệu lực từ tin nhắn tiếp theo."),
      d.corrections.length ? h("ol", {}, d.corrections.map((c, i) => h("li", {}, `(${c.date}) ${c.text} `,
        h("button", { class: "danger", onclick: () => run(async () => { await api("DELETE", `${base}/corrections/${i + 1}`); go("employees", d.id); }, "Đã xoá") }, "Xoá")))) : h("p", { class: "muted" }, "Chưa có."),
      h("div", { class: "row" }, correction, h("button", { onclick: () => correction.value.trim() && run(async () => { await api("POST", `${base}/corrections`, { text: correction.value }); go("employees", d.id); }, "Đã thêm quy tắc") }, "Thêm")),
    ),

    sharedMemoryCard(d),

    h("div", { class: "card section" },
      h("h2", {}, "Skill"),
      h("div", { class: "checks" }, skillBoxes.map((s) => s.el)),
      h("div", { class: "row section" }, h("button", { class: "primary", onclick: () => patch({ skills: skillBoxes.filter((s) => s.box.checked).map((s) => s.box.value) }, "Đã lưu skill") }, "Lưu skill")),
      d.actions.length ? h("div", {},
        h("h3", {}, "Hành động được tự làm"),
        h("p", { class: "muted" }, "Hành động không được đánh dấu sẽ chờ quản lý duyệt trước khi thực hiện."),
        h("div", { class: "checks" }, releaseBoxes.map((s) => s.el)),
        h("div", { class: "row section" }, h("button", { onclick: () => patch({ releases: releaseBoxes.filter((s) => s.box.checked).map((s) => s.box.value) }, "Đã lưu") }, "Lưu")),
      ) : null,
    ),

    h("div", { class: "card section" },
      h("h2", {}, "Lịch làm việc"),
      d.routines.length ? d.routines.map((r) => routineRow(d, r)) : h("p", { class: "muted" }, "Chưa có lịch. Thêm trong file cấu hình (routines:)."),
    ),

    h("div", { class: "card section" },
      h("h2", {}, "Quản trị viên trong chat"),
      d.admin_contacts.length ? h("ul", {}, d.admin_contacts.map((a) => h("li", {}, a.name + " ",
        h("button", { class: "danger", onclick: () => confirm(`Gỡ quyền quản trị của ${a.name}?`) && run(async () => { await api("DELETE", `${base}/admins/${a.id}`); go("employees", d.id); }, "Đã gỡ") }, "Gỡ")))) :
        h("p", { class: "muted" }, "Chưa có. Nhắn /admin <mã> cho nhân viên trong SimpleX để đăng nhập."),
    ),

    h("div", { class: "row section" },
      h("span", { class: "muted" }, d.overrides.length ? `Đã thay đổi so với file cấu hình: ${d.overrides.join(", ")}` : "Đang dùng đúng file cấu hình."),
      d.overrides.length ? h("button", { class: "danger", onclick: () => confirm("Bỏ mọi thay đổi và quay về file cấu hình?") && run(async () => { await api("POST", `${base}/reset`, {}); go("employees", d.id); }, "Đã khôi phục") }, "Khôi phục cấu hình gốc") : null,
    ),
  );
}

function sharedMemoryCard(d) {
  const base = `/api/employees/${encodeURIComponent(d.id)}/memory`;
  const redo = (p) => run(async () => { await p(); go("employees", d.id); });
  const input = h("input", { placeholder: "vd. Khách hỏi lắp đặt ngoại thành: phí 200.000đ, hẹn trong 2 ngày" });
  const pending = d.shared_memory.filter((m) => m.status === "pending");
  const active = d.shared_memory.filter((m) => m.status === "active");
  const item = (m) => h("li", {}, m.text, " ", h("span", { class: "muted" }, `(${m.source}, ${fmtTime(m.created)})`), " ",
    m.status === "pending" ? h("button", { class: "primary small", onclick: () => redo(() => api("POST", `${base}/${m.id}/approve`, {})) }, "Duyệt") : null,
    m.status === "pending" ? h("button", { class: "small", onclick: () => { const t = prompt("Sửa rồi duyệt:", m.text); if (t) redo(() => api("POST", `${base}/${m.id}/approve`, { text: t })); } }, "Sửa & duyệt") : null,
    h("button", { class: "danger small", onclick: () => confirm("Xoá ghi nhớ này?") && redo(() => api("DELETE", `${base}/${m.id}`)) }, m.status === "pending" ? "Bỏ" : "Xoá"));
  return h("div", { class: "card section" },
    h("h2", {}, "Ghi nhớ chung ", pending.length ? h("span", { class: "pill warn" }, `${pending.length} chờ duyệt`) : null),
    h("p", { class: "muted" }, "Điều nhân viên AI đã học, dùng cho mọi khách. AI tự đề xuất (skill learn) và chỉ dùng sau khi bạn duyệt, để khách không thể 'dạy' AI điều sai. Quy tắc sửa sai vẫn được ưu tiên hơn."),
    pending.length ? h("div", {}, h("h3", {}, "Chờ duyệt"), h("ul", {}, pending.map(item))) : null,
    h("h3", {}, "Đang dùng"),
    active.length ? h("ul", {}, active.map(item)) : h("p", { class: "muted" }, "Chưa có."),
    h("div", { class: "row" }, input, h("button", { onclick: () => input.value.trim() && redo(() => api("POST", base, { text: input.value.trim() })) }, "Thêm")),
  );
}

function routineRow(d, r) {
  const base = `/api/employees/${encodeURIComponent(d.id)}/routines/${encodeURIComponent(r.id)}`;
  const out = h("pre", { class: "output", hidden: !r.last_output }, r.last_output || "");
  const runBtn = h("button", {}, "Chạy ngay");
  runBtn.addEventListener("click", () => run(async () => {
    runBtn.disabled = true;
    runBtn.textContent = "Đang chạy…";
    try {
      const res = await api("POST", `${base}/run`, {});
      out.textContent = res.text;
      out.hidden = false;
    } finally {
      runBtn.disabled = false;
      runBtn.textContent = "Chạy ngay";
    }
  }, "Đã chạy xong"));
  return h("div", { class: "section" },
    h("div", { class: "row spread" },
      h("div", {}, h("b", {}, r.id), " ", h("span", { class: "muted" }, r.schedule), " ", r.paused ? pill("skipped") : null),
      h("div", { class: "row" }, runBtn,
        h("button", { onclick: () => run(async () => { await api("POST", `${base}/pause`, { paused: !r.paused }); go("employees", d.id); }) }, r.paused ? "Bật lại" : "Tạm dừng")),
    ),
    h("div", { class: "muted" }, `Lần tới: ${r.next_local || "—"} (${r.timezone}) · Lần trước: ${fmtTime(r.last_run)} `, r.last_status ? pill(r.last_status) : null),
    h("details", {}, h("summary", { class: "muted" }, "Nhiệm vụ và kết quả gần nhất"), h("p", {}, r.task), out),
  );
}

// --------------------------------------------------------------------------
// Approvals

views.approvals = async () => {
  const data = await run(() => api("GET", "/api/approvals"));
  if (!data) return;
  const decide = (a, decision) => run(async () => {
    let body = {};
    if (decision === "reject") {
      const reason = prompt("Lý do từ chối (sẽ gửi cho khách):", "");
      if (reason === null) return;
      body = { reason };
    }
    const res = await api("POST", `/api/approvals/${encodeURIComponent(a.employee)}/${a.id}/${decision}`, body);
    toast(res.message);
    go("approvals");
  });
  const args = (a) => Object.entries(a.args || {}).map(([k, v]) => h("div", {}, h("span", { class: "muted" }, k + ": "), v));
  render(
    h("h1", {}, "Chờ duyệt"),
    h("div", { class: "card" },
      data.pending.length ? h("div", { class: "table-wrap" }, h("table", {},
        h("thead", {}, h("tr", {}, ["#", "Nhân viên", "Hành động", "Khách", "Nội dung", "Lúc", ""].map((t) => h("th", {}, t)))),
        h("tbody", {}, data.pending.map((a) => h("tr", {},
          h("td", {}, a.id), h("td", {}, a.employee_name), h("td", {}, a.action), h("td", {}, a.contact_name || "—"),
          h("td", {}, args(a)), h("td", {}, fmtTime(a.created)),
          h("td", {}, h("div", { class: "row" },
            h("button", { class: "primary", onclick: () => decide(a, "approve") }, "Duyệt"),
            h("button", { class: "danger", onclick: () => decide(a, "reject") }, "Từ chối"))),
        ))))) : h("p", { class: "muted" }, "Không có yêu cầu nào chờ duyệt."),
    ),
    h("h2", { class: "section" }, "Đã xử lý gần đây"),
    h("div", { class: "card table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, ["#", "Nhân viên", "Hành động", "Khách", "Trạng thái", "Người quyết định", "Kết quả"].map((t) => h("th", {}, t)))),
      h("tbody", {}, data.recent.map((a) => h("tr", {},
        h("td", {}, a.id), h("td", {}, a.employee_name), h("td", {}, a.action), h("td", {}, a.contact_name || "—"),
        h("td", {}, pill(a.status)), h("td", {}, a.decided_by || "—"), h("td", { class: "mono" }, a.result || a.reason || "")))),
    )),
  );
};

// --------------------------------------------------------------------------
// Models

views.models = async () => {
  const data = await run(() => api("GET", "/api/models"));
  if (!data) return;
  const f = {
    name: h("input", { placeholder: "vd. deepseek" }),
    provider: h("select", {}, data.providers.map((p) => h("option", { value: p, selected: p === "openai" }, p === "openai" ? "openai (chuẩn OpenAI)" : p))),
    model: h("input", { placeholder: "tên model của nhà cung cấp" }),
    base_url: h("input", { placeholder: "https://api.deepseek.com/v1" }),
    api_key: h("input", { type: "password", autocomplete: "off", placeholder: "để trống nếu dùng biến môi trường" }),
    api_key_env: h("input", { placeholder: "vd. DEEPSEEK_API_KEY" }),
    extra_body: h("input", { placeholder: '{"temperature": 0.3}' }),
  };
  const add = () => run(async () => {
    const body = Object.fromEntries(Object.entries(f).map(([k, el]) => [k, el.value.trim()]));
    await api("POST", "/api/models", body);
    go("models");
  }, "Đã thêm model");
  const test = (m, btn) => run(async () => {
    btn.disabled = true;
    btn.textContent = "Đang thử…";
    try {
      const r = await api("POST", `/api/models/${encodeURIComponent(m.name)}/test`, {});
      toast(r.ok ? `${m.name}: kết nối được (${r.ms} ms) — "${r.text}"` : `${m.name}: ${r.error}`);
    } finally {
      btn.disabled = false;
      btn.textContent = "Thử kết nối";
    }
  });
  render(
    h("h1", {}, "Model AI"),
    h("div", { class: "card table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, ["Tên", "Nhà cung cấp", "Model", "Địa chỉ API", "API key", "Nguồn", "Đang dùng", ""].map((t) => h("th", {}, t)))),
      h("tbody", {}, data.models.map((m) => {
        const btn = h("button", {}, "Thử kết nối");
        btn.addEventListener("click", () => test(m, btn));
        return h("tr", {},
          h("td", {}, h("b", {}, m.name)), h("td", {}, m.provider), h("td", { class: "mono" }, m.model),
          h("td", { class: "mono" }, m.base_url || "mặc định"),
          h("td", {}, m.has_key ? h("span", { class: "pill ok" }, "đã có") : h("span", { class: "pill neutral" }, m.api_key_env ? `thiếu ${m.api_key_env}` : "không cần")),
          h("td", {}, m.source === "ui" ? "giao diện" : "file cấu hình"),
          h("td", {}, m.used_by.join(", ") || "—"),
          h("td", {}, h("div", { class: "row" }, btn,
            m.source === "ui" ? h("button", { class: "danger", onclick: () => confirm(`Xoá model ${m.name}?`) && run(async () => { await api("DELETE", `/api/models/${encodeURIComponent(m.name)}`); go("models"); }, "Đã xoá") }, "Xoá") : null)),
        );
      })),
    )),
    h("div", { class: "card section" },
      h("h2", {}, "Thêm model"),
      h("p", { class: "muted" }, "Dùng 'openai' cho mọi API chuẩn OpenAI: OpenAI, Gemini, DeepSeek, Groq, OpenRouter, Ollama… API key nhập ở đây được lưu trên máy chủ và không bao giờ hiển thị lại."),
      h("div", { class: "two" },
        h("label", {}, "Tên", f.name), h("label", {}, "Nhà cung cấp", f.provider),
        h("label", {}, "Model", f.model), h("label", {}, "Địa chỉ API (base_url)", f.base_url),
        h("label", {}, "API key", f.api_key), h("label", {}, "Hoặc tên biến môi trường chứa key", f.api_key_env),
        h("label", {}, "Tham số thêm (JSON)", f.extra_body),
      ),
      h("button", { class: "primary", onclick: add }, "Thêm model"),
    ),
  );
};

// --------------------------------------------------------------------------
// Conversations

views.conversations = async (arg) => {
  const { employees } = await run(() => api("GET", "/api/overview")) || { employees: [] };
  if (!employees.length) return render(h("p", {}, "Chưa có nhân viên."));
  const empId = (arg && arg.emp) || selectedEmployee || employees[0].id;
  const sel = h("select", { onchange: () => go("conversations", { emp: sel.value }) },
    employees.map((e) => h("option", { value: e.id, selected: e.id === empId }, e.display_name)));
  const listBox = h("div", { class: "list" });
  const panel = h("div", { class: "card" }, h("p", { class: "muted" }, "Chọn một khách để xem hội thoại."));
  render(h("h1", {}, "Hội thoại"), h("label", {}, "Nhân viên", sel), h("div", { class: "split" }, listBox, panel));
  const base = `/api/employees/${encodeURIComponent(empId)}/conversations`;
  const { contacts } = await run(() => api("GET", base)) || { contacts: [] };
  const open = async (c) => {
    for (const b of listBox.children) b.classList.toggle("active", b.dataset.id === String(c.id));
    const conv = await run(() => api("GET", `${base}/${c.id}`));
    if (!conv) return;
    put(panel,
      h("div", { class: "row spread" }, h("h2", {}, conv.name),
        h("button", { class: "danger", onclick: () => confirm(`Xoá lịch sử và ghi chú về ${conv.name}?`) && run(async () => { await api("DELETE", `${base}/${c.id}`); go("conversations", { emp: empId }); }, "Đã xoá") }, "Xoá trí nhớ")),
      conv.summary ? h("div", { class: "section" }, h("h3", {}, "Tóm tắt dài hạn"), h("p", { class: "pre" }, conv.summary)) : null,
      Object.keys(conv.notes).length ? h("div", { class: "section" }, h("h3", {}, "Ghi chú về khách"),
        h("ul", {}, Object.entries(conv.notes).map(([k, v]) => h("li", {}, h("b", {}, k + ": "), v)))) : null,
      h("div", { class: "transcript section" }, conv.turns.length ? conv.turns.map((t) =>
        h("div", { class: `bubble ${t.role}`, title: fmtTime(t.ts) }, t.content)) : h("p", { class: "muted" }, "Không còn tin nhắn nào trong trí nhớ.")),
    );
  };
  put(listBox, ...(contacts.length ? contacts.map((c) => h("button", { "data-id": c.id, onclick: () => open(c) },
    c.name, h("div", { class: "muted" }, `${c.turns} lượt · ${fmtTime(c.last)}${c.admin ? " · quản trị" : ""}`))) : [h("p", { class: "muted" }, "Chưa có hội thoại.")]));
};

// --------------------------------------------------------------------------
// Unified inbox: every channel's conversations in one place

const CH_SHORT = { simplex: "SimpleX", zalo_oa: "Zalo OA", zalo_personal: "Zalo", facebook: "Messenger", webhook: "Web",
  telegram: "Telegram", whatsapp: "WhatsApp", email: "Email" };
const SENDER = { customer: "Khách", ai: "AI", human: "Nhân viên", system: "Hệ thống", note: "Ghi chú nội bộ" };
let inboxSel = null;

function chBadge(info) {
  return h("span", { class: `ch ch-${info.type}`, title: info.name }, CH_SHORT[info.type] || info.type);
}

function modePill(mode) {
  return mode === "human" ? h("span", { class: "pill warn" }, "người trả lời") : h("span", { class: "pill ok" }, "AI trả lời");
}

const ATT_LABEL = { image: "Ảnh", video: "Video", audio: "Ghi âm", file: "Tệp", sticker: "Sticker", link: "Link" };

// One attachment of a customer message. Remote files come through this server
// (/api/inbox/.../media/...), since the page may only load images from itself.
function attachmentView(cid, m, a, i) {
  const media = `/api/inbox/${cid}/media/${m.id}/${i}`;
  // http(s) links, and files only the channel can download (telegram:<file id>)
  const remote = (u) => typeof u === "string" && /^(https?:\/\/|telegram:)/.test(u);
  const label = `${ATT_LABEL[a.kind] || "Tệp"}${a.name ? ": " + a.name : ""}`;
  if (a.kind === "link" && /^https?:\/\//.test(a.url || "")) {
    return h("a", { class: "att-link", href: a.url, target: "_blank", rel: "noopener noreferrer" },
      a.thumb && a.thumb.startsWith("data:image/") ? h("img", { src: a.thumb, alt: "" }) : null, label);
  }
  if (["image", "sticker", "video"].includes(a.kind) && (a.thumb || remote(a.url))) {
    const src = a.thumb && a.thumb.startsWith("data:image/") ? a.thumb : remote(a.thumb) ? `${media}?thumb=1` : media;
    const img = h("img", { src, alt: label, loading: "lazy", class: "att-img" });
    img.addEventListener("error", () => img.replaceWith(h("span", { class: "att-file" }, `${label} (hết hạn hoặc không tải được)`)));
    return remote(a.url) ? h("a", { href: media, target: "_blank", rel: "noopener", title: "Mở bản đầy đủ" }, img) : img;
  }
  if (remote(a.url)) return h("a", { class: "att-file", href: media, rel: "noopener" }, "⬇ " + label);
  return h("span", { class: "att-file" }, `${label} — xem trên ứng dụng của kênh`);
}

// A customer who writes in another language than the staff: translation helpers apply.
const foreign = (c) => !!c.lang && c.lang !== c.staff_language;

// Inbox settings shared by the inbox views: labels, saved replies, teams, staff accounts.
let deskMeta = { labels: [], canned: [], teams: [], users: [], my_teams: [], sla_minutes: 15 };
const labelColor = (name) => (deskMeta.labels.find((l) => l.name === name) || {}).color || "#64748b";
// colours through the CSSOM: the page's CSP forbids inline style attributes
const colored = (el, color) => { el.style.backgroundColor = color; return el; };
const labelChip = (name) => colored(h("span", { class: "label" }, name), labelColor(name));
const userName = (u) => (deskMeta.users.find((x) => x.username === u) || {}).name || u;
const teamName = (t) => (deskMeta.teams.find((x) => x.id === t) || {}).name || t;
const fmtDur = (s) => (s < 60 ? `${s}s` : s < 3600 ? `${Math.round(s / 60)} phút` : `${(s / 3600).toFixed(1)} giờ`);
const waitingFor = (iso) => (iso ? fmtDur(Math.max(0, Math.round((Date.now() - new Date(iso)) / 1000))) : "");

views.inbox = async (arg) => {
  const langs = await api("GET", "/api/inbox/languages").catch(() => ({ languages: [], countries: [] }));
  if (arg) inboxSel = arg;
  const chSel = h("select", {}, h("option", { value: "" }, "Mọi kênh"));
  const modeSel = h("select", {}, h("option", { value: "" }, "Mọi chế độ"),
    h("option", { value: "ai" }, "AI đang trả lời"), h("option", { value: "human" }, "Người đang trả lời"));
  const search = h("input", { type: "search", placeholder: "Tìm khách hoặc nội dung" });
  deskMeta = await api("GET", "/api/inbox/meta").catch(() => deskMeta);
  const statusSel = h("select", {}, h("option", { value: "open" }, "Đang mở"), h("option", { value: "closed" }, "Đã đóng"),
    h("option", { value: "" }, "Mọi trạng thái"));
  const whoSel = h("select", {}, h("option", { value: "" }, "Mọi người phụ trách"), h("option", { value: "me" }, "Của tôi"),
    h("option", { value: "-" }, "Chưa giao cho ai"), h("option", { value: "waiting" }, "Khách đang chờ"),
    deskMeta.teams.length ? h("optgroup", { label: "Nhóm" }, deskMeta.teams.map((t) => h("option", { value: `team:${t.id}` }, t.name))) : null);
  const labelSel = h("select", {}, h("option", { value: "" }, "Mọi nhãn"), deskMeta.labels.map((l) => h("option", { value: l.name }, l.name)));
  const listBox = h("div", { class: "conv-list" });
  const pane = h("div", { class: "card thread" }, h("p", { class: "muted" }, "Chọn một hội thoại ở bên trái."));
  let channelsLoaded = false;
  let thread = null; // {id, head, msgs, side}

  const loadList = async () => {
    const q = new URLSearchParams({ channel: chSel.value, mode: modeSel.value, q: search.value.trim(), status: statusSel.value, label: labelSel.value });
    if (whoSel.value === "waiting") q.set("waiting", "1");
    else if (whoSel.value.startsWith("team:")) q.set("team", whoSel.value.slice(5));
    else if (whoSel.value) q.set("assignee", whoSel.value);
    const data = await api("GET", `/api/inbox?${q}`);
    if (!channelsLoaded) {
      chSel.append(...data.channels.map((c) => h("option", { value: c.id }, c.name)));
      channelsLoaded = true;
    }
    put(listBox, data.conversations.length ? data.conversations.map((c) => h("button", {
      class: "conv" + (c.id === inboxSel ? " active" : ""),
      onclick: () => { inboxSel = c.id; thread = null; run(openConv).then(() => run(loadList)); },
    },
    h("div", { class: "row spread" }, h("b", {}, c.customer_name || `Khách ${c.external_id}`), h("span", { class: "muted" }, fmtTime(c.last_ts))),
    h("div", { class: "row" }, chBadge(c.channel_info), foreign(c) ? h("span", { class: "pill neutral", title: c.lang_name }, c.lang.toUpperCase()) : null,
      c.mode === "human" ? h("span", { class: "pill warn" }, "người") : null,
      c.status === "closed" ? h("span", { class: "pill neutral" }, "đã đóng") : null,
      h("span", { class: "muted" }, c.assignee ? `👤 ${userName(c.assignee)}` : c.team ? `👥 ${teamName(c.team)}` : c.employee_name),
      c.unread ? h("span", { class: "badge" }, c.unread) : null),
    c.labels.length || c.waiting_since ? h("div", { class: "row" }, c.labels.map(labelChip),
      c.waiting_since && c.status === "open" ? h("span", { class: "muted", title: "Khách chờ trả lời" }, `⏱ ${waitingFor(c.waiting_since)}`) : null) : null,
    h("div", { class: "preview" }, (c.last_sender && c.last_sender !== "customer" ? `${SENDER[c.last_sender]}: ` : "") + c.last_preview),
    )) : [h("p", { class: "muted" }, "Chưa có hội thoại nào.")]);
  };

  const renderMessages = (d) => {
    // Re-render only when something changed: keeps the scroll position and does not
    // download attachments again on every refresh.
    const last = d.messages[d.messages.length - 1];
    const sig = `${d.messages.length}:${last ? last.id : 0}:${d.messages.filter((m) => m.translation).length}:${d.conversation.lang}:${d.conversation.customer_name}`;
    if (thread.sig === sig) return;
    thread.sig = sig;
    const nearBottom = thread.msgs.scrollHeight - thread.msgs.scrollTop - thread.msgs.clientHeight < 80;
    put(thread.msgs, d.messages.length ? d.messages.map((m) => h("div", { class: `msg ${m.sender}` },
      h("div", { class: "who" }, m.sender === "customer" ? (m.author || d.conversation.customer_name || "Khách")
        : `${SENDER[m.sender]}${m.author ? " · " + m.author : ""}`, " · ", fmtTime(m.ts)),
      h("div", { class: "bubble" }, m.text || null,
        (m.attachments || []).length ? h("div", { class: "atts" }, m.attachments.map((a, i) => attachmentView(d.conversation.id, m, a, i))) : null),
      translationView(d.conversation, m),
    )) : [h("p", { class: "muted" }, "Chưa có tin nhắn.")]);
    const stick = nearBottom || thread.fresh;
    if (stick) {
      thread.msgs.scrollTop = thread.msgs.scrollHeight;
      // images take their height only once loaded: keep the newest message in view
      for (const img of thread.msgs.querySelectorAll("img")) {
        img.addEventListener("load", () => { thread.msgs.scrollTop = thread.msgs.scrollHeight; }, { once: true });
      }
    }
    thread.fresh = false;
  };

  // Under a customer's message: its translation for staff (or a button to get it).
  // Under a staff reply sent translated: what the staff member actually wrote.
  const translationView = (c, m) => {
    if (m.sender === "human" && m.translation) return h("div", { class: "translation" }, "Bản gốc: ", m.translation);
    if (m.sender !== "customer" || !m.text) return null;
    if (m.translation) return h("div", { class: "translation" }, "Dịch: ", m.translation);
    if (!foreign(c)) return null;
    const box = h("div", { class: "translation" });
    const btn = h("button", { class: "small" }, "Dịch");
    btn.addEventListener("click", () => run(async () => {
      btn.disabled = true;
      btn.textContent = "Đang dịch…";
      try {
        const r = await api("POST", `/api/inbox/${c.id}/messages/${m.id}/translate`, {});
        put(box, "Dịch: ", r.translation);
      } finally { btn.disabled = false; btn.textContent = "Dịch"; }
    }));
    put(box, btn);
    return box;
  };

  const languagePicker = (c) => {
    const current = c.lang_source === "staff" ? (c.country ? `C:${c.country}` : `L:${c.lang}`) : "";
    const auto = c.lang && c.lang_source !== "staff" ? `Tự nhận biết: ${c.lang_name}` : "Tự nhận biết";
    const sel = h("select", {},
      h("option", { value: "", selected: !current }, auto),
      h("optgroup", { label: "Khách ở nước" }, langs.countries.map((x) => {
        const l = langs.languages.find((y) => y.code === x.lang);
        return h("option", { value: `C:${x.code}`, selected: current === `C:${x.code}` }, `${x.name} · ${l ? l.native : x.lang}`);
      })),
      h("optgroup", { label: "Ngôn ngữ" }, langs.languages.map((x) =>
        h("option", { value: `L:${x.code}`, selected: current === `L:${x.code}` }, `${x.name} · ${x.native}`))));
    sel.addEventListener("change", () => run(async () => {
      const [kind, code] = sel.value.split(":");
      const body = kind === "C" ? { country: code } : kind === "L" ? { lang: code } : {};
      update(await api("POST", `/api/inbox/${c.id}/language`, body));
      loadList();
    }, "Đã đổi ngôn ngữ trả lời khách"));
    return h("label", { class: "inline", title: "AI và thông báo sẽ dùng ngôn ngữ này với khách" }, "Ngôn ngữ", sel);
  };

  const renderHead = (d) => {
    const c = d.conversation;
    const base = `/api/inbox/${c.id}`;
    // the 5-second refresh must not close an open menu or reset a select being used
    const sig = JSON.stringify([c.id, c.mode, c.status, c.assignee, c.team, c.labels, c.employee, c.lang, c.lang_source, c.country, c.customer_name]);
    if (thread.headSig === sig) return;
    thread.headSig = sig;
    const post = (path, body, msg) => run(async () => { update(await api("POST", `${base}/${path}`, body)); loadList(); }, msg);
    const person = h("select", { title: "Nhân viên phụ trách hội thoại" }, h("option", { value: "" }, "— chưa giao —"),
      deskMeta.users.map((u) => h("option", { value: u.username, selected: u.username === c.assignee }, u.name)));
    const team = h("select", { title: "Nhóm phụ trách" }, h("option", { value: "" }, "— không nhóm —"),
      deskMeta.teams.map((t) => h("option", { value: t.id, selected: t.id === c.team }, t.name)));
    const assignStaff = () => post("assignee", { assignee: person.value, team: team.value }, "Đã giao hội thoại");
    person.addEventListener("change", assignStaff);
    team.addEventListener("change", assignStaff);
    const labelMenu = h("details", { class: "label-menu" }, h("summary", {}, "Nhãn ▾"),
      h("div", { class: "menu" }, deskMeta.labels.length ? deskMeta.labels.map((l) => {
        const box = h("input", { type: "checkbox", checked: c.labels.includes(l.name) });
        box.addEventListener("change", () => {
          const next = box.checked ? [...c.labels, l.name] : c.labels.filter((x) => x !== l.name);
          post("labels", { labels: next });
        });
        return h("label", { class: "check" }, box, labelChip(l.name));
      }) : h("p", { class: "muted" }, me && me.role === "admin" ? "Chưa có nhãn: thêm trong Cài đặt hộp thư." : "Chưa có nhãn.")));
    const setMode = (mode) => run(async () => { update(await api("POST", `${base}/mode`, { mode })); loadList(); },
      mode === "ai" ? "Đã giao lại cho AI" : "Bạn đang trả lời; AI tạm dừng ở hội thoại này");
    const assign = h("select", { disabled: c.channel.startsWith("simplex:"),
      onchange: () => run(async () => { update(await api("POST", `${base}/assign`, { employee: assign.value })); loadList(); }, "Đã đổi nhân viên phụ trách") },
    d.employees.map((e) => h("option", { value: e.id, selected: e.id === c.employee }, e.name)));
    put(thread.head,
      h("div", { class: "row spread" },
        h("div", {}, h("h2", {}, c.customer_name || `Khách ${c.external_id}`),
          h("div", { class: "row" }, chBadge(c.channel_info), h("span", { class: "muted" }, c.channel_info.name), modePill(c.mode),
            c.status === "closed" ? h("span", { class: "pill neutral" }, "đã đóng") : null, c.labels.map(labelChip))),
        h("div", { class: "row" },
          languagePicker(c),
          h("label", { class: "inline", title: "Nhân viên AI trả lời hội thoại này" }, "AI", assign),
          c.mode === "ai"
            ? h("button", { onclick: () => setMode("human") }, "Tiếp quản (dừng AI)")
            : h("button", { class: "primary", onclick: () => setMode("ai") }, "Giao lại cho AI")),
      ),
      h("div", { class: "row" },
        h("label", { class: "inline" }, "Giao cho", person), team, labelMenu,
        h("button", { onclick: () => summarize(c.id) }, "Tóm tắt (AI)"),
        c.status === "open"
          ? h("button", { onclick: () => post("status", { status: "closed" }, "Đã đóng hội thoại") }, "✓ Đóng hội thoại")
          : h("button", { onclick: () => post("status", { status: "open" }, "Đã mở lại") }, "Mở lại")),
      thread.brief,
    );
  };

  // What the AI remembers about this customer; staff can correct it. Built when the
  // conversation opens or after a save, never by the 5-second refresh (it would wipe edits).
  const renderMemory = (d) => {
    const c = d.conversation;
    const noteCount = Object.keys(d.notes || {}).length;
    const summary = h("textarea", { rows: 4, placeholder: "AI chưa tóm tắt gì (tóm tắt được tạo khi hội thoại dài ra)" }, d.summary || "");
    const key = h("input", { placeholder: "vd. số điện thoại" });
    const value = h("input", { placeholder: "giá trị" });
    const save = (body, msg) => run(async () => {
      const fresh = await api("POST", `/api/inbox/${c.id}/memory`, body);
      renderMemory(fresh);
      thread.mem.open = true;
    }, msg);
    put(thread.mem,
      h("summary", { class: "muted" }, `Trí nhớ AI về khách${d.summary ? " · có tóm tắt" : ""}${noteCount ? ` · ${noteCount} ghi chú` : ""}`),
      h("div", { class: "memory" },
        h("label", {}, "Tóm tắt các cuộc trò chuyện trước", summary),
        h("div", { class: "row" }, h("button", { onclick: () => save({ summary: summary.value }, "Đã lưu tóm tắt") }, "Lưu tóm tắt")),
        h("h3", {}, "Ghi chú"),
        noteCount ? h("ul", {}, Object.entries(d.notes).map(([k, v]) => h("li", {}, h("b", {}, k + ": "), v, " ",
          h("button", { class: "danger small", title: "Xoá ghi chú", onclick: () => save({ notes: { [k]: null } }, "Đã xoá") }, "✕")))) : h("p", { class: "muted" }, "Chưa có ghi chú."),
        h("div", { class: "row" }, key, value,
          h("button", { onclick: () => key.value.trim() && value.value.trim() && save({ notes: { [key.value.trim()]: value.value.trim() } }, "Đã thêm ghi chú") }, "Thêm")),
      ));
  };

  const summarize = (cid) => run(async () => {
    put(thread.brief, h("p", { class: "muted" }, "AI đang đọc hội thoại…"));
    thread.brief.hidden = false;
    try {
      const r = await api("POST", `/api/inbox/${cid}/summary`, {});
      const keep = h("button", { class: "small", onclick: () => run(async () => {
        update(await api("POST", `/api/inbox/${cid}/note`, { text: "Tóm tắt (AI):\n" + r.text }));
        thread.brief.hidden = true;
      }, "Đã lưu thành ghi chú nội bộ") }, "Lưu thành ghi chú");
      put(thread.brief, h("div", { class: "row spread" }, h("b", {}, "Tóm tắt hội thoại (AI)"),
        h("div", { class: "row" }, keep, h("button", { class: "small", onclick: () => { thread.brief.hidden = true; } }, "Đóng"))),
      h("div", { class: "pre" }, r.text));
    } catch (e) {
      thread.brief.hidden = true;
      throw e;
    }
  });

  // The customer behind this conversation: details shared by all their channels.
  const renderContact = async (cid) => {
    const r = await api("GET", `/api/inbox/${cid}/contact`);
    if (!thread || thread.id !== cid) return;
    put(thread.contact, ...contactPanel(r.contact, r.companies, {
      save: (body) => run(async () => { await api("POST", `/api/inbox/${cid}/contact`, body); await renderContact(cid); thread.contact.open = true; }, "Đã lưu thông tin khách"),
      merge: (other) => run(async () => { await api("POST", `/api/inbox/${cid}/contact/merge`, { other }); await renderContact(cid); thread.contact.open = true; }, "Đã gộp khách"),
      split: () => run(async () => { await api("POST", `/api/inbox/${cid}/contact/merge`, { split: true }); await renderContact(cid); thread.contact.open = true; }, "Đã tách hội thoại thành khách riêng"),
      open: (id) => { inboxSel = id; thread = null; run(openConv).then(() => run(loadList)); },
      current: cid,
    }));
  };

  const update = (d) => {
    if (!thread || thread.id !== d.conversation.id) return;
    if (thread.translateWrap) {
      thread.translateWrap.hidden = !foreign(d.conversation);
      const target = d.conversation.lang_name.charAt(0).toLowerCase() + d.conversation.lang_name.slice(1);
      thread.translateLabel.textContent = `Viết tiếng Việt, tự dịch sang ${target} khi gửi`;
    }
    renderHead(d);
    renderMessages(d);
  };

  const openConv = async () => {
    if (inboxSel === null) return;
    const d = await api("GET", `/api/inbox/${inboxSel}`);
    if (!thread || thread.id !== d.conversation.id) {
      const cid = d.conversation.id;
      const text = h("textarea", { rows: 3, class: "composer-text", placeholder: "Nhập trả lời… (Enter để gửi, Shift+Enter xuống dòng)" });
      const takeOver = h("input", { type: "checkbox", checked: true });
      const translate = h("input", { type: "checkbox", checked: true });
      const translateLabel = h("span", {});
      const translateWrap = h("label", { class: "check inline", hidden: true }, translate, translateLabel);
      const translating = () => !translateWrap.hidden && translate.checked;
      const sendBtn = h("button", { class: "primary" }, "Gửi");
      const suggestBtn = h("button", {}, "Gợi ý trả lời (AI)");
      const noteMode = h("input", { type: "checkbox" });
      const composer = h("div", { class: "composer" });
      noteMode.addEventListener("change", () => {
        composer.classList.toggle("noting", noteMode.checked);
        sendBtn.textContent = noteMode.checked ? "Lưu ghi chú" : "Gửi";
        text.placeholder = noteMode.checked ? "Ghi chú cho đồng nghiệp — khách và AI không thấy" : "Nhập trả lời… (Enter để gửi, Shift+Enter xuống dòng)";
      });
      const canned = h("select", { title: "Câu trả lời mẫu" }, h("option", { value: "" }, "Câu trả lời mẫu…"),
        deskMeta.canned.map((x) => h("option", { value: x.id }, x.title)));
      canned.addEventListener("change", () => {
        const x = deskMeta.canned.find((y) => y.id === canned.value);
        canned.value = "";
        if (!x) return;
        const name = (thread && thread.name) || "anh/chị";
        const filled = x.text.replaceAll("{name}", name);
        text.value = text.value.trim() ? `${text.value.trimEnd()}\n${filled}` : filled;
        text.focus();
      });
      const send = () => {
        const body = { text: text.value.trim(), take_over: takeOver.checked, translate: translating() };
        if (!body.text) return;
        const note = noteMode.checked;
        run(async () => {
          sendBtn.disabled = true;
          try {
            update(await api("POST", `/api/inbox/${cid}/${note ? "note" : "reply"}`, note ? { text: body.text } : body));
            text.value = "";
            if (note) { noteMode.checked = false; noteMode.dispatchEvent(new Event("change")); }
            loadList();
          } finally { sendBtn.disabled = false; }
        }, note ? "Đã lưu ghi chú nội bộ" : "Đã gửi");
      };
      sendBtn.addEventListener("click", send);
      text.addEventListener("keydown", (ev) => { if (ev.key === "Enter" && !ev.shiftKey && !ev.isComposing) { ev.preventDefault(); send(); } });
      suggestBtn.addEventListener("click", () => run(async () => {
        suggestBtn.disabled = true;
        suggestBtn.textContent = "AI đang soạn…";
        try {
          const r = await api("POST", `/api/inbox/${cid}/suggest`, { staff_language: translating() });
          text.value = r.text;
          text.focus();
        } finally {
          suggestBtn.disabled = false;
          suggestBtn.textContent = "Gợi ý trả lời (AI)";
        }
      }, "AI đã soạn bản nháp; sửa rồi bấm Gửi"));
      thread = { id: cid, head: h("div", { class: "thread-head" }), mem: h("details", { class: "notes" }), msgs: h("div", { class: "msgs" }),
        brief: h("div", { class: "brief", hidden: true }), contact: h("details", { class: "notes" }), fresh: true, translateWrap, translateLabel, name: d.conversation.customer_name };
      renderMemory(d);
      run(() => renderContact(cid));
      put(composer, text,
        h("div", { class: "row spread" },
          h("div", { class: "row" }, h("span", { class: "muted" }, `Trả lời với tên: ${me ? me.name : ""}`),
            h("label", { class: "check inline" }, takeOver, h("span", {}, "Tiếp quản (AI dừng trả lời)")), translateWrap,
            h("label", { class: "check inline", title: "Chỉ nhân viên thấy" }, noteMode, h("span", {}, "Ghi chú nội bộ"))),
          h("div", { class: "row" }, canned, suggestBtn, sendBtn)));
      put(pane, thread.head, thread.contact, thread.mem, thread.msgs, composer);
      if (d.conversation.unread) api("POST", `/api/inbox/${cid}/read`, {}).then(refreshBadge).catch(() => {});
    }
    update(d);
  };

  let searchTimer;
  search.addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => run(loadList), 300); });
  for (const sel of [chSel, modeSel, statusSel, whoSel, labelSel]) sel.addEventListener("change", () => run(loadList));
  render(
    h("div", { class: "row spread" }, h("h1", {}, "Hộp thư chung"), h("span", { class: "muted" }, "Mọi kênh chat trong một màn hình · tự làm mới mỗi 5 giây")),
    h("div", { class: "inbox" },
      h("div", { class: "inbox-left" }, h("div", { class: "filters" }, search, h("div", { class: "two" }, statusSel, whoSel),
        h("div", { class: "two" }, chSel, modeSel), deskMeta.labels.length ? labelSel : null), listBox),
      pane),
  );
  await run(loadList);
  await run(openConv);
  refreshTimer = setInterval(() => {
    if (current !== "inbox") return;
    run(loadList);
    if (thread) run(openConv);
  }, 5000);
};

// --------------------------------------------------------------------------
// Customers (CRM): one contact across channels

// Details of a customer, their channels, and likely duplicates (admins can merge).
// Used in the inbox (one conversation's customer) and on the customers page.
function contactPanel(c, companies, act) {
  const f = {
    name: h("input", { value: c.name, maxlength: 120 }),
    phone: h("input", { value: c.phone, maxlength: 40, placeholder: "vd. 0901 234 567" }),
    email: h("input", { value: c.email, maxlength: 200, type: "email" }),
    company_id: h("select", {}, h("option", { value: "" }, "— không —"),
      companies.map((x) => h("option", { value: x.id, selected: x.id === c.company_id }, x.name))),
    notes: h("textarea", { rows: 2, placeholder: "Ghi chú về khách (chỉ nhân viên thấy)" }, c.notes),
  };
  const channels = c.conversations || [];
  const dupes = (c.duplicates || []).flatMap((g) => g.contacts.filter((x) => x.id !== c.id).map((x) => ({ ...x, reason: g.reason })));
  const summary = [c.name || "Khách chưa rõ tên", c.phone, c.company].filter(Boolean).join(" · ");
  return [
    h("summary", { class: "muted" }, `Khách hàng: ${summary}${channels.length > 1 ? ` · ${channels.length} kênh` : ""}${dupes.length ? " · có thể trùng" : ""}`),
    h("div", { class: "memory" },
      h("div", { class: "two" }, h("label", {}, "Tên", f.name), h("label", {}, "Công ty", f.company_id),
        h("label", {}, "Số điện thoại", f.phone), h("label", {}, "Email", f.email)),
      h("label", {}, "Ghi chú", f.notes),
      h("div", { class: "row" }, h("button", { onclick: () => act.save({ name: f.name.value, phone: f.phone.value, email: f.email.value,
        company_id: f.company_id.value ? parseInt(f.company_id.value, 10) : null, notes: f.notes.value }) }, "Lưu thông tin khách")),
      h("h3", {}, "Các kênh của khách"),
      h("div", { class: "row" }, channels.map((x) => h("button", {
        class: "small" + (x.id === act.current ? " active" : ""), title: x.last_preview,
        onclick: () => x.id !== act.current && act.open(x.id),
      }, chBadge(x.channel_info), " ", x.customer_name || x.external_id, x.last_ts ? ` · ${fmtTime(x.last_ts)}` : ""))),
      act.split && channels.length > 1 && me && me.role === "admin"
        ? h("button", { class: "small ghost", onclick: () => confirm("Tách hội thoại này thành một khách riêng?") && act.split() }, "Tách hội thoại này ra") : null,
      dupes.length ? h("div", {}, h("h3", {}, "Có thể là cùng một người"),
        dupes.map((x) => h("div", { class: "row" },
          h("span", {}, h("b", {}, x.name || `Khách #${x.id}`), ` — cùng ${x.reason === "phone" ? "số điện thoại" : "email"} ${x.reason === "phone" ? x.phone : x.email}`),
          h("button", { class: "small", onclick: () => confirm(`Gộp "${x.name || x.id}" vào khách này? Các hội thoại của họ sẽ về một chỗ, AI nhớ chung.`) && act.merge(x.id) }, "Gộp vào đây")))) : null,
    ),
  ];
}

views.customers = async (arg) => {
  const search = h("input", { type: "search", placeholder: "Tìm tên, số điện thoại, email" });
  const companySel = h("select", {}, h("option", { value: "" }, "Mọi công ty"));
  const listBox = h("div", { class: "card table-wrap" });
  const detail = h("details", { class: "card notes section", open: true }, h("summary", { class: "muted" }, "Chọn một khách để xem"));
  const dupBox = h("div", { class: "card section" });
  const coBox = h("div", { class: "card section" });
  let companies = [];
  let selected = arg || null;

  const loadDetail = async () => {
    if (!selected) return;
    const r = await api("GET", `/api/crm/contacts/${selected}`);
    put(detail, ...contactPanel(r.contact, companies, {
      save: (body) => run(async () => { await api("PATCH", `/api/crm/contacts/${selected}`, body); await loadAll(); }, "Đã lưu"),
      merge: (other) => run(async () => { await api("POST", `/api/crm/contacts/${selected}/merge`, { other }); await loadAll(); }, "Đã gộp khách"),
      open: (id) => go("inbox", id),
    }));
    detail.open = true;
  };
  const loadList = async () => {
    const q = new URLSearchParams({ q: search.value.trim(), company: companySel.value });
    const r = await api("GET", `/api/crm/contacts?${q}`);
    companies = r.companies;
    if (companySel.options.length === 1) companySel.append(...companies.map((x) => h("option", { value: x.id }, x.name)));
    put(listBox, r.contacts.length ? h("table", {},
      h("thead", {}, h("tr", {}, ["Khách", "Điện thoại", "Email", "Công ty", "Hội thoại", "Gần nhất"].map((t) => h("th", {}, t)))),
      h("tbody", {}, r.contacts.map((x) => h("tr", { class: "clickable" + (x.id === selected ? " active" : ""), onclick: () => { selected = x.id; run(loadAll); } },
        h("td", {}, h("b", {}, x.name || `Khách #${x.id}`)), h("td", {}, x.phone), h("td", {}, x.email), h("td", {}, x.company),
        h("td", {}, x.conversation_count), h("td", {}, fmtTime(x.last_ts)))))) : h("p", { class: "muted" }, "Chưa có khách nào."));
  };
  const loadDupes = async () => {
    const r = await api("GET", "/api/crm/duplicates");
    put(dupBox, h("h2", {}, "Có thể trùng"),
      r.groups.length ? r.groups.map((g) => h("div", { class: "row" },
        h("span", { class: "muted" }, `${g.reason === "phone" ? "Số điện thoại" : "Email"} ${g.value}:`),
        g.contacts.map((x) => h("button", { class: "small", onclick: () => { selected = x.id; run(loadAll); } }, x.name || `#${x.id}`)),
        h("button", { class: "small primary", onclick: () => confirm(`Gộp ${g.contacts.length} khách này thành một?`) && run(async () => {
          for (const x of g.contacts.slice(1)) await api("POST", `/api/crm/contacts/${g.contacts[0].id}/merge`, { other: x.id });
          selected = g.contacts[0].id;
          await loadAll();
        }, "Đã gộp") }, "Gộp tất cả"))) : h("p", { class: "muted" }, "Không thấy khách trùng số điện thoại hoặc email."));
  };
  const loadCompanies = async () => {
    const f = { name: h("input", { placeholder: "Tên công ty" }), domain: h("input", { placeholder: "tên miền email, vd. abc.com.vn" }),
      phone: h("input", { placeholder: "Điện thoại" }), address: h("input", { placeholder: "Địa chỉ" }) };
    const body = () => Object.fromEntries(Object.entries(f).map(([k, el]) => [k, el.value.trim()]));
    put(coBox, h("h2", {}, "Công ty"),
      h("p", { class: "muted" }, "Khách viết từ email có tên miền của công ty được tự gắn vào công ty đó."),
      companies.length ? h("table", {}, h("tbody", {}, companies.map((x) => h("tr", {},
        h("td", {}, h("b", {}, x.name)), h("td", { class: "mono" }, x.domain), h("td", {}, x.phone), h("td", {}, x.address),
        h("td", {}, h("a", { href: "#", onclick: (ev) => { ev.preventDefault(); companySel.value = x.id; run(loadList); } }, `${x.contact_count} khách`)),
        h("td", {}, h("div", { class: "row" },
          h("button", { class: "small", onclick: () => { const n = prompt("Tên công ty:", x.name); if (n && n.trim()) run(async () => { await api("PATCH", `/api/crm/companies/${x.id}`, { name: n.trim() }); await loadAll(); }, "Đã lưu"); } }, "Đổi tên"),
          h("button", { class: "small danger", onclick: () => confirm(`Xoá công ty ${x.name}? Khách vẫn giữ nguyên.`) && run(async () => { await api("DELETE", `/api/crm/companies/${x.id}`); await loadAll(); }, "Đã xoá") }, "Xoá"))))))) : h("p", { class: "muted" }, "Chưa có công ty."),
      h("div", { class: "two section" }, f.name, f.domain, f.phone, f.address),
      h("button", { class: "primary", onclick: () => f.name.value.trim() && run(async () => { await api("POST", "/api/crm/companies", body()); await loadAll(); }, "Đã thêm công ty") }, "Thêm công ty"));
  };
  const loadAll = async () => { await loadList(); await Promise.all([loadDetail(), loadDupes(), loadCompanies()]); };

  let t;
  search.addEventListener("input", () => { clearTimeout(t); t = setTimeout(() => run(loadList), 300); });
  companySel.addEventListener("change", () => run(loadList));
  render(h("div", { class: "row spread" }, h("h1", {}, "Khách hàng"), h("span", { class: "muted" }, "Một khách, mọi kênh: gộp hội thoại của cùng một người")),
    h("div", { class: "filters row" }, search, companySel), listBox, detail, dupBox, coBox);
  await run(loadAll);
};

// --------------------------------------------------------------------------
// Inbox settings (admins): labels, saved replies, teams, triage rules, SLA target

views.desk = async () => {
  const [meta, acc] = await Promise.all([run(() => api("GET", "/api/inbox/meta")), run(() => api("GET", "/api/users"))]);
  if (!meta || !acc) return;
  const save = (section, value, msg) => run(async () => { await api("PUT", `/api/desk/${section}`, { value }); go("desk"); }, msg);
  const del = (section, i) => confirm("Xoá mục này?") && save(section, meta[section].filter((_, j) => j !== i), "Đã xoá");
  const chName = Object.fromEntries(acc.channels.map((c) => [c.id, c.name]));
  const uName = Object.fromEntries(acc.users.map((u) => [u.username, u.name]));

  // labels
  const lName = h("input", { placeholder: "vd. VIP, Khiếu nại, Chờ thanh toán", maxlength: 40 });
  const lColor = h("input", { type: "color", value: "#2563eb" });
  const labels = h("div", { class: "card section" }, h("h2", {}, "Nhãn"),
    h("p", { class: "muted" }, "Gắn lên hội thoại để lọc và báo cáo. Đổi tên thì các hội thoại đi theo."),
    h("div", { class: "row" }, meta.labels.length ? meta.labels.map((l, i) => h("span", { class: "row" }, labelChipOf(l),
      h("button", { class: "small", title: "Đổi tên", onclick: () => { const n = prompt("Tên mới:", l.name); if (n && n.trim() && n.trim() !== l.name) save("labels", meta.labels.map((x, j) => (j === i ? { ...x, name: n.trim(), was: l.name } : x)), "Đã đổi tên"); } }, "✎"),
      h("button", { class: "small danger", onclick: () => del("labels", i) }, "✕"))) : h("span", { class: "muted" }, "Chưa có nhãn.")),
    h("div", { class: "row section" }, lName, lColor,
      h("button", { class: "primary", onclick: () => lName.value.trim() && save("labels", [...meta.labels, { name: lName.value.trim(), color: lColor.value }], "Đã thêm nhãn") }, "Thêm nhãn")));

  // saved replies
  const cTitle = h("input", { placeholder: "Tiêu đề, vd. Chào khách mới", maxlength: 80 });
  const cText = h("textarea", { rows: 3, placeholder: "Nội dung. {name} được thay bằng tên khách." });
  const canned = h("div", { class: "card section" }, h("h2", {}, "Câu trả lời mẫu"),
    meta.canned.length ? h("table", {}, h("tbody", {}, meta.canned.map((x, i) => h("tr", {},
      h("td", {}, h("b", {}, x.title)), h("td", { class: "pre" }, x.text),
      h("td", {}, h("button", { class: "small danger", onclick: () => del("canned", i) }, "Xoá")))))) : h("p", { class: "muted" }, "Chưa có câu mẫu."),
    h("div", { class: "section" }, h("label", {}, "Tiêu đề", cTitle), h("label", {}, "Nội dung", cText),
      h("button", { class: "primary", onclick: () => cTitle.value.trim() && cText.value.trim() && save("canned", [...meta.canned, { title: cTitle.value.trim(), text: cText.value.trim() }], "Đã thêm câu mẫu") }, "Thêm câu mẫu")));

  // teams
  const tId = h("input", { placeholder: "mã, vd. cskh", maxlength: 48 });
  const tName = h("input", { placeholder: "Tên nhóm, vd. Chăm sóc khách hàng" });
  const tBoxes = acc.users.map((u) => { const box = h("input", { type: "checkbox", value: u.username }); return { box, el: h("label", { class: "check" }, box, h("span", {}, u.name)) }; });
  const teams = h("div", { class: "card section" }, h("h2", {}, "Nhóm"),
    h("p", { class: "muted" }, "Hội thoại giao cho nhóm hiện trong mục \"Của tôi\" của mọi thành viên, đến khi giao cho một người."),
    meta.teams.length ? h("table", {}, h("tbody", {}, meta.teams.map((t, i) => h("tr", {},
      h("td", {}, h("b", {}, t.name), " ", h("span", { class: "mono muted" }, t.id)),
      h("td", {}, t.members.map((m) => uName[m] || m).join(", ") || "—"),
      h("td", {}, h("button", { class: "small danger", onclick: () => del("teams", i) }, "Xoá")))))) : h("p", { class: "muted" }, "Chưa có nhóm."),
    h("div", { class: "two section" }, h("label", {}, "Mã nhóm", tId), h("label", {}, "Tên nhóm", tName)),
    h("div", { class: "checks" }, tBoxes.map((b) => b.el)),
    h("button", { class: "primary", onclick: () => save("teams", [...meta.teams, { id: tId.value.trim(), name: tName.value.trim(), members: tBoxes.filter((b) => b.box.checked).map((b) => b.box.value) }], "Đã thêm nhóm") }, "Thêm nhóm"));

  // triage rules
  const r = {
    name: h("input", { placeholder: "vd. Khiếu nại → CSKH" }),
    keywords: h("input", { placeholder: "khiếu nại, hoàn tiền, lỗi (để trống = mọi tin)" }),
    team: h("select", {}, h("option", { value: "" }, "—"), meta.teams.map((t) => h("option", { value: t.id }, t.name))),
    assignee: h("select", {}, h("option", { value: "" }, "—"), acc.users.map((u) => h("option", { value: u.username }, u.name))),
    handoff: h("input", { type: "checkbox" }),
  };
  const rCh = acc.channels.map((c) => { const box = h("input", { type: "checkbox", value: c.id }); return { box, el: h("label", { class: "check" }, box, h("span", {}, c.name)) }; });
  const rLb = meta.labels.map((l) => { const box = h("input", { type: "checkbox", value: l.name }); return { box, el: h("label", { class: "check" }, box, labelChipOf(l)) }; });
  const picked = (xs) => xs.filter((b) => b.box.checked).map((b) => b.box.value);
  const describeRule = (x) => [
    x.channels.length ? `kênh ${x.channels.map((c) => chName[c] || c).join(", ")}` : "mọi kênh",
    x.keywords.length ? `có "${x.keywords.join('", "')}"` : "mọi tin",
  ].join(", ") + " → " + [
    x.labels.length ? `gắn ${x.labels.join(", ")}` : null, x.team ? `nhóm ${teamName(x.team)}` : null,
    x.assignee ? `giao ${uName[x.assignee] || x.assignee}` : null, x.handoff ? "chuyển người trả lời (AI dừng)" : null,
  ].filter(Boolean).join(", ");
  deskMeta = { ...deskMeta, ...meta };
  const rules = h("div", { class: "card section" }, h("h2", {}, "Quy tắc tự phân loại"),
    h("p", { class: "muted" }, "Chạy với mỗi tin mới của khách (không phân biệt dấu, hoa thường). Chỉ giao người/nhóm khi hội thoại chưa có ai phụ trách."),
    meta.rules.length ? h("table", {}, h("tbody", {}, meta.rules.map((x, i) => h("tr", {},
      h("td", {}, h("b", {}, x.name)), h("td", {}, describeRule(x)),
      h("td", {}, h("div", { class: "row" },
        h("button", { class: "small", onclick: () => save("rules", meta.rules.map((y, j) => (j === i ? { ...y, enabled: !y.enabled } : y)), x.enabled ? "Đã tắt" : "Đã bật") }, x.enabled ? "Tắt" : "Bật"),
        h("button", { class: "small danger", onclick: () => del("rules", i) }, "Xoá"))))))) : h("p", { class: "muted" }, "Chưa có quy tắc."),
    h("div", { class: "two section" }, h("label", {}, "Tên quy tắc", r.name), h("label", {}, "Từ khoá (cách nhau bởi dấu phẩy)", r.keywords)),
    h("h3", {}, "Kênh (không chọn = mọi kênh)"), h("div", { class: "checks" }, rCh.map((b) => b.el)),
    h("h3", {}, "Gắn nhãn"), rLb.length ? h("div", { class: "checks" }, rLb.map((b) => b.el)) : h("p", { class: "muted" }, "Thêm nhãn ở trên trước."),
    h("div", { class: "two section" }, h("label", {}, "Giao cho nhóm", r.team), h("label", {}, "Giao cho người", r.assignee)),
    h("label", { class: "check" }, r.handoff, h("span", {}, "Chuyển cho người trả lời (AI dừng ở hội thoại này)")),
    h("div", { class: "row section" }, h("button", { class: "primary", onclick: () => save("rules", [...meta.rules, {
      name: r.name.value.trim(), keywords: r.keywords.value.split(",").map((k) => k.trim()).filter(Boolean),
      channels: picked(rCh), labels: picked(rLb), team: r.team.value, assignee: r.assignee.value, handoff: r.handoff.checked,
    }], "Đã thêm quy tắc") }, "Thêm quy tắc")));

  const sla = h("input", { type: "number", min: 1, max: 10080, value: meta.sla_minutes, class: "narrow" });
  render(h("h1", {}, "Cài đặt hộp thư"),
    h("div", { class: "card section" }, h("h2", {}, "Mục tiêu thời gian trả lời (SLA)"),
      h("div", { class: "row" }, "Khách chờ quá", sla, "phút là trễ",
        h("button", { onclick: () => save("sla_minutes", parseInt(sla.value, 10), "Đã lưu") }, "Lưu"),
        h("button", { class: "ghost", onclick: () => go("sla") }, "Xem báo cáo SLA →"))),
    labels, canned, teams, rules);
};
const labelChipOf = (l) => colored(h("span", { class: "label" }, l.name), l.color);

views.sla = async () => {
  const hours = h("select", {}, [[24, "24 giờ qua"], [168, "7 ngày qua"], [720, "30 ngày qua"]].map(([v, t]) => h("option", { value: v }, t)));
  const box = h("div", {});
  const load = async () => {
    const [d, meta] = await Promise.all([api("GET", `/api/inbox/sla?hours=${hours.value}`), api("GET", "/api/inbox/meta")]);
    deskMeta = meta;
    const target = d.target_seconds;
    const pct = (x) => (x.answers ? `${Math.round((100 * x.within_target) / x.answers)}%` : "—");
    put(box,
      h("div", { class: "tiles" },
        tile(d.counts.open, "hội thoại đang mở"), tile(d.counts.waiting, "khách đang chờ trả lời"),
        tile(d.counts.late, `chờ quá ${fmtDur(target)}`), tile(d.counts.unassigned, "đang mở, chưa giao ai")),
      h("div", { class: "card section" }, h("h2", {}, "Thời gian trả lời"),
        d.responders.length ? h("table", {}, h("thead", {}, h("tr", {}, ["Ai trả lời", "Số lần", "Trung bình", "Lâu nhất", `Trong ${fmtDur(target)}`].map((t) => h("th", {}, t)))),
          h("tbody", {}, d.responders.map((x) => h("tr", {},
            h("td", {}, x.responder === "ai" ? "AI" : x.author || "Nhân viên"), h("td", {}, x.answers),
            h("td", {}, fmtDur(x.avg_seconds)), h("td", {}, fmtDur(x.max_seconds)), h("td", {}, pct(x)))))) : h("p", { class: "muted" }, "Chưa có câu trả lời nào trong khoảng này.")),
      h("div", { class: "card section" }, h("h2", {}, "Khách chờ lâu nhất"),
        d.oldest_waiting.length ? h("table", {}, h("tbody", {}, d.oldest_waiting.map((c) => h("tr", {},
          h("td", {}, chBadge(c.channel_info), " ", h("a", { href: "#", onclick: (ev) => { ev.preventDefault(); go("inbox", c.id); } }, c.customer_name || `Khách ${c.external_id}`)),
          h("td", {}, c.labels.map(labelChip)),
          h("td", {}, c.assignee ? userName(c.assignee) : c.team ? teamName(c.team) : h("span", { class: "muted" }, "chưa giao")),
          h("td", {}, c.mode === "ai" ? "AI" : "người"),
          h("td", {}, h("span", { class: `pill ${new Date(c.waiting_since) < Date.now() - target * 1000 ? "bad" : "warn"}` }, `chờ ${waitingFor(c.waiting_since)}`)))))) : h("p", { class: "muted" }, "Không khách nào đang chờ.")),
      h("div", { class: "card section" }, h("h2", {}, "Việc của từng người / nhóm"),
        d.workload.length ? h("table", {}, h("thead", {}, h("tr", {}, ["Người", "Nhóm", "Đang mở", "Khách đang chờ"].map((t) => h("th", {}, t)))),
          h("tbody", {}, d.workload.map((w) => h("tr", {}, h("td", {}, w.assignee_name || "—"), h("td", {}, w.team_name || "—"), h("td", {}, w.open), h("td", {}, w.waiting))))) : h("p", { class: "muted" }, "Chưa giao hội thoại nào.")),
    );
  };
  hours.addEventListener("change", () => run(load));
  render(h("div", { class: "row spread" }, h("h1", {}, "Báo cáo SLA"), hours), box);
  await run(load);
  refreshTimer = setInterval(() => current === "sla" && run(load), 30000);
};

// --------------------------------------------------------------------------
// Channels and SimpleX accounts

const ZALO_STATE = { idle: "chưa khởi động", qr_pending: "chờ quét mã", qr_scanned: "đã quét, chờ xác nhận trên điện thoại",
  connected: "đã kết nối", error: "lỗi" };

async function zaloLogin(c, btn, box) {
  btn.disabled = true;
  const step = async () => {
    const r = await api("POST", `/api/channels/${encodeURIComponent(c.id)}/login`, {});
    put(box, h("p", { class: "muted" }, `Trạng thái: ${ZALO_STATE[r.state] || r.state}`),
      r.qr ? h("img", { src: r.qr, alt: "Mã QR đăng nhập Zalo", class: "qr" }) : null,
      r.qr ? h("p", { class: "muted" }, "Mở Zalo trên điện thoại → Quét mã QR. Không chia sẻ mã này.") : null);
    return r.state;
  };
  try {
    for (let i = 0; i < 100 && current === "channels"; i++) {
      if ((await step()) === "connected") { toast("Zalo đã kết nối"); break; }
      await new Promise((ok) => setTimeout(ok, 3000));
    }
  } catch (e) {
    toast("Lỗi: " + e.message);
  } finally {
    btn.disabled = false;
  }
}

views.channels = async () => {
  const [chs, sx] = await Promise.all([run(() => api("GET", "/api/channels")), run(() => api("GET", "/api/simplex"))]);
  if (!chs || !sx) return;
  const poll = (c, btn) => run(async () => {
    btn.disabled = true;
    try {
      const r = await api("POST", `/api/channels/${encodeURIComponent(c.id)}/poll`, {});
      toast(r.state.last_error ? `Lỗi: ${r.state.last_error}` : `Đã lấy ${r.added} tin mới`);
      go("channels");
    } finally { btn.disabled = false; }
  });
  const rows = chs.channels.map((c) => {
    const st = c.stats || {};
    const s = c.state || {};
    let how;
    if (c.type === "simplex") how = "tin đến trực tiếp";
    else if (c.type === "webhook") how = h("span", { class: "mono" }, `POST /hooks/${c.id}`);
    else if (c.type === "telegram") {
      const btn = h("button", {}, "Đăng ký webhook");
      btn.addEventListener("click", () => run(async () => {
        btn.disabled = true;
        try { toast(`Telegram sẽ gửi tin tới ${(await api("POST", `/api/channels/${encodeURIComponent(c.id)}/webhook`, {})).url}`); go("channels"); }
        finally { btn.disabled = false; }
      }));
      how = h("div", {}, h("div", { class: "muted" }, s.webhook ? `tin đẩy về ${s.webhook}` : "chưa đăng ký webhook"), btn);
    }
    else if (c.type === "whatsapp") how = h("div", {}, h("div", { class: "muted" }, "tin đẩy về từ WAHA"), h("span", { class: "mono" }, `POST /hooks/${c.id}`));
    else if (c.type === "email") how = h("div", {}, h("div", { class: "muted" }, "Inbound Parse → trả lời qua SMTP"), h("span", { class: "mono" }, `POST /hooks/${c.id}?key=…`));
    else if (c.type === "zalo_personal") {
      const box = h("div", { class: "zalo-login" });
      const btn = h("button", {}, "Đăng nhập Zalo (QR)");
      btn.addEventListener("click", () => zaloLogin(c, btn, box));
      how = h("div", {}, h("div", { class: "muted" }, "tin đẩy về từ Zalo gateway"), btn, box);
    }
    else {
      const btn = h("button", {}, "Lấy tin ngay");
      btn.addEventListener("click", () => poll(c, btn));
      how = h("div", {}, h("div", { class: "muted" }, `mỗi ${c.poll_seconds}s · lần cuối ${fmtTime(s.last_poll)}`), btn);
    }
    return h("tr", {},
      h("td", {}, chBadge(c), " ", h("b", {}, c.name)),
      h("td", {}, c.employee),
      h("td", {}, c.auto_reply ? h("span", { class: "pill ok" }, "AI tự trả lời") : h("span", { class: "pill neutral" }, "chỉ gom tin")),
      h("td", {}, `${st.conversations || 0} hội thoại · ${st.unread || 0} chưa đọc · ${st.human || 0} người trả lời`),
      h("td", {}, how),
      h("td", {}, s.last_error ? h("span", { class: "pill bad", title: s.last_error }, "lỗi") : h("span", { class: "pill ok" }, "ổn")),
    );
  });
  const accounts = sx.accounts.map((a) => {
    const out = h("div", { class: "invite" });
    const link = h("input", { placeholder: "Dán link SimpleX (địa chỉ hoặc link mời 1 lần)" });
    const invite = () => run(async () => {
      const r = await api("POST", `/api/simplex/${encodeURIComponent(a.id)}/invite`, {});
      put(out, h("p", { class: "muted" }, "Link mời dùng một lần — gửi cho khách hoặc quét mã:"),
        h("img", { src: r.qr, alt: "QR link mời", class: "qr" }), h("p", { class: "mono" }, r.link),
        h("button", { onclick: () => navigator.clipboard.writeText(r.link).then(() => toast("Đã chép link")) }, "Chép link"));
    }, "Đã tạo link mời");
    const connect = () => link.value.trim() && run(async () => {
      await api("POST", `/api/simplex/${encodeURIComponent(a.id)}/connect`, { link: link.value.trim() });
      link.value = "";
    }, "Đã gửi yêu cầu kết nối; hội thoại sẽ hiện trong Hộp thư khi bên kia chấp nhận");
    return h("div", { class: "card" },
      h("div", { class: "row spread" }, h("h2", {}, a.name), h("span", { class: "muted" }, `${a.contacts} khách · ${a.admins} quản trị`)),
      a.address ? h("div", { class: "addr" },
        h("img", { src: a.qr, alt: `QR địa chỉ của ${a.name}`, class: "qr" }),
        h("div", {}, h("p", { class: "muted" }, "Địa chỉ liên hệ cố định (khách quét mã bằng app SimpleX để nhắn):"),
          h("p", { class: "mono" }, a.address),
          h("button", { onclick: () => navigator.clipboard.writeText(a.address).then(() => toast("Đã chép địa chỉ")) }, "Chép địa chỉ")))
        : h("p", { class: "muted" }, "Chưa có địa chỉ (bot đang khởi động?)."),
      h("div", { class: "row section" }, h("button", { onclick: invite }, "Tạo link mời 1 lần"),
        h("button", { onclick: () => go("inbox") }, "Mở hộp thư")),
      out,
      h("div", { class: "row section" }, link, h("button", { onclick: connect }, "Kết nối")),
    );
  });
  render(
    h("h1", {}, "Kênh chat"),
    h("div", { class: "card table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, ["Kênh", "Nhân viên", "Chế độ", "Thống kê", "Nhận tin", "Trạng thái"].map((t) => h("th", {}, t)))),
      h("tbody", {}, rows))),
    h("p", { class: "muted" }, "Thêm kênh Zalo OA, Zalo cá nhân (qua zalo-gateway), Facebook Messenger hoặc webhook trong file cấu hình (channels:). Token chỉ đặt qua biến môi trường."),
    h("h1", { class: "section" }, "SimpleX"),
    h("div", { class: "grid wide" }, accounts),
  );
};

// --------------------------------------------------------------------------
// Run log

views.runlog = async () => {
  const { employees } = await run(() => api("GET", "/api/overview")) || { employees: [] };
  const emp = h("select", {}, h("option", { value: "" }, "Tất cả nhân viên"), employees.map((e) => h("option", { value: e.id }, e.display_name)));
  const kind = h("select", {}, h("option", { value: "" }, "Mọi loại việc"), Object.entries(KIND).map(([k, v]) => h("option", { value: k }, v)));
  const body = h("tbody");
  const load = async () => {
    const q = new URLSearchParams({ employee: emp.value, kind: kind.value, limit: "300" });
    const { records } = await api("GET", `/api/runlog?${q}`);
    put(body, ...records.map((r) => h("tr", {},
      h("td", {}, fmtTime(r.ts)), h("td", {}, r.employee), h("td", {}, KIND[r.kind] || r.kind), h("td", {}, pill(r.status)),
      h("td", {}, [r.routine && `lịch ${r.routine}`, r.action && `${r.action} #${r.request}`, r.contact !== undefined && `khách #${r.contact}`,
        r.asker && `hỏi bởi ${r.asker}`, r.tools && r.tools.length && `skill: ${r.tools.join(", ")}`].filter(Boolean).join(" · ")),
      h("td", { class: "muted" }, [r.model, r.tokens_in !== undefined && `${r.tokens_in}/${r.tokens_out} token`, r.ms !== undefined && `${(r.ms / 1000).toFixed(1)} s`].filter(Boolean).join(" · ")),
    )));
  };
  emp.addEventListener("change", () => run(load));
  kind.addEventListener("change", () => run(load));
  render(
    h("h1", {}, "Nhật ký"),
    h("div", { class: "two" }, h("label", {}, "Nhân viên", emp), h("label", {}, "Loại việc", kind)),
    h("div", { class: "card table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, ["Lúc", "Nhân viên", "Loại", "Kết quả", "Chi tiết", "Model"].map((t) => h("th", {}, t)))), body)),
  );
  await run(load);
  refreshTimer = setInterval(() => current === "runlog" && run(load), 15000);
};

// --------------------------------------------------------------------------

// --------------------------------------------------------------------------
// Accounts (admin) and my account

views.accounts = async () => {
  const data = await run(() => api("GET", "/api/users"));
  if (!data) return;
  const chName = Object.fromEntries(data.channels.map((c) => [c.id, c.name]));
  const reload = (res) => { if (res) go("accounts"); };
  const patch = (u, body, msg) => run(async () => reload(await api("PATCH", `/api/users/${encodeURIComponent(u.username)}`, body)), msg);
  const f = {
    username: h("input", { placeholder: "vd. thu.tran", autocomplete: "off" }),
    name: h("input", { placeholder: "Tên hiện với khách và đồng nghiệp" }),
    role: h("select", {}, Object.entries(ROLE).map(([k, v]) => h("option", { value: k, selected: k === "agent" }, v))),
    password: h("input", { type: "password", autocomplete: "new-password", placeholder: "ít nhất 10 ký tự" }),
  };
  const boxes = data.channels.map((c) => {
    const box = h("input", { type: "checkbox", value: c.id });
    return { box, el: h("label", { class: "check" }, box, h("span", {}, c.name)) };
  });
  const add = () => run(async () => {
    const body = Object.fromEntries(Object.entries(f).map(([k, el]) => [k, el.value.trim()]));
    body.channels = boxes.filter((b) => b.box.checked).map((b) => b.box.value);
    reload(await api("POST", "/api/users", body));
  }, "Đã tạo tài khoản");
  render(
    h("h1", {}, "Tài khoản"),
    h("div", { class: "card table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, ["Tên đăng nhập", "Tên", "Vai trò", "Kênh được xem", "Trạng thái", ""].map((t) => h("th", {}, t)))),
      h("tbody", {}, data.users.map((u) => h("tr", {},
        h("td", { class: "mono" }, u.username), h("td", {}, u.name), h("td", {}, ROLE[u.role] || u.role),
        h("td", {}, u.role === "admin" || !u.channels.length ? "tất cả" : u.channels.map((c) => chName[c] || c).join(", ")),
        h("td", {}, u.owner ? h("span", { class: "pill ok" }, "chủ") : u.disabled ? pill("skipped") : h("span", { class: "pill ok" }, "đang dùng")),
        h("td", {}, u.owner ? h("span", { class: "muted" }, "mật khẩu trong file cấu hình") : h("div", { class: "row" },
          h("button", { onclick: () => patch(u, { disabled: !u.disabled }, u.disabled ? "Đã mở lại" : "Đã khoá") }, u.disabled ? "Mở lại" : "Khoá"),
          h("button", { onclick: () => { const p = prompt(`Mật khẩu mới cho ${u.username} (ít nhất 10 ký tự):`); if (p) patch(u, { password: p }, "Đã đặt mật khẩu mới"); } }, "Đặt mật khẩu"),
          h("button", { class: "danger", onclick: () => confirm(`Xoá tài khoản ${u.username}?`) && run(async () => reload(await api("DELETE", `/api/users/${encodeURIComponent(u.username)}`)), "Đã xoá") }, "Xoá"))),
      ))),
    )),
    h("div", { class: "card section" },
      h("h2", {}, "Thêm tài khoản"),
      h("p", { class: "muted" }, "Nhân viên bán hàng chỉ vào được Hộp thư; chọn kênh để giới hạn (không chọn = mọi kênh). Quản trị làm được mọi việc. Khi đổi vai trò, kênh hoặc mật khẩu, người đó phải đăng nhập lại."),
      h("div", { class: "two" },
        h("label", {}, "Tên đăng nhập", f.username), h("label", {}, "Tên hiển thị", f.name),
        h("label", {}, "Vai trò", f.role), h("label", {}, "Mật khẩu", f.password)),
      h("h3", {}, "Kênh được xem (với nhân viên bán hàng)"),
      h("div", { class: "checks" }, boxes.map((b) => b.el)),
      h("div", { class: "row section" }, h("button", { class: "primary", onclick: add }, "Tạo tài khoản")),
    ),
  );
};

views.me = async () => {
  const old = h("input", { type: "password", autocomplete: "current-password" });
  const pw = h("input", { type: "password", autocomplete: "new-password", placeholder: "ít nhất 10 ký tự" });
  const pw2 = h("input", { type: "password", autocomplete: "new-password" });
  const change = () => {
    if (pw.value !== pw2.value) return toast("Hai lần nhập mật khẩu mới không khớp");
    run(async () => {
      await api("POST", "/api/me/password", { old: old.value, new: pw.value });
      old.value = pw.value = pw2.value = "";
    }, "Đã đổi mật khẩu");
  };
  render(
    h("h1", {}, "Tài khoản của tôi"),
    h("div", { class: "card" },
      h("p", {}, h("b", {}, me.name), h("span", { class: "muted" }, ` · ${me.username} · ${ROLE[me.role] || me.role}`)),
      me.role !== "admin" && me.channels.length ? h("p", { class: "muted" }, `Kênh được xem: ${me.channels.join(", ")}`) : null,
      me.username === "admin" ? h("p", { class: "muted" }, "Mật khẩu tài khoản chủ đặt trong file cấu hình (admin_ui).") : h("div", {},
        h("h2", { class: "section" }, "Đổi mật khẩu"),
        h("div", { class: "two" }, h("label", {}, "Mật khẩu hiện tại", old), h("span"),
          h("label", {}, "Mật khẩu mới", pw), h("label", {}, "Nhập lại mật khẩu mới", pw2)),
        h("button", { class: "primary", onclick: change }, "Đổi mật khẩu")),
    ),
  );
};

// --------------------------------------------------------------------------

async function start() {
  ({ user: me } = await api("GET", "/api/me"));
  $("#login").hidden = true;
  $("#app").hidden = false;
  for (const b of document.querySelectorAll("#nav button[data-admin]")) b.hidden = me.role !== "admin";
  $("#me").textContent = `${me.name} · ${ROLE[me.role] || me.role}`;
  go(me.role === "admin" ? "overview" : "inbox");
}

(async () => {
  try {
    await start();
  } catch (_) {
    showLogin();
  }
})();

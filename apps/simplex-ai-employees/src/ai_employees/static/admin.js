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
const KIND = { reply: "trả lời", consult: "hỏi đồng nghiệp", routine: "lịch làm việc", action: "hành động" };
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
    await api("POST", "/api/login", { password: $("#login-password").value });
    $("#login-password").value = "";
    start();
  } catch (e) {
    $("#login-error").textContent = e.message;
  }
});

$("#logout").addEventListener("click", async () => {
  await run(() => api("POST", "/api/logout", {}));
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
    const { pending } = await api("GET", "/api/approvals");
    $("#badge").hidden = pending.length === 0;
    $("#badge").textContent = pending.length;
  } catch (_) { /* shown elsewhere */ }
}

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

async function start() {
  $("#login").hidden = true;
  $("#app").hidden = false;
  go("overview");
}

(async () => {
  try {
    await api("GET", "/api/overview");
    start();
  } catch (_) {
    showLogin();
  }
})();

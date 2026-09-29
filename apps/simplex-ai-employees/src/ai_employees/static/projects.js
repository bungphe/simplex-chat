"use strict";
// Công việc: projects and their tasks as a mind map, a list or a Kanban board; the panel on
// the right edits a task and saves as you type. Uses the helpers of admin.js (h, api, run,
// toast, put...); notes are rich text, rebuilt element by element (never innerHTML).

(() => {
  const STATUS_OPT = [["todo", tr("Chưa bắt đầu")], ["doing", tr("Đang làm")], ["done", tr("Hoàn thành")], ["paused", tr("Tạm dừng")]];
  const PRIO_OPT = [["low", tr("Thấp")], ["medium", tr("Vừa")], ["high", tr("Cao")], ["urgent", tr("Gấp")]];
  const PRIO_TEXT = Object.fromEntries(PRIO_OPT);
  const STATE_TEXT = { ...Object.fromEntries(STATUS_OPT), late: tr("Trễ hạn"), soon: tr("Sắp đến hạn") };
  const LEGEND = ["todo", "doing", "done", "paused", "late", "soon"];
  const EMOJI = ["⭐", "🔥", "🎯", "🚀", "💡", "📌", "📣", "📈", "💰", "🧾", "🛒", "📦",
    "🚚", "🎨", "📷", "🎬", "✍️", "📚", "🤝", "👥", "🛠️", "⚙️", "✅", "❗"];
  const SWATCHES = ["#fde2e4", "#fae1dd", "#ffe8cc", "#fff3bf", "#e9f5db", "#d8f3dc",
    "#d0f4f7", "#dbeafe", "#e0e7ff", "#ede9fe", "#fce7f3", "#eef1f5"];
  const BRANCH = ["#6366f1", "#0ea5e9", "#10b981", "#f59e0b", "#ef4444", "#ec4899", "#8b5cf6", "#14b8a6", "#84cc16", "#f97316"];
  const LOG_TEXT = {
    project_created: tr("tạo dự án"), project_updated: tr("sửa dự án"), created: tr("thêm việc"), updated: tr("sửa việc"),
    moved: tr("chuyển nhánh"), deleted: tr("xoá việc"), commented: tr("bình luận"), file_added: tr("thêm tệp"),
    file_deleted: tr("xoá tệp"), imported: tr("nhập dự án"),
  };
  const NOTE_TAGS = new Set(["P", "BR", "B", "STRONG", "I", "EM", "U", "S", "STRIKE", "UL", "OL", "LI", "BLOCKQUOTE",
    "H1", "H2", "H3", "A", "CODE", "PRE", "DIV", "SPAN"]);
  const DROP_TAGS = new Set(["SCRIPT", "STYLE", "TEMPLATE", "IFRAME", "OBJECT", "EMBED", "NOSCRIPT", "TITLE", "HEAD"]);
  const SVG_NS = "http://www.w3.org/2000/svg";
  const MAX_AVATAR = 200000;

  // this browser's own preferences (a private window may refuse them)
  const ls = {
    get(k, d) { try { const v = localStorage.getItem("pm." + k); return v === null ? d : JSON.parse(v); } catch (_) { return d; } },
    set(k, v) { try { localStorage.setItem("pm." + k, JSON.stringify(v)); } catch (_) { /* not kept */ } },
  };

  const S = {
    pid: ls.get("project", null), view: ls.get("view", "map"),
    projects: [], people: [], manager: false, project: null, tasks: [], byId: new Map(), tops: [],
    f: { q: "", who: "", prio: "", label: "", hideDone: false, week: false, late: false },
    sel: null, collapsed: new Set(), zoom: 1, panX: 0, panY: 0, fitted: false, bounds: null, layout: new Map(),
    kanbanAll: false, archived: false, logOpen: false, els: null,
  };
  if (!["map", "list", "kanban", "projects"].includes(S.view)) S.view = "map";
  let P = null; // the open detail panel

  // ------------------------------------------------------------------ helpers

  const dm = (d) => (d ? `${d.slice(8, 10)}/${d.slice(5, 7)}` : "");
  const isoDay = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  const norm = (s) => String(s || "").toLowerCase().normalize("NFD").replace(/[̀-ͯ]/g, "").replace(/đ/g, "d");
  const personName = (u) => (S.people.find((p) => p.username === u) || {}).name || u || "";
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const bar = (pct) => { const i = h("i"); i.style.width = `${clamp(pct || 0, 0, 100)}%`; return h("div", { class: "pm-bar" }, i); };
  const svg = (tag, attrs = {}) => { const el = document.createElementNS(SVG_NS, tag); for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v); return el; };
  function selectOf(options, value, onchange, attrs = {}) {
    const s = h("select", attrs, options.map(([v, l]) => h("option", { value: v }, l)));
    s.value = value ?? "";
    if (onchange) s.addEventListener("change", () => onchange(s.value));
    return s;
  }

  function stateOf(t) {
    if (t.status === "done" || (t.children.length && t.leaf_total && t.leaf_done === t.leaf_total)) return "done";
    if (t.status === "paused") return "paused";
    if (t.late) return "late";
    if (t.due_soon) return "soon";
    if (t.status === "doing" || t.done_pct > 0) return "doing";
    return "todo";
  }

  function weekRange() {
    const d = new Date();
    const monday = new Date(d.getFullYear(), d.getMonth(), d.getDate() - ((d.getDay() + 6) % 7));
    const sunday = new Date(monday.getFullYear(), monday.getMonth(), monday.getDate() + 6);
    return [isoDay(monday), isoDay(sunday)];
  }

  function subtreeIds(t, out = new Set()) {
    out.add(t.id);
    for (const c of t.children) subtreeIds(S.byId.get(c), out);
    return out;
  }

  // the board's tasks, indexed; each one knows its branch colour and its path
  function index() {
    S.byId = new Map(S.tasks.map((t) => [t.id, t]));
    S.tops = S.tasks.filter((t) => t.parent_id === null);
    const walk = (t, color, path) => {
      t._branch = color;
      t._path = path;
      for (const c of t.children) walk(S.byId.get(c), color, [...path, t.title]);
    };
    S.tops.forEach((t, i) => walk(t, BRANCH[i % BRANCH.length], []));
  }

  // ------------------------------------------------------------------ filters

  function anyFilter() {
    const f = S.f;
    return !!(f.q.trim() || f.who || f.prio || f.label || f.hideDone || f.week || f.late);
  }

  function matches(t) {
    const f = S.f;
    if (f.hideDone && stateOf(t) === "done") return false;
    if (f.who === "-" ? t.assignee : f.who && t.assignee !== f.who) return false;
    if (f.prio && t.priority !== f.prio) return false;
    if (f.label && !t.labels_list.includes(f.label)) return false;
    if (f.late && !t.late) return false;
    if (f.week) {
      const [mon, sun] = weekRange();
      const start = t.start_date || t.due_date, end = t.due_date || t.start_date;
      if (!start || start > sun || end < mon) return false;
    }
    const q = norm(f.q.trim());
    if (q && !norm(`${t.code} ${t.title} ${t.labels} ${personName(t.assignee)}`).includes(q)) return false;
    return true;
  }

  // id -> "match" | "ancestor" (shown faded, for a descendant that matches)
  function visibility() {
    const vis = new Map();
    const walk = (t) => {
      let any = false;
      for (const c of t.children) if (walk(S.byId.get(c))) any = true;
      const m = matches(t);
      if (m) vis.set(t.id, "match");
      else if (any) vis.set(t.id, "ancestor");
      return m || any;
    };
    S.tops.forEach(walk);
    return vis;
  }

  // ------------------------------------------------------------------ the page

  views.projects = async (arg) => {
    S.els = null;
    P = null;
    const list = await run(() => api("GET", "/api/pm/projects"));
    if (!list) return;
    S.projects = list.projects;
    S.people = list.people;
    S.manager = list.manager;
    if (arg && arg.pid) {
      S.pid = arg.pid;
      S.sel = arg.task || null;
    } else if (!S.projects.some((p) => p.id === S.pid)) {
      S.pid = S.projects.length ? S.projects[0].id : null;
    }
    if (!S.pid) S.view = "projects";
    else if (arg && arg.pid && S.view === "projects") S.view = "map";
    S.fitted = false;
    shell();
    if (S.pid && S.view !== "projects") await loadBoard(true);
    else showMain();
    if (arg && arg.task) revealTask(arg.task);
    refreshTimer = setInterval(poll, 30000);
  };

  function poll() {
    if (current !== "projects" || !S.els || S.view === "projects" || pending || document.hidden) return;
    const active = document.activeElement;
    if (S.els.panel.contains(active) || S.els.main.contains(active) || S.els.root.querySelector(".dragging")) return;
    loadBoard().catch(() => {});
  }

  async function loadBoard(first = false) {
    const pid = S.pid;
    let b;
    try {
      b = await api("GET", `/api/pm/projects/${pid}`);
    } catch (e) {
      toast(tr("Lỗi: ") + e.message);
      if (first) { S.pid = null; setView("projects"); }
      return;
    }
    if (pid !== S.pid || !S.els || current !== "projects") return;
    S.project = b.project;
    S.tasks = b.tasks;
    S.people = b.people;
    S.manager = b.manager;
    index();
    if (first) {
      S.collapsed = new Set(ls.get(`collapsed.${pid}`, []));
      S.fitted = false;
    }
    if (typeof S.sel === "number" && !S.byId.has(S.sel)) { S.sel = null; closePanel(); }
    toolbarOptions();
    showMain();
    if (P && S.byId.has(P.id)) P.sync(S.byId.get(P.id));
  }

  function shell() {
    const E = {};
    S.els = E;
    E.projSel = h("select", { class: "pm-proj-sel", "aria-label": tr("Dự án"), onchange: () => openProject(+E.projSel.value) });
    E.viewBtns = [["map", tr("Sơ đồ")], ["list", tr("Danh sách")], ["kanban", "Kanban"], ["projects", tr("Dự án")]]
      .map(([k, l]) => h("button", { class: "pm-vbtn", "data-v": k, onclick: () => setView(k) }, l));
    let qTimer;
    E.search = h("input", { type: "search", class: "pm-search", placeholder: tr("Tìm…"), "aria-label": tr("Tìm"),
      oninput: () => { clearTimeout(qTimer); qTimer = setTimeout(() => { S.f.q = E.search.value; filtersChanged(); }, 150); } });
    E.who = h("select", { "aria-label": tr("Người phụ trách"), onchange: () => { S.f.who = E.who.value; filtersChanged(); } });
    E.prio = selectOf([["", tr("Mọi ưu tiên")], ...PRIO_OPT], "", (v) => { S.f.prio = v; filtersChanged(); }, { "aria-label": tr("Ưu tiên") });
    E.label = h("select", { "aria-label": tr("Nhãn"), onchange: () => { S.f.label = E.label.value; filtersChanged(); } });
    E.hideDone = h("input", { type: "checkbox", onchange: () => { S.f.hideDone = E.hideDone.checked; filtersChanged(); } });
    const toggle = (key, label, title) => {
      const b = h("button", { class: "pm-tbtn", title, onclick: () => { S.f[key] = !S.f[key]; b.classList.toggle("active", S.f[key]); filtersChanged(); } }, label);
      return b;
    };
    E.week = toggle("week", tr("Tuần này"), tr("Việc có ngày làm trong tuần này"));
    E.late = toggle("late", tr("Trễ hạn"), tr("Chỉ việc đã quá hạn"));
    E.clear = h("button", { class: "pm-tbtn ghost", hidden: true, onclick: clearFilters }, tr("✕ Bỏ lọc"));
    E.filters = h("div", { class: "pm-filters" }, E.search, E.who, E.prio, E.label,
      h("label", { class: "pm-chk" }, E.hideDone, tr("Ẩn việc xong")), E.week, E.late, E.clear);
    E.filterBtn = h("button", { class: "pm-tbtn pm-filter-toggle", onclick: () => E.toolbar.classList.toggle("show-filters") }, tr("Lọc ▾"));
    E.logBtn = h("button", { class: "pm-tbtn", onclick: toggleLog }, tr("Nhật ký"));
    E.expand = h("button", { class: "pm-tbtn", onclick: expandAll }, tr("Mở hết"));
    E.fit = h("button", { class: "pm-tbtn pm-fit-btn", onclick: () => fit() }, tr("Vừa màn hình"));
    E.importInput = h("input", { type: "file", accept: ".json,application/json", hidden: true, onchange: () => importJson(E.importInput) });
    E.fileMenu = h("details", { class: "pm-menu" }, h("summary", { class: "pm-tbtn" }, tr("Tệp ▾")),
      h("div", { class: "menu" },
        E.exportJson = h("a", { class: "pm-mitem", download: "" }, tr("Xuất JSON")),
        E.importBtn = h("button", { class: "pm-mitem", onclick: () => { E.fileMenu.open = false; E.importInput.click(); } }, tr("Nhập JSON")),
        E.exportCsv = h("a", { class: "pm-mitem", download: "" }, tr("Xuất CSV")),
        h("button", { class: "pm-mitem", onclick: () => { E.fileMenu.open = false; printBoard(); } }, tr("In"))));
    E.help = h("div", { class: "pm-help card", hidden: true },
      h("b", {}, tr("Phím tắt trên sơ đồ")),
      h("ul", {},
        h("li", {}, tr("Bấm một nốt để xem và sửa chi tiết")),
        h("li", {}, h("kbd", {}, "Tab"), " ", tr("thêm nhánh con")),
        h("li", {}, h("kbd", {}, "Enter"), " ", tr("thêm việc cùng cấp")),
        h("li", {}, h("kbd", {}, "Delete"), " ", tr("xoá việc đang chọn")),
        h("li", {}, h("kbd", {}, "← ↑ → ↓"), " ", tr("chọn việc bên cạnh")),
        h("li", {}, h("kbd", {}, "F2"), " ", tr("sửa tên")),
        h("li", {}, tr("Kéo nền để di chuyển; Ctrl + cuộn chuột hoặc + / − để phóng to, thu nhỏ")),
        h("li", {}, tr("Kanban: kéo thẻ sang cột khác để đổi trạng thái"))));
    E.helpBtn = h("button", { class: "pm-tbtn", title: tr("Trợ giúp"), onclick: () => { E.help.hidden = !E.help.hidden; } }, "?");
    E.toolbar = h("div", { class: "pm-toolbar" },
      h("div", { class: "pm-tgroup" }, E.projSel, h("div", { class: "pm-views" }, E.viewBtns)),
      E.filterBtn, E.filters,
      h("div", { class: "pm-tgroup pm-tools" }, E.logBtn, E.expand, E.fit, E.fileMenu, E.helpBtn),
      E.importInput, E.help);
    E.main = h("div", { class: "pm-main" });
    E.log = h("aside", { class: "pm-log", hidden: true });
    E.panel = h("aside", { class: "pm-panel", hidden: true });
    E.body = h("div", { class: "pm-body" }, E.main, E.log, E.panel);
    E.root = h("div", { class: "pm" }, E.toolbar, E.body);
    render(E.root);
    E.root.addEventListener("click", (e) => {
      if (E.fileMenu.open && !e.target.closest(".pm-menu summary")) setTimeout(() => { E.fileMenu.open = false; }, 0);
      if (!E.help.hidden && !E.help.contains(e.target) && e.target !== E.helpBtn) E.help.hidden = true;
    });
    fitHeight();
  }

  // the page fills the window under the app's bar (the map and the lists scroll inside)
  function fitHeight() {
    if (!S.els || !S.els.root.isConnected) return;
    const top = S.els.root.getBoundingClientRect().top + window.scrollY;
    S.els.root.style.setProperty("--pm-top", `${Math.max(0, Math.round(top))}px`);
  }
  window.addEventListener("resize", () => { if (current === "projects") fitHeight(); });

  function toolbarOptions() {
    const E = S.els;
    const list = [...S.projects];
    if (S.project && !list.some((p) => p.id === S.project.id)) list.unshift(S.project);
    put(E.projSel, list.length ? list.map((p) => h("option", { value: p.id }, p.archived ? tr("{0} (lưu trữ)", p.name) : p.name))
      : [h("option", { value: "" }, tr("(chưa có dự án)"))]);
    E.projSel.value = S.pid || "";
    put(E.who, h("option", { value: "" }, tr("Mọi người")), h("option", { value: "-" }, tr("(Chưa giao)")),
      S.people.map((p) => h("option", { value: p.username }, p.name)));
    E.who.value = S.f.who;
    const labels = [...new Set(S.tasks.flatMap((t) => t.labels_list))].sort((a, b) => a.localeCompare(b, LOCALE));
    if (S.f.label && !labels.includes(S.f.label)) labels.push(S.f.label);
    put(E.label, h("option", { value: "" }, tr("Mọi nhãn")), labels.map((l) => h("option", { value: l }, l)));
    E.label.value = S.f.label;
    if (S.pid) {
      E.exportJson.href = `/api/pm/projects/${S.pid}/export.json`;
      E.exportCsv.href = `/api/pm/projects/${S.pid}/tasks.csv`;
    }
    E.importBtn.hidden = !S.manager;
  }

  function filtersChanged() {
    S.els.clear.hidden = !anyFilter();
    if (S.view !== "projects") showMain();
  }

  function clearFilters() {
    const E = S.els;
    S.f = { q: "", who: "", prio: "", label: "", hideDone: false, week: false, late: false };
    E.search.value = "";
    E.who.value = E.prio.value = E.label.value = "";
    E.hideDone.checked = false;
    E.week.classList.remove("active");
    E.late.classList.remove("active");
    filtersChanged();
  }

  function setView(v) {
    S.view = v;
    ls.set("view", v);
    if (v === "projects") { closePanel(); closeLog(); }
    showMain();
    if (v !== "projects" && S.pid && (!S.project || S.project.id !== S.pid)) loadBoard(true);
  }

  function showMain() {
    const E = S.els;
    if (!E) return;
    for (const b of E.viewBtns) b.classList.toggle("active", b.dataset.v === S.view);
    const board = S.view !== "projects";
    E.root.classList.toggle("pm-v-projects", !board);
    E.filters.hidden = E.filterBtn.hidden = !board;
    E.expand.hidden = E.logBtn.hidden = E.fileMenu.hidden = !board || !S.pid;
    E.fit.hidden = S.view !== "map";
    E.projSel.disabled = !S.projects.length && !S.project;
    if (!board) { renderProjects(); return; }
    if (!S.pid) {
      put(E.main, h("div", { class: "pm-pad" }, h("p", { class: "muted" }, tr("Chưa chọn dự án.")),
        h("button", { class: "primary", onclick: () => setView("projects") }, tr("Xem các dự án"))));
      return;
    }
    if (!S.project || S.project.id !== S.pid) { put(E.main, h("p", { class: "muted pm-pad" }, tr("Đang tải…"))); return; }
    if (S.view === "map") renderMap();
    else if (S.view === "list") renderList();
    else renderKanban();
    fitHeight();
  }

  async function openProject(pid, taskId) {
    if (!pid) return;
    await flushSave();
    closePanel();
    S.pid = pid;
    ls.set("project", pid);
    S.sel = taskId || null;
    S.project = null;
    if (S.view === "projects") { S.view = "map"; ls.set("view", "map"); }
    showMain();
    await loadBoard(true);
    if (taskId) revealTask(taskId);
  }

  // open a task: its ancestors unfolded, the panel open, the map centred on it
  function revealTask(id) {
    const t = S.byId.get(id);
    if (!t) return;
    let p = t.parent_id;
    let changed = false;
    while (p !== null) { if (S.collapsed.delete(p)) changed = true; p = S.byId.get(p).parent_id; }
    if (changed) saveCollapsed();
    select(id);
    showMain();
    if (S.view === "map") centerOn(id);
  }

  function saveCollapsed() {
    if (S.pid) ls.set(`collapsed.${S.pid}`, [...S.collapsed].filter((id) => S.byId.has(id)));
  }

  function expandAll() {
    S.collapsed.clear();
    saveCollapsed();
    showMain();
    if (S.view === "map") fit();
  }

  function toggleCollapse(id) {
    if (S.collapsed.has(id)) S.collapsed.delete(id);
    else S.collapsed.add(id);
    saveCollapsed();
    showMain();
  }

  function select(id) {
    S.sel = id;
    for (const el of S.els.root.querySelectorAll(".pm-node.selected, tr.active, .pm-card.selected")) el.classList.remove("selected", "active");
    for (const el of S.els.root.querySelectorAll(`[data-id="${id}"]`)) el.classList.add(el.tagName === "TR" ? "active" : "selected");
    if (typeof id === "number") openPanel(id);
    else closePanel();
    if (id !== null && S.view === "map") centerOn(id); // the panel may have covered it
  }

  function printBoard() {
    if (S.view === "map") fit();
    setTimeout(() => window.print(), 50);
  }

  async function importJson(input) {
    const file = input.files[0];
    input.value = "";
    if (!file) return;
    await run(async () => {
      let data;
      try { data = JSON.parse(await file.text()); } catch (_) { throw new Error(tr("Tệp không phải JSON")); }
      const p = await api("POST", "/api/pm/import", data);
      const list = await api("GET", "/api/pm/projects");
      S.projects = list.projects;
      await openProject(p.id);
    }, tr("Đã nhập dự án"));
  }

  // ------------------------------------------------------------------ tasks: add, delete, move

  async function addTask(parentId, afterId) {
    await flushSave();
    const body = { parent_id: parentId || null };
    if (afterId) body.after_id = afterId;
    const t = await run(() => api("POST", `/api/pm/projects/${S.pid}/tasks`, body));
    if (!t) return;
    if (parentId && S.collapsed.delete(parentId)) saveCollapsed();
    S.sel = t.id;
    await loadBoard();
    select(t.id);
    if (S.view === "map") centerOn(t.id, true);
    if (P) { P.title.focus(); P.title.select(); }
  }

  async function deleteTask(id) {
    const t = S.byId.get(id);
    if (!t) return;
    const n = subtreeIds(t).size;
    if (!confirm(n > 1 ? tr("Xoá “{0}” cùng {1} việc con?", t.title, n - 1) : tr("Xoá “{0}”?", t.title))) return;
    await flushSave();
    const ok = await run(() => api("DELETE", `/api/pm/tasks/${id}`), tr("Đã xoá"));
    if (!ok) return;
    S.sel = t.parent_id;
    closePanel();
    await loadBoard();
    select(S.sel || "root");
  }

  async function moveTask(id, body) {
    await flushSave();
    const ok = await run(() => api("POST", `/api/pm/tasks/${id}/move`, body));
    if (!ok) return;
    await loadBoard();
    if ("parent_id" in body) revealTask(id);
  }

  async function patchNow(id, data) {
    const ok = await run(() => api("PATCH", `/api/pm/tasks/${id}`, data));
    if (ok) await loadBoard();
    return ok;
  }

  // ------------------------------------------------------------------ mind map

  function mapEls() {
    const E = S.els;
    if (E.canvas) return E;
    E.stage = h("div", { class: "pm-stage" });
    E.zoomTxt = h("span", { class: "pm-zoom-txt" }, "100%");
    E.zoomBar = h("div", { class: "pm-zoom" },
      h("button", { title: tr("Thêm việc (con của nốt đang chọn)"), onclick: () => addTask(typeof S.sel === "number" ? S.sel : null) }, tr("+ Việc")),
      h("button", { title: tr("Thu nhỏ"), onclick: () => zoomBy(1 / 1.2) }, "−"), E.zoomTxt,
      h("button", { title: tr("Phóng to"), onclick: () => zoomBy(1.2) }, "+"),
      h("button", { title: tr("Vừa màn hình"), onclick: () => fit() }, "⤢"));
    E.legend = h("div", { class: "pm-legend" }, LEGEND.map((s) => h("span", { class: `st-${s}` }, h("i"), STATE_TEXT[s])));
    E.canvas = h("div", { class: "pm-canvas", tabindex: "0", "aria-label": tr("Sơ đồ công việc") }, E.stage, E.zoomBar, E.legend);
    mapEvents(E.canvas);
    return E;
  }

  function nodeFor(t, side, faded) {
    const st = stateOf(t);
    const leaf = !t.children.length;
    const sub = leaf
      ? [t.done_pct ? `${t.done_pct}%` : "", STATE_TEXT[st], t.start_date || t.due_date ? `${dm(t.start_date) || "…"} → ${dm(t.due_date) || "…"}` : "",
        t.checklist.length ? `☑ ${t.checklist.filter((i) => i.done).length}/${t.checklist.length}` : "",
        t.comment_count ? `💬 ${t.comment_count}` : "", t.file_count ? `📎 ${t.file_count}` : ""]
      : [`${t.done_pct}%`, STATE_TEXT[st], `${t.leaf_done}/${t.leaf_total}`];
    const cls = ["pm-node", `st-${st}`, side < 0 ? "left" : "right"];
    if (faded) cls.push("faded");
    if (S.sel === t.id) cls.push("selected");
    if (t.color) cls.push("tinted");
    const title = [t.code, t.assignee ? personName(t.assignee) : "", PRIO_TEXT[t.priority]].filter(Boolean).join(" · ");
    const el = h("div", { class: cls.join(" "), "data-id": t.id, title },
      h("span", { class: `pm-dot p-${t.priority}` }),
      h("div", { class: "pm-node-main" },
        h("div", { class: "pm-node-title" }, t.icon ? h("span", { class: "pm-ico" }, t.icon) : null, t.title),
        h("div", { class: "pm-node-sub" }, sub.filter(Boolean).join(" · ")),
        bar(t.done_pct)),
      t.avatar ? h("img", { class: "pm-ava", src: t.avatar, alt: "" }) : null,
      t.children.length ? h("button", { class: "pm-tog", "data-tog": t.id, title: S.collapsed.has(t.id) ? tr("Mở nhánh") : tr("Thu nhánh"), tabindex: "-1" },
        S.collapsed.has(t.id) ? `+${subtreeIds(t).size - 1}` : "−") : null);
    if (t.color) el.style.background = t.color;
    el.style.setProperty("--branch", t._branch);
    return el;
  }

  function renderMap() {
    const E = mapEls();
    if (E.main.firstChild !== E.canvas) put(E.main, E.canvas);
    const vis = visibility();
    const stage = E.stage;
    stage.replaceChildren();
    const links = svg("svg", { class: "pm-links" });
    stage.append(links);
    const p = S.project;
    const rootEl = h("div", { class: `pm-node root${S.sel === "root" ? " selected" : ""}`, "data-id": "root" },
      h("div", { class: "pm-node-main" },
        h("div", { class: "pm-root-title" }, p.name),
        h("div", { class: "pm-node-sub" }, tr("{0}/{1} việc xong · {2}%", p.done_count, p.task_count, p.progress)),
        bar(p.progress)));
    if (p.color) rootEl.style.setProperty("--branch", p.color);
    stage.append(rootEl);
    const R = { id: "root", el: rootEl, kids: [], side: 0 };
    const make = (t, side, parent) => {
      const n = { id: t.id, t, side, parent, el: nodeFor(t, side, vis.get(t.id) === "ancestor"), kids: [] };
      stage.append(n.el);
      if (!S.collapsed.has(t.id)) for (const c of t.children) if (vis.has(c)) n.kids.push(make(S.byId.get(c), side, n));
      return n;
    };
    const tops = S.tops.filter((t) => vis.has(t.id));
    const half = Math.ceil(tops.length / 2);
    const right = tops.slice(0, half).map((t) => make(t, 1, R));
    const left = tops.slice(half).map((t) => make(t, -1, R));
    R.right = right;
    R.left = left;
    // sizes, then a tidy tree on each side of the project
    const VG = 10, HG = 44, TOPG = 22, ROOTG = 70;
    const all = [];
    const measure = (n) => { n.w = n.el.offsetWidth; n.h = n.el.offsetHeight; all.push(n); n.kids.forEach(measure); };
    measure(R);
    right.forEach(measure);
    left.forEach(measure);
    const span = (n) => {
      n.kh = n.kids.reduce((a, k) => a + span(k), 0) + VG * Math.max(0, n.kids.length - 1);
      n.sh = Math.max(n.h, n.kh);
      return n.sh;
    };
    const place = (n, edge, top) => {
      n.x = n.side > 0 ? edge : edge - n.w;
      n.y = top + (n.sh - n.h) / 2;
      let y = top + (n.sh - n.kh) / 2;
      const next = n.side > 0 ? n.x + n.w + HG : n.x - HG;
      for (const k of n.kids) { place(k, next, y); y += k.sh + VG; }
    };
    R.x = -R.w / 2;
    R.y = -R.h / 2;
    for (const [list, side] of [[right, 1], [left, -1]]) {
      const total = list.reduce((a, n) => a + span(n), 0) + TOPG * Math.max(0, list.length - 1);
      let y = -total / 2;
      for (const n of list) { place(n, side > 0 ? R.x + R.w + ROOTG : R.x - ROOTG, y); y += n.sh + TOPG; }
    }
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (const n of all) {
      n.el.style.left = `${n.x}px`;
      n.el.style.top = `${n.y}px`;
      minX = Math.min(minX, n.x); minY = Math.min(minY, n.y);
      maxX = Math.max(maxX, n.x + n.w); maxY = Math.max(maxY, n.y + n.h);
    }
    const pad = 20;
    S.bounds = { minX, minY, w: maxX - minX, h: maxY - minY };
    links.setAttribute("width", maxX - minX + pad * 2);
    links.setAttribute("height", maxY - minY + pad * 2);
    links.style.left = `${minX - pad}px`;
    links.style.top = `${minY - pad}px`;
    const ox = minX - pad, oy = minY - pad;
    for (const n of all) {
      if (n === R) continue;
      const par = n.parent;
      const x1 = (n.side > 0 ? par.x + par.w : par.x) - ox, y1 = par.y + par.h / 2 - oy;
      const x2 = (n.side > 0 ? n.x : n.x + n.w) - ox, y2 = n.y + n.h / 2 - oy;
      const mx = (x1 + x2) / 2;
      const path = svg("path", { d: `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`, stroke: n.t._branch,
        "stroke-width": par === R ? 3 : 2, fill: "none" });
      if (n.el.classList.contains("faded")) path.setAttribute("opacity", "0.35");
      links.append(path);
    }
    S.layout = new Map(all.map((n) => [n.id, n]));
    if (!S.fitted && E.canvas.clientWidth) {
      S.fitted = true;
      fit();
      // on a phone the whole map would be too small to read: start from the project, readable
      if (E.canvas.clientWidth < 800 && S.zoom < 0.55) { S.zoom = 0.55; centerOn("root", false); }
    } else applyTransform();
  }

  function applyTransform() {
    const E = S.els;
    if (!E || !E.stage) return;
    E.stage.style.transform = `translate(${S.panX}px, ${S.panY}px) scale(${S.zoom})`;
    E.zoomTxt.textContent = `${Math.round(S.zoom * 100)}%`;
  }

  function fit() {
    const E = S.els;
    if (!E || !E.canvas || !S.bounds || S.view !== "map") return;
    const cw = E.canvas.clientWidth, ch = E.canvas.clientHeight;
    if (!cw || !ch) return;
    // room for the zoom buttons above and the legend below
    const b = S.bounds, pad = 24, top = 56, bottom = 44;
    S.zoom = clamp(Math.min((cw - pad * 2) / Math.max(b.w, 1), (ch - top - bottom) / Math.max(b.h, 1)), 0.15, 1.2);
    S.panX = cw / 2 - (b.minX + b.w / 2) * S.zoom;
    S.panY = top + (ch - top - bottom) / 2 - (b.minY + b.h / 2) * S.zoom;
    applyTransform();
  }

  function zoomAt(cx, cy, z) {
    const r = S.els.canvas.getBoundingClientRect();
    const mx = cx - r.left, my = cy - r.top;
    z = clamp(z, 0.15, 2.5);
    S.panX = mx - ((mx - S.panX) * z) / S.zoom;
    S.panY = my - ((my - S.panY) * z) / S.zoom;
    S.zoom = z;
    applyTransform();
  }

  function zoomBy(k) {
    const r = S.els.canvas.getBoundingClientRect();
    zoomAt(r.left + r.width / 2, r.top + r.height / 2, S.zoom * k);
  }

  // keep a node in view (or centre it)
  function centerOn(id, onlyIfHidden = true) {
    const n = S.layout.get(id);
    const c = S.els && S.els.canvas;
    if (!n || !c || S.view !== "map") return;
    const cw = c.clientWidth, ch = c.clientHeight;
    const x = n.x * S.zoom + S.panX, y = n.y * S.zoom + S.panY;
    const inside = x >= 10 && y >= 10 && x + n.w * S.zoom <= cw - 10 && y + n.h * S.zoom <= ch - 50;
    if (onlyIfHidden && inside) return;
    S.panX = cw / 2 - (n.x + n.w / 2) * S.zoom;
    S.panY = ch / 2 - (n.y + n.h / 2) * S.zoom;
    applyTransform();
  }

  function mapEvents(canvas) {
    const pts = new Map();
    let pan = null, pinch = null, suppress = false;
    const two = () => { const [a, b] = [...pts.values()]; return { d: Math.hypot(a[0] - b[0], a[1] - b[1]) || 1, x: (a[0] + b[0]) / 2, y: (a[1] + b[1]) / 2 }; };
    canvas.addEventListener("pointerdown", (e) => {
      if (e.target.closest(".pm-zoom, .pm-tog, .pm-legend") || (e.pointerType === "mouse" && e.button !== 0)) return;
      canvas.focus({ preventScroll: true });
      pts.set(e.pointerId, [e.clientX, e.clientY]);
      if (pts.size === 1) pan = { x: e.clientX, y: e.clientY, px: S.panX, py: S.panY, moved: false };
      else if (pts.size === 2) { pinch = { ...two(), z: S.zoom }; pan = null; }
    });
    // dragging the map must not select the text around it
    canvas.addEventListener("mousedown", (e) => { if (!e.target.closest("input, select, textarea")) e.preventDefault(); });
    canvas.addEventListener("pointermove", (e) => {
      if (!pts.has(e.pointerId)) return;
      pts.set(e.pointerId, [e.clientX, e.clientY]);
      if (pinch && pts.size === 2) {
        const now = two();
        zoomAt(now.x, now.y, (pinch.z * now.d) / pinch.d);
        suppress = true;
      } else if (pan) {
        const dx = e.clientX - pan.x, dy = e.clientY - pan.y;
        if (!pan.moved && Math.hypot(dx, dy) > 4) {
          pan.moved = true;
          try { canvas.setPointerCapture(e.pointerId); } catch (_) { /* already gone */ }
          canvas.classList.add("panning");
        }
        if (pan.moved) { S.panX = pan.px + dx; S.panY = pan.py + dy; applyTransform(); }
      }
    });
    const up = (e) => {
      if (!pts.delete(e.pointerId)) return;
      if (pan && pan.moved) suppress = true;
      if (pts.size < 2) pinch = null;
      if (!pts.size) { pan = null; canvas.classList.remove("panning"); setTimeout(() => { suppress = false; }, 0); }
    };
    canvas.addEventListener("pointerup", up);
    canvas.addEventListener("pointercancel", up);
    canvas.addEventListener("click", (e) => {
      if (suppress) return;
      const tog = e.target.closest(".pm-tog");
      if (tog) { toggleCollapse(+tog.dataset.tog); canvas.focus({ preventScroll: true }); return; }
      const node = e.target.closest(".pm-node");
      if (!node) return;
      select(node.dataset.id === "root" ? "root" : +node.dataset.id);
    });
    canvas.addEventListener("dblclick", (e) => {
      const node = e.target.closest(".pm-node");
      if (node && node.dataset.id !== "root" && P) { P.title.focus(); P.title.select(); }
    });
    canvas.addEventListener("wheel", (e) => {
      e.preventDefault();
      if (e.ctrlKey || e.metaKey) zoomAt(e.clientX, e.clientY, S.zoom * Math.exp(-e.deltaY * 0.0015));
      else { S.panX -= e.deltaX; S.panY -= e.deltaY; applyTransform(); }
    }, { passive: false });
    canvas.addEventListener("keydown", mapKey);
  }

  function mapKey(e) {
    if (e.target !== S.els.canvas) return;
    if (e.key === "+" || e.key === "=") { zoomBy(1.2); e.preventDefault(); return; }
    if (e.key === "-") { zoomBy(1 / 1.2); e.preventDefault(); return; }
    if (!S.sel) { if (e.key.startsWith("Arrow")) { select("root"); e.preventDefault(); } return; }
    const id = S.sel;
    const t = typeof id === "number" ? S.byId.get(id) : null;
    if (e.key === "Tab" && !e.shiftKey) { e.preventDefault(); addTask(t ? t.id : null); return; }
    if (e.key === "Enter") { e.preventDefault(); if (t) addTask(t.parent_id, t.id); else addTask(null); return; }
    if ((e.key === "Delete" || e.key === "Backspace") && t) { e.preventDefault(); deleteTask(t.id); return; }
    if (e.key === "F2" && P) { e.preventDefault(); P.title.focus(); P.title.select(); return; }
    if (e.key === "Escape") { closePanel(); return; }
    const n = S.layout.get(id);
    if (!n || !e.key.startsWith("Arrow")) return;
    e.preventDefault();
    let to = null;
    if (n.id === "root") {
      if (e.key === "ArrowRight") to = n.right[0];
      else if (e.key === "ArrowLeft") to = n.left[0];
    } else {
      const outward = n.side > 0 ? "ArrowRight" : "ArrowLeft";
      const inward = n.side > 0 ? "ArrowLeft" : "ArrowRight";
      const sibs = n.parent.id === "root" ? (n.side > 0 ? n.parent.right : n.parent.left) : n.parent.kids;
      const i = sibs.indexOf(n);
      if (e.key === outward) to = n.kids[0];
      else if (e.key === inward) to = n.parent;
      else if (e.key === "ArrowUp") to = sibs[i - 1];
      else if (e.key === "ArrowDown") to = sibs[i + 1];
    }
    if (to) { select(to.id); centerOn(to.id); }
  }

  // ------------------------------------------------------------------ list

  function renderList() {
    const E = S.els;
    const vis = visibility();
    const rows = [];
    const walk = (t) => {
      if (!vis.has(t.id)) return;
      const kids = t.children.filter((c) => vis.has(c));
      const closed = S.collapsed.has(t.id);
      const st = stateOf(t);
      const status = selectOf(STATUS_OPT, t.status, (v) => patchNow(t.id, { status: v }), { class: "pm-inline", "aria-label": tr("Trạng thái") });
      status.addEventListener("click", (ev) => ev.stopPropagation());
      const arrow = kids.length || (closed && t.children.length)
        ? h("button", { class: "pm-arrow", onclick: (ev) => { ev.stopPropagation(); toggleCollapse(t.id); } }, closed ? "▸" : "▾")
        : h("span", { class: "pm-arrow" });
      const titleTd = h("td", { class: "pm-title-cell" }, arrow, t.icon ? h("span", { class: "pm-ico" }, t.icon) : null,
        h("span", { class: t.children.length ? "pm-branch-title" : "" }, t.title));
      titleTd.style.paddingLeft = `${6 + t.depth * 18}px`;
      const due = h("td", { class: t.late ? "pm-late" : t.due_soon ? "pm-soon" : "" }, dm(t.due_date) || "—");
      const tr_ = h("tr", { class: `clickable st-${st}${vis.get(t.id) === "ancestor" ? " faded" : ""}${S.sel === t.id ? " active" : ""}`, "data-id": t.id,
        onclick: () => select(t.id) },
        h("td", { class: "mono" }, h("span", { class: "pm-state-dot" }), t.code), titleTd,
        h("td", {}, personName(t.assignee) || "—"),
        h("td", {}, h("span", { class: `pm-dot inline p-${t.priority}` }), PRIO_TEXT[t.priority]),
        h("td", {}, status),
        h("td", { class: "pm-pct" }, bar(t.done_pct), h("span", { class: "muted" }, `${t.done_pct}%`)),
        h("td", {}, dm(t.start_date) || "—"), due,
        h("td", {}, t.labels_list.map((l) => h("span", { class: "pm-chip" }, l))));
      rows.push(tr_);
      if (!closed) kids.forEach((c) => walk(S.byId.get(c)));
    };
    S.tops.forEach(walk);
    put(E.main, h("div", { class: "pm-scroll" },
      h("div", { class: "row pm-pad" }, h("button", { class: "primary", onclick: () => addTask(null) }, tr("+ Việc mới")),
        h("span", { class: "muted" }, tr("{0} việc", rows.length))),
      h("div", { class: "card table-wrap pm-table" }, rows.length ? h("table", {},
        h("thead", {}, h("tr", {}, [tr("Mã"), tr("Công việc"), tr("Phụ trách"), tr("Ưu tiên"), tr("Trạng thái"), "%", tr("Bắt đầu"), tr("Kết thúc"), tr("Nhãn")]
          .map((x) => h("th", {}, x)))),
        h("tbody", {}, rows)) : h("p", { class: "muted" }, anyFilter() ? tr("Không có việc nào khớp bộ lọc.") : tr("Chưa có công việc.")))));
  }

  // ------------------------------------------------------------------ Kanban

  function renderKanban() {
    const E = S.els;
    const tasks = S.tasks.filter((t) => (S.kanbanAll || !t.children.length) && matches(t));
    const cols = STATUS_OPT.map(([status, label]) => {
      const mine = tasks.filter((t) => t.status === status);
      const col = h("div", { class: `pm-col st-${status}`, "data-status": status },
        h("div", { class: "pm-col-head" }, h("b", {}, label), h("span", { class: "pill neutral" }, mine.length)),
        h("div", { class: "pm-col-body" }, mine.map(card)));
      col.addEventListener("dragover", (e) => { e.preventDefault(); e.dataTransfer.dropEffect = "move"; col.classList.add("over"); });
      col.addEventListener("dragleave", (e) => { if (!col.contains(e.relatedTarget)) col.classList.remove("over"); });
      col.addEventListener("drop", (e) => {
        e.preventDefault();
        col.classList.remove("over");
        const id = +e.dataTransfer.getData("text/plain");
        const t = S.byId.get(id);
        if (t && t.status !== status) patchNow(id, { status });
      });
      return col;
    });
    const all = h("input", { type: "checkbox", checked: S.kanbanAll, onchange: () => { S.kanbanAll = all.checked; showMain(); } });
    put(E.main, h("div", { class: "pm-scroll" },
      h("div", { class: "row pm-pad" }, h("button", { class: "primary", onclick: () => addTask(null) }, tr("+ Việc mới")),
        h("label", { class: "pm-chk" }, all, tr("Cả nhánh")),
        h("span", { class: "muted" }, S.kanbanAll ? tr("Mọi việc, cả các nhánh") : tr("Chỉ các việc cuối (không có nhánh con)"))),
      h("div", { class: "pm-kanban" }, cols)));
  }

  function card(t) {
    const st = stateOf(t);
    const c = h("div", { class: `pm-card st-${st}${S.sel === t.id ? " selected" : ""}`, draggable: "true", "data-id": t.id, onclick: () => select(t.id) },
      t._path.length ? h("div", { class: "pm-card-path" }, t._path.join(" › ")) : null,
      h("div", { class: "pm-card-title" }, h("span", { class: `pm-dot inline p-${t.priority}`, title: PRIO_TEXT[t.priority] }),
        t.icon ? h("span", { class: "pm-ico" }, t.icon) : null, t.title),
      h("div", { class: "pm-card-meta" }, h("span", { class: "mono" }, t.code),
        t.assignee ? h("span", {}, "👤 ", personName(t.assignee)) : null,
        t.due_date ? h("span", { class: t.late ? "pm-late" : t.due_soon ? "pm-soon" : "" }, "📅 ", dm(t.due_date)) : null,
        t.comment_count ? h("span", {}, `💬 ${t.comment_count}`) : null, t.file_count ? h("span", {}, `📎 ${t.file_count}`) : null),
      t.labels_list.length ? h("div", {}, t.labels_list.map((l) => h("span", { class: "pm-chip" }, l))) : null,
      h("div", { class: "pm-pct" }, bar(t.done_pct), h("span", { class: "muted" }, `${t.done_pct}%`)));
    if (t.color) { c.style.background = t.color; c.classList.add("tinted"); }
    c.addEventListener("dragstart", (e) => { e.dataTransfer.setData("text/plain", String(t.id)); e.dataTransfer.effectAllowed = "move"; c.classList.add("dragging"); });
    c.addEventListener("dragend", () => c.classList.remove("dragging"));
    return c;
  }

  // ------------------------------------------------------------------ projects

  async function renderProjects() {
    const E = S.els;
    const box = h("div", { class: "pm-scroll pm-projects" }, h("p", { class: "muted" }, tr("Đang tải…")));
    put(E.main, box);
    const [list, my] = await Promise.all([
      api("GET", `/api/pm/projects${S.archived ? "?archived=1" : ""}`).catch((e) => { toast(tr("Lỗi: ") + e.message); return null; }),
      api("GET", "/api/pm/my").catch(() => ({ tasks: [] })),
    ]);
    if (!list || !S.els || S.view !== "projects") return;
    if (!S.archived) S.projects = list.projects;
    S.people = list.people;
    S.manager = list.manager;
    toolbarOptions();
    const reload = () => renderProjects();
    const mine = my.tasks.slice(0, 30);
    const myBox = h("div", { class: "card pm-my" }, h("h2", {}, tr("Việc của tôi")),
      mine.length ? h("div", { class: "pm-my-list" }, mine.map((t) => h("button", { class: `pm-my-item st-${stateOf(t)}`, onclick: () => openProject(t.project_id, t.id) },
        h("span", { class: "pm-state-dot" }),
        h("span", { class: "pm-my-title" }, t.icon ? `${t.icon} ` : "", t.title, h("span", { class: "muted" }, ` · ${t.project_name} · ${t.code}`)),
        h("span", { class: t.late ? "pm-late" : t.due_soon ? "pm-soon" : "muted" }, t.due_date ? dm(t.due_date) : ""),
        h("span", { class: "muted" }, `${STATE_TEXT[t.status]} · ${t.done_pct}%`))))
        : h("p", { class: "muted" }, tr("Không có việc nào đang giao cho bạn.")));
    const archivedChk = h("input", { type: "checkbox", checked: S.archived, onchange: () => { S.archived = archivedChk.checked; reload(); } });
    const importInput = h("input", { type: "file", accept: ".json,application/json", hidden: true, onchange: () => importJson(importInput) });
    const head = h("div", { class: "row spread pm-pad0" }, h("h2", {}, S.archived ? tr("Dự án lưu trữ") : tr("Dự án")),
      h("div", { class: "row" },
        h("label", { class: "pm-chk" }, archivedChk, tr("Xem dự án lưu trữ")),
        S.manager ? h("button", { onclick: () => importInput.click() }, tr("Nhập JSON")) : null, importInput,
        S.manager ? h("button", { class: "primary", onclick: newProject }, tr("+ Dự án mới")) : null));
    const cards = list.projects.map((p) => projectCard(p, reload));
    put(box, S.archived ? null : myBox, head,
      cards.length ? h("div", { class: "grid pm-proj-grid" }, cards)
        : h("p", { class: "muted" }, S.archived ? tr("Không có dự án lưu trữ.") : S.manager ? tr("Chưa có dự án. Bấm “+ Dự án mới”.") : tr("Chưa có dự án nào.")));
  }

  function projectCard(p, reload) {
    const color = h("input", { type: "color", value: p.color || "#6366f1", title: tr("Màu"),
      onchange: () => run(async () => { await api("PATCH", `/api/pm/projects/${p.id}`, { color: color.value }); reload(); }) });
    const stop = (fn) => (e) => { e.stopPropagation(); fn(); };
    const el = h("div", { class: "card pm-proj", onclick: () => openProject(p.id) },
      h("div", { class: "row spread" }, h("h3", {}, p.name), p.archived ? h("span", { class: "pill neutral" }, tr("lưu trữ")) : null),
      h("div", { class: "pm-pct" }, bar(p.progress), h("b", {}, `${p.progress}%`)),
      h("div", { class: "row" }, h("span", {}, tr("{0}/{1} việc xong", p.done_count, p.task_count)),
        p.late_count ? h("span", { class: "pill bad" }, tr("{0} trễ hạn", p.late_count)) : null,
        p.soon_count ? h("span", { class: "pill warn" }, tr("{0} sắp đến hạn", p.soon_count)) : null),
      h("div", { class: "muted" }, tr("Cập nhật {0}", fmtTime(p.updated))),
      S.manager ? h("div", { class: "row pm-proj-actions", onclick: (e) => e.stopPropagation() },
        h("button", { class: "small", onclick: stop(() => renameProject(p, reload)) }, tr("Đổi tên")), color,
        h("button", { class: "small", onclick: stop(() => run(async () => { await api("PATCH", `/api/pm/projects/${p.id}`, { archived: !p.archived }); reload(); },
          p.archived ? tr("Đã bỏ lưu trữ") : tr("Đã lưu trữ"))) }, p.archived ? tr("Bỏ lưu trữ") : tr("Lưu trữ")),
        h("button", { class: "small danger", onclick: stop(() => deleteProject(p, reload)) }, tr("Xoá"))) : null);
    el.style.setProperty("--branch", p.color || "#6366f1");
    return el;
  }

  async function newProject() {
    const name = (prompt(tr("Tên dự án mới")) || "").trim();
    if (!name) return;
    const p = await run(() => api("POST", "/api/pm/projects", { name, color: BRANCH[Math.floor(Math.random() * BRANCH.length)] }), tr("Đã tạo dự án"));
    if (!p) return;
    const list = await run(() => api("GET", "/api/pm/projects"));
    if (list) S.projects = list.projects;
    openProject(p.id);
  }

  async function renameProject(p, reload) {
    const name = (prompt(tr("Tên dự án"), p.name) || "").trim();
    if (!name || name === p.name) return;
    await run(async () => { await api("PATCH", `/api/pm/projects/${p.id}`, { name }); reload(); }, tr("Đã đổi tên"));
  }

  async function deleteProject(p, reload) {
    const typed = prompt(tr("Xoá hẳn dự án “{0}” cùng mọi công việc, bình luận và tệp? Gõ tên dự án để xác nhận.", p.name));
    if (typed === null) return;
    if (typed.trim() !== p.name) { toast(tr("Tên không khớp, chưa xoá")); return; }
    await run(async () => {
      await api("DELETE", `/api/pm/projects/${p.id}`);
      if (S.pid === p.id) { S.pid = null; S.project = null; ls.set("project", null); }
      reload();
    }, tr("Đã xoá dự án"));
  }

  // ------------------------------------------------------------------ log

  function closeLog() {
    S.logOpen = false;
    if (S.els) { S.els.log.hidden = true; S.els.logBtn.classList.remove("active"); }
  }

  async function toggleLog() {
    const E = S.els;
    if (S.logOpen) { closeLog(); return; }
    S.logOpen = true;
    E.logBtn.classList.add("active");
    E.log.hidden = false;
    put(E.log, h("p", { class: "muted" }, tr("Đang tải…")));
    const r = await run(() => api("GET", `/api/pm/projects/${S.pid}/log?limit=200`));
    if (!r || !S.logOpen) return;
    put(E.log, h("div", { class: "row spread pm-panel-head" }, h("h3", {}, tr("Nhật ký dự án")), h("button", { class: "ghost", title: tr("Đóng"), onclick: closeLog }, "✕")),
      r.log.length ? r.log.map((l) => h("div", { class: `pm-log-item${l.task_id && S.byId.has(l.task_id) ? " clickable" : ""}`,
        onclick: () => l.task_id && S.byId.has(l.task_id) && revealTask(l.task_id) },
      h("div", {}, h("b", {}, personName(l.actor)), " ", LOG_TEXT[l.action] || l.action),
      l.detail ? h("div", { class: "pm-log-detail" }, l.detail) : null,
      h("div", { class: "muted" }, fmtTime(l.ts)))) : h("p", { class: "muted" }, tr("Chưa có hoạt động.")));
  }

  // ------------------------------------------------------------------ saving

  let pending = null; // {id, data}: changes not sent yet
  let saveTimer = null;
  let saving = Promise.resolve();

  function queue(id, data, delay = 600) {
    if (pending && pending.id !== id) flushSave();
    if (!pending) pending = { id, data: {} };
    Object.assign(pending.data, data);
    clearTimeout(saveTimer);
    saveTimer = setTimeout(flushSave, delay);
    savedState("saving");
  }

  function flushSave() {
    clearTimeout(saveTimer);
    if (!pending) return saving;
    const { id, data } = pending;
    pending = null;
    saving = saving.then(async () => {
      try {
        await api("PATCH", `/api/pm/tasks/${id}`, data);
        savedState("saved");
      } catch (e) {
        toast(tr("Lỗi: ") + e.message);
        savedState("error");
      }
      if (current === "projects") loadBoard().catch(() => {}); // not awaited: a later page must not hold up saving
    });
    return saving;
  }

  let savedTimer;
  function savedState(state) {
    if (!P) return;
    const el = P.saved;
    el.className = `pm-saved ${state}`;
    el.textContent = { saving: tr("Đang lưu…"), saved: tr("Đã lưu"), error: tr("Chưa lưu được") }[state];
    clearTimeout(savedTimer);
    if (state === "saved") savedTimer = setTimeout(() => { el.textContent = ""; }, 2000);
  }

  // ------------------------------------------------------------------ the detail panel

  function closePanel() {
    if (P) flushSave();
    P = null;
    if (!S.els) return;
    S.els.panel.hidden = true;
    S.els.root.classList.remove("with-panel");
    put(S.els.panel);
  }

  function openPanel(id) {
    const t = S.byId.get(id);
    if (!t) return;
    if (P && P.id === id) { P.sync(t); return; }
    if (P) flushSave();
    P = buildPanel(t);
    const E = S.els;
    put(E.panel, P.el);
    E.panel.hidden = false;
    E.root.classList.add("with-panel");
    E.panel.scrollTop = 0;
  }

  // Safe rich text: only simple formatting elements, rebuilt one by one (links: http, https, mailto).
  function noteNodes(html) {
    const doc = new DOMParser().parseFromString(html || "", "text/html");
    const frag = document.createDocumentFragment();
    const copy = (from, to) => {
      for (const n of from.childNodes) {
        if (n.nodeType === Node.TEXT_NODE) { to.append(document.createTextNode(n.nodeValue)); continue; }
        if (n.nodeType !== Node.ELEMENT_NODE || DROP_TAGS.has(n.tagName)) continue;
        if (!NOTE_TAGS.has(n.tagName)) { copy(n, to); continue; }
        const el = document.createElement(n.tagName.toLowerCase());
        if (n.tagName === "A") {
          const href = (n.getAttribute("href") || "").trim();
          if (/^(https?:\/\/|mailto:)/i.test(href)) { el.setAttribute("href", href); el.setAttribute("target", "_blank"); el.setAttribute("rel", "noopener noreferrer"); }
        }
        copy(n, el);
        to.append(el);
      }
    };
    copy(doc.body, frag);
    return frag;
  }

  function readAvatar(file) {
    return new Promise((resolve, reject) => {
      const fail = () => reject(new Error(tr("Không đọc được ảnh")));
      const fr = new FileReader();
      fr.onerror = fail;
      fr.onload = () => {
        const img = new Image();
        img.onerror = fail;
        img.onload = () => {
          const k = Math.min(1, 96 / Math.max(img.naturalWidth, img.naturalHeight));
          const w = Math.max(1, Math.round(img.naturalWidth * k)), hh = Math.max(1, Math.round(img.naturalHeight * k));
          const c = document.createElement("canvas");
          c.width = w;
          c.height = hh;
          const ctx = c.getContext("2d");
          let url = "";
          if (file.type === "image/png") { ctx.drawImage(img, 0, 0, w, hh); url = c.toDataURL("image/png"); }
          if (!url || url.length > MAX_AVATAR) {
            ctx.fillStyle = "#ffffff";
            ctx.fillRect(0, 0, w, hh);
            ctx.drawImage(img, 0, 0, w, hh);
            url = c.toDataURL("image/jpeg", 0.85);
          }
          if (url.length > MAX_AVATAR) reject(new Error(tr("Ảnh quá lớn")));
          else resolve(url);
        };
        img.src = fr.result;
      };
      fr.readAsDataURL(file);
    });
  }

  function buildPanel(t0) {
    const id = t0.id;
    const binds = [];
    const self = { id };
    let task = t0;
    // a control shows the task's value unless it is being edited (focused, or its change not sent yet)
    const bind = (key, el, set) => { binds.push({ key, el, set }); set(task, el); return el; };
    const later = (data) => queue(id, data);
    const now = (data) => queue(id, data, 0);

    const crumbs = h("div", { class: "pm-crumbs" });
    self.saved = h("span", { class: "pm-saved" });
    const head = h("div", { class: "pm-panel-head" },
      crumbs, h("div", { class: "row" }, self.saved, h("button", { class: "ghost", title: tr("Đóng"), onclick: () => select(null) }, "✕")));

    // title and code
    self.title = bind("title", h("input", { class: "pm-title-input", maxlength: "300",
      oninput: () => self.title.value.trim() && later({ title: self.title.value }),
      onkeydown: (e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          if (self.title.value.trim()) now({ title: self.title.value });
          if (S.view === "map" && S.els.canvas) S.els.canvas.focus({ preventScroll: true });
        }
      } }), (t, el) => { el.value = t.title; });
    const code = h("input", { readonly: true, class: "mono pm-code", value: t0.code, "aria-label": tr("Mã") });

    // icon and colour
    const icons = h("div", { class: "pm-picker" });
    const iconBtn = (v, label) => h("button", { class: "pm-emoji", "data-v": v, title: v ? v : tr("Không"), onclick: () => { now({ icon: v }); mark(icons, v); } }, label);
    icons.append(iconBtn("", "∅"), ...EMOJI.map((e) => iconBtn(e, e)));
    const mark = (box, v) => { for (const b of box.children) b.classList.toggle("active", b.dataset.v === v); };
    bind("icon", icons, (t) => mark(icons, t.icon || ""));
    const colors = h("div", { class: "pm-picker" });
    const swatch = (v) => {
      const b = h("button", { class: `pm-swatch${v ? "" : " none"}`, "data-v": v, title: v || tr("Không"), onclick: () => { now({ color: v }); mark(colors, v); } }, v ? "" : "∅");
      if (v) b.style.background = v;
      return b;
    };
    colors.append(swatch(""), ...SWATCHES.map(swatch));
    bind("color", colors, (t) => mark(colors, t.color || ""));

    // who, priority, status, progress
    const assignee = bind("assignee", h("select", { onchange: () => now({ assignee: assignee.value }) }), (t, el) => {
      const opts = [["", tr("(Chưa giao)")], ...S.people.map((p) => [p.username, p.name])];
      if (t.assignee && !S.people.some((p) => p.username === t.assignee)) opts.push([t.assignee, t.assignee]);
      put(el, opts.map(([v, l]) => h("option", { value: v }, l)));
      el.value = t.assignee || "";
    });
    const prio = bind("priority", selectOf(PRIO_OPT, t0.priority, (v) => now({ priority: v })), (t, el) => { el.value = t.priority; });
    const status = bind("status", selectOf(STATUS_OPT, t0.status, (v) => now({ status: v })), (t, el) => { el.value = t.status; });
    const progress = bind("progress", h("select", { onchange: () => now({ progress: +progress.value }) }), (t, el) => {
      const auto = t.children.length > 0 || (t.track_checklist && t.checklist.length > 0);
      const opts = [];
      for (let v = 0; v <= 100; v += 10) opts.push(v);
      const shown = auto ? t.done_pct : t.progress;
      if (!opts.includes(shown)) opts.push(shown);
      opts.sort((a, b) => a - b);
      put(el, opts.map((v) => h("option", { value: v }, auto && v === shown ? tr("{0}% (tự tính)", v) : `${v}%`)));
      el.value = String(shown);
      el.disabled = auto;
    });
    const labels = bind("labels", h("input", { placeholder: tr("vd: marketing, gấp"), oninput: () => later({ labels: labels.value }) }), (t, el) => { el.value = t.labels; });

    // the node's picture
    const avaImg = h("img", { class: "pm-ava big", alt: "" });
    const avaFile = h("input", { type: "file", accept: "image/*", hidden: true, onchange: async () => {
      const f = avaFile.files[0];
      avaFile.value = "";
      if (!f) return;
      const url = await run(() => readAvatar(f));
      if (url) { avaImg.src = url; avaImg.hidden = false; now({ avatar: url }); }
    } });
    const avatar = bind("avatar", h("div", { class: "row" }, avaImg,
      h("button", { class: "small", onclick: () => avaFile.click() }, tr("Chọn ảnh")),
      h("button", { class: "small", onclick: () => { avaImg.hidden = true; avaImg.removeAttribute("src"); now({ avatar: "" }); } }, tr("Bỏ ảnh")), avaFile), (t) => {
      avaImg.hidden = !t.avatar;
      if (t.avatar) avaImg.src = t.avatar;
      else avaImg.removeAttribute("src");
    });

    // dependencies and dates
    const waiting = h("div", { class: "pm-warn" });
    const depends = bind("depends", h("input", { class: "mono", placeholder: "ABC12, XYZ34", oninput: () => later({ depends: depends.value }) }), (t, el) => { el.value = t.depends; });
    const start = bind("start_date", h("input", { type: "date", onchange: () => now({ start_date: start.value }) }), (t, el) => { el.value = t.start_date || ""; });
    const due = bind("due_date", h("input", { type: "date", onchange: () => now({ due_date: due.value }) }), (t, el) => { el.value = t.due_date || ""; });

    // notes: rich text
    const editor = h("div", { class: "pm-editor", contenteditable: "true", role: "textbox", "aria-multiline": "true", "aria-label": tr("Ghi chú") });
    let range = null;
    const keepRange = () => {
      const s = getSelection();
      if (s.rangeCount && editor.contains(s.getRangeAt(0).commonAncestorContainer)) range = s.getRangeAt(0).cloneRange();
    };
    const saveNotes = () => later({ notes: editor.innerHTML });
    const exec = (cmd, arg) => {
      editor.focus();
      if (range) { const s = getSelection(); s.removeAllRanges(); s.addRange(range); }
      document.execCommand(cmd, false, arg);
      keepRange();
      saveNotes();
    };
    const tool = (label, title, fn, cls = "") => h("button", { class: `pm-tool ${cls}`, title, type: "button", onmousedown: (e) => e.preventDefault(), onclick: fn }, label);
    const block = selectOf([["p", tr("Thường")], ["h1", "H1"], ["h2", "H2"], ["h3", "H3"]], "p", (v) => exec("formatBlock", `<${v}>`), { class: "pm-block", "aria-label": tr("Kiểu đoạn") });
    const notesBar = h("div", { class: "pm-notes-bar" }, block,
      tool("B", tr("Đậm"), () => exec("bold"), "b"), tool("I", tr("Nghiêng"), () => exec("italic"), "i"),
      tool("U", tr("Gạch chân"), () => exec("underline"), "u"), tool("S", tr("Gạch ngang"), () => exec("strikeThrough"), "s"),
      tool("•", tr("Danh sách"), () => exec("insertUnorderedList")), tool("1.", tr("Danh sách đánh số"), () => exec("insertOrderedList")),
      tool("❝", tr("Trích dẫn"), () => exec("formatBlock", "<blockquote>")),
      tool("🔗", tr("Liên kết"), () => {
        const s = getSelection();
        if (!range || range.collapsed) { toast(tr("Chọn chữ cần gắn liên kết trước")); return; }
        const url = (prompt(tr("Địa chỉ liên kết (https://…)"), "https://") || "").trim();
        if (!url) return;
        if (!/^(https?:\/\/|mailto:)\S+$/i.test(url)) { toast(tr("Liên kết phải bắt đầu bằng http://, https:// hoặc mailto:")); return; }
        s.removeAllRanges();
        exec("createLink", url);
      }),
      tool("⌫", tr("Bỏ định dạng"), () => { exec("removeFormat"); exec("formatBlock", "<p>"); exec("unlink"); }));
    editor.addEventListener("input", saveNotes);
    editor.addEventListener("keyup", keepRange);
    editor.addEventListener("mouseup", keepRange);
    editor.addEventListener("focus", () => document.execCommand("defaultParagraphSeparator", false, "p"));
    editor.addEventListener("paste", (e) => {
      e.preventDefault();
      document.execCommand("insertText", false, (e.clipboardData || window.clipboardData).getData("text/plain"));
    });
    editor.addEventListener("click", (e) => {
      const a = e.target.closest("a[href]");
      if (a && (e.ctrlKey || e.metaKey)) window.open(a.href, "_blank", "noopener");
    });
    const notes = bind("notes", h("div", { class: "pm-notes" }, notesBar, editor), (t) => { editor.replaceChildren(noteNodes(t.notes)); });

    // checklist
    const track = bind("track_checklist", h("input", { type: "checkbox", onchange: () => now({ track_checklist: track.checked }) }), (t, el) => { el.checked = t.track_checklist; });
    let items = [];
    const listBox = h("div", { class: "pm-checklist" });
    const sendItems = (delay) => queue(id, { checklist: items.map((i) => ({ text: i.text, done: i.done })) }, delay);
    const drawItems = () => {
      put(listBox, items.map((it, i) => {
        const cb = h("input", { type: "checkbox", checked: it.done, onchange: () => { it.done = cb.checked; sendItems(0); } });
        const txt = h("input", { value: it.text, placeholder: tr("Việc cần làm"), oninput: () => { it.text = txt.value; sendItems(600); },
          onkeydown: (e) => { if (e.key === "Enter") { e.preventDefault(); addItem(i + 1); } } });
        return h("div", { class: "pm-item" }, cb, txt, h("button", { class: "ghost small", title: tr("Xoá mục"), onclick: () => { items.splice(i, 1); drawItems(); sendItems(0); } }, "✕"));
      }));
    };
    const addItem = (at = items.length) => {
      items.splice(at, 0, { text: "", done: false });
      drawItems();
      const inputs = listBox.querySelectorAll("input:not([type=checkbox])");
      if (inputs[at]) inputs[at].focus();
    };
    const checklist = bind("checklist", h("div", {}, listBox, h("button", { class: "small", onclick: () => addItem() }, tr("+ Mục"))), (t) => {
      items = t.checklist.map((i) => ({ text: i.text, done: !!i.done }));
      drawItems();
    });

    // links
    const linkList = h("div", { class: "pm-links-list" });
    const links = bind("links", h("textarea", { class: "pm-links-input", rows: "3", placeholder: "https://…", oninput: () => later({ links: links.value }) }), (t, el) => { el.value = t.links; });

    // the branch it belongs to
    const parent = bind("parent_id", h("select", { onchange: () => moveTask(id, { parent_id: parent.value ? +parent.value : null }) }), (t, el) => {
      const own = subtreeIds(t);
      const opts = [h("option", { value: "" }, tr("(gốc dự án)"))];
      const walk = (x) => {
        if (own.has(x.id)) return;
        opts.push(h("option", { value: x.id }, `${"   ".repeat(x.depth)}${x.icon ? x.icon + " " : ""}${x.title} · ${x.code}`));
        x.children.forEach((c) => walk(S.byId.get(c)));
      };
      S.tops.forEach(walk);
      put(el, opts);
      el.value = t.parent_id === null ? "" : String(t.parent_id);
    });

    // comments and files
    const extrasBtn = h("button", { class: "pm-extras-btn", onclick: () => toggleExtras() });
    const extras = h("div", { class: "pm-extras", hidden: true });
    const toggleExtras = () => { extras.hidden = !extras.hidden; if (!extras.hidden) loadExtras(); };
    const loadExtras = async () => {
      const [c, f] = await Promise.all([api("GET", `/api/pm/tasks/${id}/comments`, undefined, { keep: true }), api("GET", `/api/pm/tasks/${id}/files`, undefined, { keep: true })])
        .catch((e) => { toast(tr("Lỗi: ") + e.message); return [null, null]; });
      if (!c || !P || P.id !== id) return;
      const text = h("textarea", { class: "pm-comment-input", rows: "2", placeholder: tr("Viết bình luận…"),
        onkeydown: (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); send(); } } });
      const send = () => {
        if (!text.value.trim()) return;
        run(async () => { await api("POST", `/api/pm/tasks/${id}/comments`, { text: text.value }); await loadExtras(); loadBoard(); });
      };
      const upload = h("input", { type: "file", multiple: true, hidden: true, onchange: async () => {
        const files = [...upload.files];
        upload.value = "";
        for (const file of files) {
          if (file.size > f.max_size) { toast(tr("Tệp {0} quá lớn (tối đa {1} MB)", file.name, Math.round(f.max_size / 1048576))); continue; }
          await run(async () => {
            const r = await fetch(`/api/pm/tasks/${id}/files?name=${encodeURIComponent(file.name)}`, {
              method: "PUT", credentials: "same-origin", body: file,
              headers: { "X-Requested-With": "ai-employees", "Content-Type": file.type || "application/octet-stream" } });
            if (!r.ok) { let m = `HTTP ${r.status}`; try { m = (await r.json()).error || m; } catch (_) { /* not JSON */ } throw new Error(m); }
          }, tr("Đã tải lên {0}", file.name));
        }
        await loadExtras();
        loadBoard();
      } });
      const mayDelete = (who) => S.manager || (me && who === me.username);
      put(extras,
        h("h4", {}, tr("Bình luận")),
        c.comments.length ? c.comments.map((m) => h("div", { class: "pm-comment" },
          h("div", { class: "row spread" }, h("span", {}, h("b", {}, personName(m.author)), h("span", { class: "muted" }, ` · ${fmtTime(m.created)}`)),
            mayDelete(m.author) ? h("button", { class: "ghost small", title: tr("Xoá bình luận"), onclick: () => confirm(tr("Xoá bình luận này?")) &&
              run(async () => { await api("DELETE", `/api/pm/comments/${m.id}`); await loadExtras(); loadBoard(); }) }, "✕") : null),
          h("div", { class: "pre" }, m.text))) : h("p", { class: "muted" }, tr("Chưa có bình luận.")),
        text, h("div", { class: "row" }, h("button", { class: "primary small", onclick: send }, tr("Gửi")), h("span", { class: "muted" }, "Ctrl+Enter")),
        h("h4", {}, tr("Tệp")),
        f.files.length ? f.files.map((x) => h("div", { class: "pm-file" },
          h("a", { href: `/api/pm/files/${x.id}`, download: x.name }, `📎 ${x.name}`),
          h("span", { class: "muted" }, ` ${Math.max(1, Math.round(x.size / 1024))} KB · ${personName(x.author)} · ${fmtTime(x.created)}`),
          mayDelete(x.author) ? h("button", { class: "ghost small", title: tr("Xoá tệp"), onclick: () => confirm(tr("Xoá tệp {0}?", x.name)) &&
            run(async () => { await api("DELETE", `/api/pm/files/${x.id}`); await loadExtras(); loadBoard(); }) }, "✕") : null))
          : h("p", { class: "muted" }, tr("Chưa có tệp.")),
        h("button", { class: "small", onclick: () => upload.click() }, tr("Tải tệp lên")), upload);
    };

    const actions = h("div", { class: "pm-actions" },
      h("button", { onclick: () => addTask(id) }, tr("+ Nhánh con")),
      h("button", { onclick: () => addTask(task.parent_id, id) }, tr("+ Cùng cấp")),
      h("button", { onclick: () => moveTask(id, { direction: "up" }) }, tr("↑ Lên")),
      h("button", { onclick: () => moveTask(id, { direction: "down" }) }, tr("↓ Xuống")),
      h("button", { class: "danger", onclick: () => deleteTask(id) }, tr("Xoá")));

    const F = (label, el, cls = "") => h("label", { class: cls }, label, el);
    // several controls: not a <label> (a click on it would press the first one)
    const G = (label, el) => h("div", { class: "pm-field" }, h("div", { class: "pm-cap" }, label), el);
    self.el = h("div", { class: "pm-panel-in" }, head,
      h("div", { class: "pm-grid2 pm-title-row" }, F(tr("Tên công việc"), self.title, "pm-wide"), F(tr("Mã"), code)),
      G(tr("Biểu tượng"), icons), G(tr("Màu nền"), colors),
      h("div", { class: "pm-grid2" }, F(tr("Người phụ trách"), assignee), F(tr("Ưu tiên"), prio),
        F(tr("Trạng thái"), status), F(tr("% hoàn thành"), progress)),
      F(tr("Nhãn (phẩy)"), labels),
      G(tr("Ảnh đại diện của nốt"), avatar),
      F(tr("Phụ thuộc (mã các việc phải xong trước, phẩy)"), depends), waiting,
      h("div", { class: "pm-grid2" }, F(tr("Ngày bắt đầu"), start), F(tr("Ngày kết thúc"), due)),
      G(tr("Ghi chú"), notes),
      h("label", { class: "check" }, track, tr("Nốt cuối — theo dõi bằng danh sách nhiệm vụ")),
      G(tr("Checklist"), checklist),
      F(tr("Liên kết (mỗi dòng một link)"), links), linkList,
      F(tr("Thuộc nhánh"), parent),
      extrasBtn, extras, actions);

    // values worked out by the server (never typed here)
    const derived = (t) => {
      put(crumbs, [S.project ? S.project.name : "", ...t._path, t.title].map((x, i, a) => h("span", { class: i === a.length - 1 ? "cur" : "" }, x)));
      code.value = t.code;
      waiting.hidden = !t.waiting_for.length;
      waiting.textContent = t.waiting_for.length ? tr("Chờ: {0}", t.waiting_for.join(", ")) : "";
      put(linkList, t.links_list.map((u) => /^https?:\/\//i.test(u) ? h("a", { href: u, target: "_blank", rel: "noopener noreferrer" }, u) : null));
      extrasBtn.textContent = tr("💬 Bình luận ({0}) · 📎 Tệp ({1})", t.comment_count || 0, t.file_count || 0);
    };
    derived(t0);
    self.sync = (t) => {
      task = t;
      const act = document.activeElement;
      for (const b of binds) {
        if (b.el === act || b.el.contains(act)) continue;
        if (pending && pending.id === id && b.key in pending.data) continue;
        b.set(t, b.el);
      }
      derived(t);
    };
    return self;
  }
})();

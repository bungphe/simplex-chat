"use strict";
// The admin UI's language: I18N (the catalog, loaded by /i18n/catalog.js just before this
// file) turns the Vietnamese texts of the scripts into the chosen language.

function tr(text, ...values) {
  const out = (window.I18N && I18N.msgs[text]) || text;
  return values.length ? out.replace(/\{(\d+)\}/g, (m, i) => (values[+i] ?? "")) : out;
}

const LANG = (window.I18N && I18N.lang) || "vi";
// dates and numbers as the staff member reads them (4.500.000 in Vietnamese, 4,500,000 in English)
const LOCALE = { vi: "vi-VN", zh: "zh-CN", pt: "pt-BR" }[LANG] || LANG;
document.documentElement.lang = LANG;
if (window.I18N && I18N.rtl) document.documentElement.dir = "rtl";

// the texts written in admin.html itself
(function translatePage(root) {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const text = node.nodeValue.trim();
    if (text && tr(text) !== text) node.nodeValue = node.nodeValue.replace(text, tr(text));
  }
  for (const el of root.querySelectorAll("[placeholder], [title], [aria-label]")) {
    for (const attr of ["placeholder", "title", "aria-label"]) {
      const v = el.getAttribute(attr);
      if (v && tr(v) !== v) el.setAttribute(attr, tr(v));
    }
  }
  document.title = tr(document.title);
})(document.documentElement);

function setLanguage(code) {
  document.cookie = `ui_lang=${encodeURIComponent(code)}; path=/; max-age=31536000; samesite=lax`;
  location.reload();
}

// a language menu (login page, account page, and the shop's language settings)
function languageSelect(current, onChange) {
  const sel = document.createElement("select");
  sel.setAttribute("aria-label", tr("Ngôn ngữ"));
  for (const [code, name] of Object.entries((window.I18N && I18N.languages) || { vi: "Tiếng Việt" })) {
    const o = document.createElement("option");
    o.value = code;
    o.textContent = name;
    o.selected = code === current;
    sel.append(o);
  }
  sel.addEventListener("change", () => onChange(sel.value));
  return sel;
}

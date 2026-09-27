/**
 * Zalo personal-account gateway for the AI employees inbox.
 *
 * Adapted from m.agent's zalo_personal bridge (zca-js). One process serves several
 * Zalo accounts; each account id is the id of a `zalo_personal` channel in employees.yaml.
 *
 *   Zalo  --websocket-->  gateway  --POST {WEBHOOK_URL}/{account}, X-Hook-Secret-->  AI employees
 *   AI employees  --POST /{account}/api/send-message, X-Api-Key-->  gateway  -->  Zalo
 *
 * Differences from the original: every API needs the key (header only, constant-time
 * check), inbound webhooks carry a shared secret, no CORS, no HTML QR page and no
 * third-party QR image service (the login QR is a credential), account ids are
 * validated, session files are owner-only, and the sender's display name is forwarded.
 *
 * zca-js drives the Zalo web protocol and is not an official API: Zalo may restrict
 * accounts used this way. Prefer a Zalo Official Account (zalo_oa channel) for sales.
 */
import express from "express";
import { timingSafeEqual } from "node:crypto";
import { chmodSync, existsSync, mkdirSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";
import { LoginQRCallbackEventType, ThreadType, Zalo } from "zca-js";

const PORT = Number(process.env.PORT || 3000);
const HOST = process.env.HOST || "0.0.0.0";
const API_SECRET = process.env.API_SECRET || "";
const WEBHOOK_URL = (process.env.WEBHOOK_URL || "").replace(/\/+$/, "");
const WEBHOOK_SECRET = process.env.WEBHOOK_SECRET || "";
const SESSIONS_DIR = process.env.SESSIONS_DIR || "/app/sessions";
const ACCOUNT_ID = /^[a-z0-9][a-z0-9_-]{0,47}$/;

for (const [name, value] of [["API_SECRET", API_SECRET], ["WEBHOOK_SECRET", WEBHOOK_SECRET]]) {
  if (value.length < 16) {
    console.error(`${name} must be set (at least 16 characters)`);
    process.exit(1);
  }
}
if (!WEBHOOK_URL) {
  console.error("WEBHOOK_URL must be set, e.g. http://ai-office:8080/hooks");
  process.exit(1);
}
mkdirSync(SESSIONS_DIR, { recursive: true, mode: 0o700 });

// account id -> { api, state: idle|qr_pending|qr_scanned|connected|error, qr, busy }
const accounts = new Map();

function account(id) {
  if (!accounts.has(id)) accounts.set(id, { api: null, state: "idle", qr: null, busy: false });
  return accounts.get(id);
}

const sessionFile = (id) => path.join(SESSIONS_DIR, `${id}.json`);

function saveSession(id, credentials) {
  const file = sessionFile(id);
  writeFileSync(file, JSON.stringify(credentials), { mode: 0o600 });
  chmodSync(file, 0o600);
}

function loadSession(id) {
  try {
    return existsSync(sessionFile(id)) ? JSON.parse(readFileSync(sessionFile(id), "utf-8")) : null;
  } catch (e) {
    console.error(`[${id}] cannot read the saved session: ${e.message}`);
    return null;
  }
}

async function forward(id, data) {
  try {
    const r = await fetch(`${WEBHOOK_URL}/${id}`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Hook-Secret": WEBHOOK_SECRET },
      body: JSON.stringify({ event: "message", account: id, data }),
    });
    if (!r.ok) console.warn(`[${id}] webhook answered HTTP ${r.status}`);
  } catch (e) {
    console.error(`[${id}] webhook failed: ${e.message}`);
  }
}

function listen(id) {
  const acc = account(id);
  acc.api.listener.on("message", (m) => {
    const d = m.data || {};
    const c = d.content;
    // photos, files and link cards carry an object: keep its URL, thumbnail and title
    const attachment = c && typeof c === "object"
      ? { type: d.msgType || "", url: c.href || c.normalUrl || null, thumb: c.thumb || null, name: c.title || null }
      : null;
    forward(id, {
      id: String(d.msgId ?? m.msgId ?? ""),
      type: m.type === ThreadType.Group ? "group" : "user",
      threadId: String(m.threadId ?? ""),
      senderId: String(d.uidFrom ?? m.uidFrom ?? ""),
      senderName: d.dName || "",
      // text messages carry a string; stickers, photos and files an object
      content: typeof d.content === "string" ? d.content : null,
      contentType: d.msgType || (typeof d.content === "string" ? "text" : "other"),
      attachment,
      timestamp: Number(d.ts ?? m.serverTime ?? Date.now()),
      isSelf: Boolean(m.isSelf),
    });
  });
  acc.api.listener.on("error", (e) => console.error(`[${id}] socket error:`, e?.message || e));
  acc.api.listener.on("closed", () => {
    console.warn(`[${id}] socket closed; reconnecting in 5 s`);
    acc.api = null;
    acc.state = "idle";
    setTimeout(() => start(id).catch((e) => console.error(`[${id}] reconnect failed: ${e.message}`)), 5000);
  });
  acc.api.listener.start();
  console.log(`[${id}] connected; forwarding messages to ${WEBHOOK_URL}/${id}`);
}

async function start(id) {
  const acc = account(id);
  if (acc.busy || acc.state === "connected") return;
  acc.busy = true;
  try {
    // selfListen: messages the account sends from the phone reach the inbox too
    const zalo = new Zalo({ logging: false, selfListen: true });
    const saved = loadSession(id);
    if (saved) {
      try {
        acc.api = await zalo.login({ imei: saved.imei, cookie: saved.cookie, userAgent: saved.userAgent });
        acc.state = "connected";
        listen(id);
        return;
      } catch (e) {
        console.warn(`[${id}] saved session no longer valid (${e.message}); a new QR login is needed`);
      }
    }
    acc.state = "qr_pending";
    for (;;) {
      try {
        acc.api = await zalo.loginQR({}, ({ type, data, actions }) => {
          if (type === LoginQRCallbackEventType.QRCodeGenerated) {
            acc.qr = data?.image ? `data:image/png;base64,${data.image}` : null;
            acc.state = "qr_pending";
          } else if (type === LoginQRCallbackEventType.QRCodeExpired) {
            acc.qr = null;
            actions.retry();
          } else if (type === LoginQRCallbackEventType.QRCodeScanned) {
            acc.state = "qr_scanned";
          } else if (type === LoginQRCallbackEventType.QRCodeDeclined) {
            acc.qr = null;
            acc.state = "qr_pending";
            actions.abort();
          } else if (type === LoginQRCallbackEventType.GotLoginInfo) {
            saveSession(id, data);
          }
        });
        break;
      } catch (e) {
        console.warn(`[${id}] QR login did not complete (${e.message}); new QR in 2 s`);
        acc.qr = null;
        acc.state = "qr_pending";
        await new Promise((r) => setTimeout(r, 2000));
      }
    }
    acc.qr = null;
    acc.state = "connected";
    listen(id);
  } catch (e) {
    acc.state = "error";
    throw e;
  } finally {
    acc.busy = false;
  }
}

const app = express();
app.disable("x-powered-by");
app.use(express.json({ limit: "256kb" }));

app.get("/health", (_req, res) => {
  const states = {};
  for (const acc of accounts.values()) states[acc.state] = (states[acc.state] || 0) + 1;
  res.json({ status: "ok", accounts: states });
});

const key = Buffer.from(API_SECRET);
app.use("/:account/api", (req, res, next) => {
  const given = Buffer.from(String(req.get("x-api-key") || ""));
  if (given.length !== key.length || !timingSafeEqual(given, key)) return res.status(401).json({ error: "unauthorized" });
  if (!ACCOUNT_ID.test(req.params.account)) return res.status(400).json({ error: "bad account id" });
  next();
});

// Start the account: reuses the saved session, otherwise waits for a QR scan.
app.post("/:account/api/init", (req, res) => {
  const id = req.params.account;
  start(id).catch((e) => console.error(`[${id}] start failed: ${e.message}`));
  res.json({ state: account(id).state });
});

// Login state and, while waiting, the QR image (a data: URI) for the admin page.
app.get("/:account/api/qr", (req, res) => {
  const acc = account(req.params.account);
  res.json({ state: acc.state, qr: acc.state === "qr_pending" ? acc.qr : null });
});

app.post("/:account/api/send-message", async (req, res) => {
  const acc = account(req.params.account);
  if (acc.state !== "connected" || !acc.api) return res.status(503).json({ error: "account not connected" });
  const { threadId, message } = req.body || {};
  if (typeof threadId !== "string" || typeof message !== "string" || !threadId || !message) {
    return res.status(400).json({ error: "threadId and message are required" });
  }
  try {
    const r = await acc.api.sendMessage({ msg: message }, threadId, ThreadType.User);
    res.json({ ok: true, message_id: String(r?.message?.msgId ?? "") });
  } catch (e) {
    res.status(502).json({ error: e.message });
  }
});

// Forget the saved session (the next init shows a new QR).
app.post("/:account/api/logout", (req, res) => {
  const id = req.params.account;
  const acc = account(id);
  try {
    acc.api?.listener?.stop?.();
  } catch (_) {
    /* already closed */
  }
  accounts.delete(id);
  rmSync(sessionFile(id), { force: true });
  res.json({ ok: true });
});

app.listen(PORT, HOST, () => {
  console.log(`Zalo gateway on ${HOST}:${PORT}, webhooks to ${WEBHOOK_URL}/<account>`);
  for (const f of readdirSync(SESSIONS_DIR)) {
    const id = f.replace(/\.json$/, "");
    if (f.endsWith(".json") && ACCOUNT_ID.test(id)) start(id).catch((e) => console.error(`[${id}] ${e.message}`));
  }
});

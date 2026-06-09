const express = require("express");
const fs = require("fs");
const pino = require("pino");
const QRCode = require("qrcode");
const qrcode = require("qrcode-terminal");

const {
  DisconnectReason,
  fetchLatestBaileysVersion,
  useMultiFileAuthState,
} = require("@whiskeysockets/baileys");
const makeWASocket = require("@whiskeysockets/baileys").default;

const PORT = Number(process.env.PORT || 8080);
const AUTH_DIR = process.env.WA_AUTH_DIR || "./auth";
const API_TOKEN = process.env.WHATSAPP_GROUP_SENDER_TOKEN || "";
const ASSISTANT_API_URL = process.env.ASSISTANT_API_URL || process.env.WHATSAPP_INGEST_URL || "";

const app = express();
const log = pino({ level: process.env.LOG_LEVEL || "info" });

app.use(express.json({ limit: "256kb" }));

let sock = null;
let connected = false;
let lastQr = null;
let me = null;
let reconnecting = false;

function requireAuth(req, res, next) {
  if (!API_TOKEN) {
    return next();
  }
  const expected = `Bearer ${API_TOKEN}`;
  if (req.get("authorization") !== expected) {
    return res.status(401).json({ ok: false, error: "unauthorized" });
  }
  return next();
}

function normalizeGroupId(value) {
  const groupId = String(value || "").trim();
  if (!groupId.endsWith("@g.us")) {
    throw new Error("group_id must be a WhatsApp group JID ending in @g.us");
  }
  return groupId;
}

function extractMessageText(message) {
  const content = message?.message || {};
  return (
    content.conversation ||
    content.extendedTextMessage?.text ||
    content.imageMessage?.caption ||
    content.videoMessage?.caption ||
    content.documentMessage?.caption ||
    content.buttonsResponseMessage?.selectedDisplayText ||
    content.listResponseMessage?.title ||
    content.templateButtonReplyMessage?.selectedDisplayText ||
    null
  );
}

function normalizeEventResponse(response) {
  const raw = typeof response === "string" ? response.trim().toLowerCase() : response;
  const map = {
    0: "unknown",
    1: "going",
    2: "not_going",
    3: "maybe",
    going: "going",
    yes: "going",
    maybe: "maybe",
    not_going: "not_going",
    "not going": "not_going",
    no: "not_going",
  };
  return map[raw] || map[String(raw).trim().toLowerCase()] || null;
}

async function forwardInboundMessages(messages) {
  if (!ASSISTANT_API_URL) {
    return;
  }

  const normalized = [];
  for (const message of messages || []) {
    if (!message?.key || message.key.fromMe) {
      continue;
    }

    const groupId = String(message.key.remoteJid || "").trim();
    if (!groupId.endsWith("@g.us")) {
      continue;
    }

    const content = message.message || {};
    const eventResponse = content.eventResponseMessage || null;
    const text = eventResponse ? normalizeEventResponse(eventResponse.response) : extractMessageText(message);
    if (!eventResponse && !text) {
      continue;
    }

    const tsSeconds = Number(message.messageTimestamp || Math.floor(Date.now() / 1000));
    normalized.push({
      external_id: String(message.key.id || ""),
      from_external_id: String(message.key.participant || message.key.remoteJid || ""),
      display_name: message.pushName || null,
      message_type: eventResponse ? "event_response" : "text",
      text: text ? String(text) : null,
      received_at: new Date(Number.isFinite(tsSeconds) ? tsSeconds * 1000 : Date.now()).toISOString(),
      group_id: groupId,
      group_name: message.chatName || null,
      raw: {
        key: message.key,
        message: content,
        pushName: message.pushName || null,
        group_id: groupId,
        participant: message.key.participant || null,
        remote_jid: groupId,
        event_response: eventResponse
          ? {
              response: eventResponse.response,
              extra_guest_count: eventResponse.extraGuestCount ?? null,
            }
          : null,
      },
    });
  }

  if (!normalized.length) {
    return;
  }

  try {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 10000);
    const response = await fetch(`${ASSISTANT_API_URL.replace(/\/$/, "")}/internal/whatsapp/sidecar`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(API_TOKEN ? { Authorization: `Bearer ${API_TOKEN}` } : {}),
      },
      body: JSON.stringify({ messages: normalized }),
      signal: controller.signal,
    });
    clearTimeout(timer);

    if (!response.ok) {
      const body = await response.text();
      log.warn(
        {
          status: response.status,
          body: body.slice(0, 500),
          count: normalized.length,
        },
        "inbound sidecar forward failed"
      );
    }
  } catch (error) {
    log.warn({ error: error.message, count: normalized.length }, "inbound sidecar forward error");
  }
}

async function connectWhatsApp() {
  if (reconnecting) {
    return;
  }
  reconnecting = true;

  try {
    fs.mkdirSync(AUTH_DIR, { recursive: true });
    const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
    const { version } = await fetchLatestBaileysVersion();

    sock = makeWASocket({
      auth: state,
      browser: ["PI Assistant", "Chrome", "1.0.0"],
      logger: log.child({ component: "baileys" }),
      printQRInTerminal: false,
      version,
    });

    sock.ev.on("creds.update", saveCreds);

    sock.ev.on("connection.update", (update) => {
      const { connection, lastDisconnect, qr } = update;

      if (qr) {
        lastQr = qr;
        connected = false;
        log.info("Scan this WhatsApp login QR:");
        qrcode.generate(qr, { small: true });
      }

      if (connection === "open") {
        connected = true;
        lastQr = null;
        me = sock.user || null;
        log.info({ user: me }, "whatsapp connected");
      }

      if (connection === "close") {
        connected = false;
        const statusCode = lastDisconnect?.error?.output?.statusCode;
        log.warn({ statusCode }, "whatsapp connection closed");
        if (statusCode !== DisconnectReason.loggedOut) {
          setTimeout(connectWhatsApp, 3000);
        } else {
          log.error("whatsapp logged out; delete auth state and scan a new QR");
        }
      }
    });

    sock.ev.on("messages.upsert", ({ messages }) => {
      void forwardInboundMessages(messages);
    });

    sock.ev.on("messaging-history.set", ({ messages }) => {
      void forwardInboundMessages(messages);
    });
  } catch (error) {
    log.error({ error: error.message }, "whatsapp connect failed");
    setTimeout(connectWhatsApp, 5000);
  } finally {
    reconnecting = false;
  }
}

app.get("/healthz", (_req, res) => {
  res.json({
    ok: true,
    connected,
    has_qr: Boolean(lastQr),
    user: me,
    assistant_api_configured: Boolean(ASSISTANT_API_URL),
  });
});

app.get("/", async (_req, res) => {
  const qrHtml = await renderQrHtml();
  res.setHeader("Content-Type", "text/html; charset=utf-8");
  res.send(qrHtml);
});

app.get("/qr", requireAuth, (_req, res) => {
  if (connected) {
    return res.json({ ok: true, connected: true, qr: null });
  }
  if (!lastQr) {
    return res.status(404).json({ ok: false, error: "qr_not_ready" });
  }
  return res.json({ ok: true, connected: false, qr: lastQr });
});

app.get("/qr.svg", async (_req, res) => {
  if (connected) {
    return res.status(404).send("WhatsApp already connected");
  }
  if (!lastQr) {
    return res.status(404).send("QR not ready");
  }

  const svg = await QRCode.toString(lastQr, {
    type: "svg",
    margin: 1,
    width: 320,
    errorCorrectionLevel: "M",
  });
  res.setHeader("Content-Type", "image/svg+xml; charset=utf-8");
  res.send(svg);
});

app.get("/groups", requireAuth, async (_req, res) => {
  if (!connected || !sock) {
    return res.status(503).json({ ok: false, error: "whatsapp_not_connected" });
  }

  try {
    const groups = await sock.groupFetchAllParticipating();
    const result = Object.values(groups).map((group) => ({
      id: group.id,
      subject: group.subject,
      participants: group.participants?.length || 0,
    }));
    return res.json({ ok: true, groups: result });
  } catch (error) {
    log.error({ error: error.message }, "group list failed");
    return res.status(500).json({ ok: false, error: error.message });
  }
});

async function sendGroupMessage(req, res) {
  if (!connected || !sock) {
    return res.status(503).json({ ok: false, error: "whatsapp_not_connected" });
  }

  try {
    const groupId = normalizeGroupId(req.body.group_id || req.body.to);
    const text = String(req.body.text || "").trim();
    if (!text) {
      return res.status(400).json({ ok: false, error: "text is required" });
    }

    const response = await sock.sendMessage(groupId, { text });
    const messageId = response?.key?.id || "";
    log.info({ groupId, messageId }, "group message sent");
    return res.json({ ok: true, message_id: messageId });
  } catch (error) {
    log.error({ error: error.message }, "group send failed");
    return res.status(400).json({ ok: false, error: error.message });
  }
}

app.post("/", requireAuth, sendGroupMessage);
app.post("/send", requireAuth, sendGroupMessage);

app.listen(PORT, () => {
  log.info({ port: PORT, authDir: AUTH_DIR }, "whatsapp group sender listening");
  if (!ASSISTANT_API_URL) {
    log.warn("ASSISTANT_API_URL is not set; inbound group RSVP syncing is disabled");
  }
  connectWhatsApp();
});

async function renderQrHtml() {
  const status = connected
    ? "Connected"
    : lastQr
      ? "Ready to scan"
      : "Waiting for WhatsApp QR";

  const qrImage = connected
    ? "<p>WhatsApp is already connected.</p>"
    : lastQr
      ? '<img alt="WhatsApp QR" src="/qr.svg" style="width:320px;height:320px;image-rendering:pixelated;border:1px solid #ddd;background:#fff;padding:12px" />'
      : "<p>QR is not ready yet. Refresh in a few seconds.</p>";

  return `<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>WhatsApp Group Sender</title>
    <style>
      body { font-family: system-ui, sans-serif; margin: 32px; color: #111; background: #fafafa; }
      .card { max-width: 760px; margin: 0 auto; padding: 24px; background: #fff; border: 1px solid #ddd; border-radius: 12px; }
      .status { font-weight: 700; margin-bottom: 16px; }
      .meta { color: #555; line-height: 1.5; }
      code { background: #f4f4f4; padding: 2px 6px; border-radius: 6px; }
    </style>
  </head>
  <body>
    <div class="card">
      <div class="status">Status: ${status}</div>
      ${qrImage}
      <p class="meta">
        Open this page in a browser on a laptop or another screen, then scan it with WhatsApp on your phone.
        Once connected, the sidecar can send to any group ID it knows about.
      </p>
    </div>
  </body>
</html>`;
}

const express = require("express");
const pino = require("pino");
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

async function connectWhatsApp() {
  if (reconnecting) {
    return;
  }
  reconnecting = true;

  try {
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
  });
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
  connectWhatsApp();
});

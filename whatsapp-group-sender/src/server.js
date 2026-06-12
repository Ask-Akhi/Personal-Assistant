const express = require("express");
const fs = require("fs");
const pino = require("pino");
const { Pool } = require("pg");
const QRCode = require("qrcode");
const qrcode = require("qrcode-terminal");

const {
  BufferJSON,
  DisconnectReason,
  fetchLatestBaileysVersion,
  initAuthCreds,
  makeCacheableSignalKeyStore,
  proto,
  useMultiFileAuthState,
} = require("@whiskeysockets/baileys");
const makeWASocket = require("@whiskeysockets/baileys").default;

const PORT = Number(process.env.PORT || 8080);
const AUTH_DIR = process.env.WA_AUTH_DIR || "./auth";
const API_TOKEN = process.env.WHATSAPP_GROUP_SENDER_TOKEN || "";
const ASSISTANT_API_URL = process.env.ASSISTANT_API_URL || process.env.WHATSAPP_INGEST_URL || "";
const DATABASE_URL = process.env.DATABASE_URL || "";
const SYNC_FULL_HISTORY = String(process.env.WA_SYNC_FULL_HISTORY || "true").toLowerCase() !== "false";

const app = express();
const log = pino({ level: process.env.LOG_LEVEL || "info" });

app.use(express.json({ limit: "256kb" }));

let sock = null;
let connected = false;
let lastQr = null;
let me = null;
let reconnecting = false;
let reconnectAttempt = 0;
let authBackend = DATABASE_URL ? "postgres" : "filesystem";
let authPool = null;
let forwardStats = {
  seenMessages: 0,
  skippedNoKey: 0,
  skippedFromMe: 0,
  skippedNonGroup: 0,
  skippedNoContent: 0,
  skippedNonPerson: 0,
  attempts: 0,
  successes: 0,
  failures: 0,
  lastStatus: null,
  lastError: null,
  lastSeenAt: null,
  lastForwardedAt: null,
  lastForwardedCount: 0,
};
let recentGroupMessages = [];

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

function isRealParticipantId(value) {
  const jid = String(value || "").trim();
  if (!jid) {
    return false;
  }
  if (jid.endsWith("@g.us")) {
    return false;
  }
  if (jid.endsWith("@broadcast")) {
    return false;
  }
  return true;
}

function addParticipantAlias(aliases, value) {
  const jid = String(value || "").trim();
  if (!jid) {
    return;
  }
  if (isRealParticipantId(jid)) {
    aliases.add(jid);
  }

  const barePhone = jid.replace(/\D/g, "");
  if (barePhone.length >= 8 && jid === barePhone) {
    aliases.add(`${barePhone}@s.whatsapp.net`);
  }
}

function participantAliases(source) {
  const aliases = new Set();
  const explicitFields = [
    "id",
    "jid",
    "lid",
    "participant",
    "participantPn",
    "participantLid",
    "remoteJidAlt",
    "senderPn",
    "senderLid",
  ];

  for (const field of explicitFields) {
    addParticipantAlias(aliases, source?.[field]);
  }

  for (const value of Object.values(source || {})) {
    if (typeof value === "string" && value.includes("@")) {
      addParticipantAlias(aliases, value);
    }
  }

  return [...aliases];
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

function rememberGroupMessage(message, groupId) {
  const content = message?.message || {};
  const eventMsg = content.eventMessage || content.eventCreationMessage || null;
  recentGroupMessages.push({
    at: new Date().toISOString(),
    group_id: groupId,
    message_id: message?.key?.id || null,
    from_me: Boolean(message?.key?.fromMe),
    participant: message?.key?.participant || message?.key?.participantPn || message?.key?.participantLid || null,
    top_level_keys: Object.keys(message || {}),
    message_keys: Object.keys(content),
    event_responses: Array.isArray(message?.eventResponses) ? message.eventResponses.length : 0,
    has_event_response_message: Boolean(content.eventResponseMessage),
    has_event_message: Boolean(eventMsg),
    embedded_rsvp_count: Array.isArray(eventMsg?.eventResponseMessages) ? eventMsg.eventResponseMessages.length : 0,
    stub_type: message?.messageStubType || null,
    stub_parameters: message?.messageStubParameters || null,
  });
  recentGroupMessages = recentGroupMessages.slice(-20);
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
    unknown: "unknown",
    GOING: "going",
    NOT_GOING: "not_going",
    MAYBE: "maybe",
    UNKNOWN: "unknown",
    EVENT_RESPONSE_TYPE_GOING: "going",
    EVENT_RESPONSE_TYPE_NOT_GOING: "not_going",
    EVENT_RESPONSE_TYPE_MAYBE: "maybe",
  };
  return map[raw] || map[String(raw).trim().toLowerCase()] || null;
}

function isBareRsvpText(text) {
  return /^(yes|y|no|n|wnbo|w|maybe|m)$/i.test(String(text || "").trim());
}

function responseParticipant(response, fallbackGroupId) {
  const key = response?.eventResponseMessageKey || response?.key || {};
  const participant = (
    key.participant ||
    key.participantPn ||
    key.participantLid ||
    response?.participant ||
    response?.participantPn ||
    response?.participantLid ||
    response?.senderPn ||
    response?.senderLid ||
    ""
  );
  return String(participant).trim();
}

function responseTimestamp(response, fallbackSeconds) {
  const raw = response?.timestampMs || response?.eventResponseMessage?.timestampMs || fallbackSeconds * 1000;
  const n = Number(raw);
  if (Number.isFinite(n)) {
    return n > 100000000000 ? n : n * 1000;
  }
  return Date.now();
}

function eventResponseExternalId(response, eventMessageId, participant, responseText, responseTimestampMs) {
  const key = response?.eventResponseMessageKey || response?.key || {};
  const responseKeyId = String(key.id || "").trim();
  const eventId = String(eventMessageId || responseKeyId || "event").trim();
  const actor = String(participant || "unknown").trim();
  const status = String(responseText || "unknown").trim();
  const timestamp = String(responseTimestampMs || "").trim();
  return `event-response:${eventId}:${actor}:${status}:${timestamp}`;
}

async function forwardInboundMessages(messages) {
  if (!ASSISTANT_API_URL) {
    return;
  }

  const normalized = [];
  for (const message of messages || []) {
    forwardStats.seenMessages += 1;
    forwardStats.lastSeenAt = new Date().toISOString();

    if (!message?.key) {
      forwardStats.skippedNoKey += 1;
      continue;
    }

    const groupId = String(message.key.remoteJid || "").trim();
    if (!groupId.endsWith("@g.us")) {
      forwardStats.skippedNonGroup += 1;
      continue;
    }
    rememberGroupMessage(message, groupId);

    const content = message.message || {};
    const eventResponse = content.eventResponseMessage || null;
    const text = eventResponse ? normalizeEventResponse(eventResponse.response) : extractMessageText(message);
    const hasEventResponses = Array.isArray(message.eventResponses) && message.eventResponses.length > 0;
    if (message.key.fromMe && !hasEventResponses && !isBareRsvpText(text)) {
      forwardStats.skippedFromMe += 1;
      continue;
    }
    if (Array.isArray(message.eventResponses)) {
      for (const response of message.eventResponses) {
        const responseMessage = response?.eventResponseMessage || {};
        const responseKey = response?.eventResponseMessageKey || response?.key || {};
        const participant = responseParticipant(response, groupId);
        const aliases = participantAliases(responseKey);
        const responseText = normalizeEventResponse(responseMessage.response);
        const timestampMs = responseTimestamp(response, Number(message.messageTimestamp || Math.floor(Date.now() / 1000)));
        if ((!isRealParticipantId(participant) && !aliases.length) || !responseText || responseText === "unknown") {
          forwardStats.skippedNonPerson += 1;
          continue;
        }

        normalized.push({
          external_id: eventResponseExternalId(response, message.key.id, participant || aliases[0], responseText, timestampMs),
          from_external_id: participant || aliases[0],
          participant_aliases: aliases,
          display_name: null,
          message_type: "event_response",
          text: responseText,
          received_at: new Date(timestampMs).toISOString(),
          group_id: groupId,
          group_name: message.chatName || null,
          event_response: {
            response: responseMessage.response,
            normalized_response: responseText,
            extra_guest_count: responseMessage.extraGuestCount ?? null,
            event_message_id: message.key.id || null,
          },
          raw: {
            key: responseKey || null,
            event_key: message.key,
            event_response: responseMessage,
            event_message_id: message.key.id || null,
            group_id: groupId,
            participant: participant || aliases[0] || null,
            participant_aliases: aliases,
            remote_jid: groupId,
          },
        });
      }
    }

    // Handle RSVPs embedded in the event creation message itself.
    // These appear when Baileys replays history (messaging-history.set) or
    // when messages.update fires for the event card after votes come in.
    const embeddedEventMsg = content.eventMessage || content.eventCreationMessage || null;
    if (embeddedEventMsg) {
      const embeddedResponses = Array.isArray(embeddedEventMsg.eventResponseMessages)
        ? embeddedEventMsg.eventResponseMessages
        : [];
      const msgTs = Number(message.messageTimestamp || Math.floor(Date.now() / 1000));
      const msgTsMs = msgTs > 100000000000 ? msgTs : msgTs * 1000;
      for (const resp of embeddedResponses) {
        const participant = String(resp.participant || resp.participantPn || resp.participantLid || "").trim();
        if (!isRealParticipantId(participant)) continue;
        const responseText = normalizeEventResponse(resp.response);
        if (!responseText || responseText === "unknown") continue;
        normalized.push({
          external_id: `event-emb:${message.key.id}:${participant}:${responseText}`,
          from_external_id: participant,
          participant_aliases: participantAliases({ participant }),
          display_name: null,
          message_type: "event_response",
          text: responseText,
          received_at: new Date(msgTsMs).toISOString(),
          group_id: groupId,
          group_name: message.chatName || null,
          event_response: {
            response: resp.response,
            normalized_response: responseText,
            extra_guest_count: resp.extraGuestCount ?? null,
            event_message_id: message.key.id || null,
          },
          raw: {
            key: message.key,
            event_message_id: message.key.id || null,
            group_id: groupId,
            participant,
            remote_jid: groupId,
            event_response: { response: resp.response, normalized_response: responseText },
          },
        });
      }
    }

    // Only count as no_content if nothing was added from eventResponses/embeddedEventMsg either
    if (!eventResponse && !text) {
      const hadEmbedded = normalized.length > 0;
      if (!hadEmbedded) {
        forwardStats.skippedNoContent += 1;
      }
      continue;
    }

    const tsSeconds = Number(message.messageTimestamp || Math.floor(Date.now() / 1000));
    const aliases = participantAliases({
      ...message.key,
      id: message.key.participant || message.key.participantPn || message.key.participantLid || me?.id || null,
      jid: me?.id || null,
    });
    normalized.push({
      external_id: String(message.key.id || ""),
      from_external_id: String(message.key.participant || message.key.participantPn || message.key.participantLid || aliases[0] || me?.id || ""),
      participant_aliases: aliases,
      display_name: message.pushName || null,
      message_type: eventResponse ? "event_response" : "text",
      text: text ? String(text) : null,
      received_at: new Date(Number.isFinite(tsSeconds) ? tsSeconds * 1000 : Date.now()).toISOString(),
      group_id: groupId,
      group_name: message.chatName || null,
      event_response: eventResponse
        ? {
            response: eventResponse.response,
            normalized_response: text,
            extra_guest_count: eventResponse.extraGuestCount ?? null,
          }
        : null,
      raw: {
        key: message.key,
        message: content,
        pushName: message.pushName || null,
        group_id: groupId,
        participant: message.key.participant || null,
        participant_aliases: aliases,
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
    forwardStats.attempts += 1;
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
    forwardStats.lastStatus = response.status;
    forwardStats.lastForwardedCount = normalized.length;
    forwardStats.lastForwardedAt = new Date().toISOString();

    if (!response.ok) {
      const body = await response.text();
      forwardStats.failures += 1;
      forwardStats.lastError = body.slice(0, 500);
      log.warn(
        {
          status: response.status,
          body: body.slice(0, 500),
          count: normalized.length,
        },
        "inbound sidecar forward failed"
      );
    } else {
      forwardStats.successes += 1;
      forwardStats.lastError = null;
      log.info({ count: normalized.length }, "inbound sidecar forward succeeded");
    }
  } catch (error) {
    forwardStats.failures += 1;
    forwardStats.lastError = error.message;
    log.warn({ error: error.message, count: normalized.length }, "inbound sidecar forward error");
  }
}

async function connectWhatsApp() {
  if (reconnecting) {
    return;
  }
  reconnecting = true;

  try {
    const { state, saveCreds } = DATABASE_URL
      ? await usePostgresAuthState(DATABASE_URL)
      : await useFilesystemAuthState(AUTH_DIR);
    const { version } = await fetchLatestBaileysVersion();

    sock = makeWASocket({
      auth: state,
      browser: ["Windows", "Chrome", "10.0"],
      logger: log.child({ component: "baileys" }),
      printQRInTerminal: false,
      syncFullHistory: SYNC_FULL_HISTORY,
      shouldSyncHistoryMessage: () => true,
      version,
      // Render free tier wakes slowly — give WA handshake enough time
      connectTimeoutMs: 60_000,
      defaultQueryTimeoutMs: 60_000,
      keepAliveIntervalMs: 25_000,
      retryRequestDelayMs: 2_000,
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

      if (connection === "open") {
        reconnectAttempt = 0;  // reset on successful connect
      }

      if (connection === "close") {
        connected = false;
        const statusCode = lastDisconnect?.error?.output?.statusCode;
        log.warn({ statusCode, attempt: reconnectAttempt }, "whatsapp connection closed");
        if (statusCode !== DisconnectReason.loggedOut) {
          // Exponential backoff: 3s, 6s, 12s, 24s … capped at 60s
          const delay = Math.min(3000 * Math.pow(2, reconnectAttempt), 60_000);
          reconnectAttempt += 1;
          log.info({ delay_ms: delay, attempt: reconnectAttempt }, "scheduling reconnect");
          setTimeout(connectWhatsApp, delay);
        } else {
          log.error("whatsapp logged out; delete auth state and scan a new QR");
        }
      }
    });

    sock.ev.on("messages.upsert", ({ messages }) => {
      void forwardInboundMessages(messages);
    });

    sock.ev.on("messages.update", (updates) => {
      // Skip updates that carry only delivery/read status — no RSVP or content data.
      // Passing these into forwardInboundMessages produces misleading seenMessages/noContent noise.
      const meaningful = (updates || []).filter((item) => {
        const up = item.update || {};
        return up.message || up.eventResponses || up.messageStubType || up.pollUpdates;
      });
      if (!meaningful.length) return;
      const messages = meaningful.map((item) => ({
        key: item.key,
        ...(item.update || {}),
      }));
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
    auth_backend: authBackend,
    sync_full_history: SYNC_FULL_HISTORY,
    forward_stats: forwardStats,
    recent_group_messages: recentGroupMessages,
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
    const result = [];

    for (const group of Object.values(groups)) {
      const isCommunity = Boolean(group.isCommunity);
      result.push({
        id: group.id,
        subject: group.subject,
        participants: group.participants?.length || 0,
        is_community: isCommunity,
      });

      if (!isCommunity) continue;

      // For community containers, explicitly fetch full metadata to get sub-group list.
      // groupFetchAllParticipating() does NOT populate group.groups — we must call
      // groupMetadata() separately to get the groups[] array.
      let communityMeta = null;
      try {
        communityMeta = await sock.groupMetadata(group.id);
      } catch (_e) {
        log.warn({ community: group.id }, "could not fetch community metadata");
      }

      const subGroupJids = Array.isArray(communityMeta?.groups)
        ? communityMeta.groups
        : (Array.isArray(group.groups) ? group.groups : []);

      for (const subJid of subGroupJids) {
        if (!String(subJid).endsWith("@g.us")) continue;
        // Skip if account is already in this sub-group (it appears in the main list)
        if (groups[subJid]) continue;
        try {
          const meta = await sock.groupMetadata(subJid);
          result.push({
            id: subJid,
            subject: `↳ ${meta.subject || subJid}`,
            participants: meta.participants?.length || 0,
            is_community: false,
            not_member: true,
            community_jid: group.id,
            community_subject: group.subject,
          });
        } catch (_e) {
          result.push({
            id: subJid,
            subject: `↳ sub-group (${subJid})`,
            participants: 0,
            is_community: false,
            not_member: true,
            community_jid: group.id,
            community_subject: group.subject,
          });
        }
      }
    }

    return res.json({ ok: true, groups: result });
  } catch (error) {
    log.error({ error: error.message }, "group list failed");
    return res.status(500).json({ ok: false, error: error.message });
  }
});

app.get("/participants", requireAuth, async (req, res) => {
  if (!connected || !sock) {
    return res.status(503).json({ ok: false, error: "whatsapp_not_connected" });
  }

  const groupId = String(req.query.group_id || "").trim();
  if (!groupId.endsWith("@g.us")) {
    return res.status(400).json({ ok: false, error: "group_id must end with @g.us" });
  }

  try {
    let group = await sock.groupMetadata(groupId);
    if (!group) {
      const groups = await sock.groupFetchAllParticipating();
      group = groups[groupId];
      if (!group) {
        return res.status(404).json({ ok: false, error: "group_not_found" });
      }
    }

    const participants = (group.participants || []).map((participant) => ({
      id: participant.id || participant.jid || null,
      aliases: participantAliases(participant),
      admin: participant.admin || null,
    }));

    return res.json({
      ok: true,
      group_id: groupId,
      subject: group.subject || null,
      participants,
    });
  } catch (error) {
    log.error({ error: error.message, groupId }, "group participant list failed");
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
  log.info({ port: PORT, authDir: AUTH_DIR, authBackend }, "whatsapp group sender listening");
  if (!ASSISTANT_API_URL) {
    log.warn("ASSISTANT_API_URL is not set; inbound group RSVP syncing is disabled");
  }
  connectWhatsApp();
});

async function useFilesystemAuthState(folder) {
  fs.mkdirSync(folder, { recursive: true });
  authBackend = "filesystem";
  return useMultiFileAuthState(folder);
}

async function usePostgresAuthState(connectionString) {
  authPool =
    authPool ||
    new Pool({
      connectionString,
    });

  await authPool.query(`
    create table if not exists wa_auth_state (
      key text primary key,
      value jsonb not null,
      updated_at timestamptz not null default now()
    )
  `);

  const writeData = async (data, key) => {
    const payload = JSON.stringify(data, BufferJSON.replacer);
    await authPool.query(
      `
      insert into wa_auth_state (key, value, updated_at)
      values ($1, $2::jsonb, now())
      on conflict (key)
      do update set value = excluded.value, updated_at = now()
      `,
      [key, payload]
    );
  };

  const readData = async (key) => {
    const result = await authPool.query(
      "select value from wa_auth_state where key = $1",
      [key]
    );
    if (!result.rowCount) {
      return null;
    }
    return JSON.parse(JSON.stringify(result.rows[0].value), BufferJSON.reviver);
  };

  const removeData = async (key) => {
    await authPool.query("delete from wa_auth_state where key = $1", [key]);
  };

  const creds = (await readData("creds.json")) || initAuthCreds();
  authBackend = "postgres";

  const keyStore = {
    get: async (type, ids) => {
      const data = {};
      await Promise.all(
        ids.map(async (id) => {
          let value = await readData(`${type}-${id}.json`);
          if (type === "app-state-sync-key" && value) {
            value = proto.Message.AppStateSyncKeyData.fromObject(value);
          }
          data[id] = value;
        })
      );
      return data;
    },
    set: async (data) => {
      const tasks = [];
      for (const category in data) {
        for (const id in data[category]) {
          const value = data[category][id];
          const key = `${category}-${id}.json`;
          tasks.push(value ? writeData(value, key) : removeData(key));
        }
      }
      await Promise.all(tasks);
    },
  };

  return {
    state: {
      creds,
      keys: makeCacheableSignalKeyStore(keyStore, log.child({ component: "auth-store" })),
    },
    saveCreds: async () => writeData(creds, "creds.json"),
  };
}

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

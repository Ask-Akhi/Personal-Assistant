# WhatsApp Group Sender

This sidecar sends messages to WhatsApp groups using a WhatsApp Web session.
It is group-agnostic: once the connected WhatsApp account belongs to a group,
the service can send to that group's `@g.us` ID.

## Local Run

```pwsh
cd whatsapp-group-sender
npm install
$env:WHATSAPP_GROUP_SENDER_TOKEN="choose-a-long-random-secret"
npm start
```

Scan the QR code printed in the terminal with the WhatsApp account that belongs
to your target groups.

Useful endpoints:

- `GET /healthz` checks whether WhatsApp is connected.
- `GET /qr` returns the current QR string if one is waiting to be scanned.
- `GET /groups` lists groups visible to the logged-in WhatsApp account.
- `POST /send` sends `{ "group_id": "120...@g.us", "text": "message" }`.

If `WHATSAPP_GROUP_SENDER_TOKEN` is set, call protected endpoints with:

```text
Authorization: Bearer <WHATSAPP_GROUP_SENDER_TOKEN>
```

## Render Wiring

Deploy this folder as a separate Node web service. Persist `WA_AUTH_DIR` with a
disk if your host supports it; otherwise you will need to scan a new QR after
service restarts.

Set these env vars on the Python assistant service:

```text
WHATSAPP_GROUP_SENDER_URL=https://<group-sender-service>/send
WHATSAPP_GROUP_SENDER_TOKEN=<same-secret-as-sidecar>
```

The Python app will automatically route any `@g.us` target through this sidecar.
Phone-number targets still use WhatsApp Cloud API.

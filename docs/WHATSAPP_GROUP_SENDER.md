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

- `GET /` shows a browser-friendly QR page.
- `GET /healthz` checks whether WhatsApp is connected.
- `GET /qr` returns the current QR string if one is waiting to be scanned.
- `GET /qr.svg` returns the QR as an SVG image.
- `GET /groups` lists groups visible to the logged-in WhatsApp account.
- `POST /send` sends `{ "group_id": "120...@g.us", "text": "message" }`.

If `WHATSAPP_GROUP_SENDER_TOKEN` is set, call protected endpoints with:

```text
Authorization: Bearer <WHATSAPP_GROUP_SENDER_TOKEN>
```

## Render Wiring

Deploy this folder as a separate Node web service. The sidecar now prefers
database-backed auth when `DATABASE_URL` is set, which means the WhatsApp linked
device session can survive restarts and free-instance spin-down without needing
to re-scan the QR on every deploy.

The blueprint config uses:

```text
plan: free
DATABASE_URL=<assistant-db connection string>
```

After the first successful deploy, open the sidecar root URL and scan the QR
once. That login state is then written to Postgres and should survive normal
restarts and deploys. You may still need to scan again if WhatsApp logs the
device out or you remove the linked device from your phone.

Set these env vars on the Python assistant service:

```text
WHATSAPP_GROUP_SENDER_URL=https://<group-sender-service>/send
WHATSAPP_GROUP_SENDER_TOKEN=<same-secret-as-sidecar>
```

Set this env var on the WhatsApp sidecar so it can forward group RSVP
responses back into the Python app:

```text
ASSISTANT_API_URL=https://<assistant-api-service>
```

The Python app will automatically route any `@g.us` target through this sidecar.
Phone-number targets still use WhatsApp Cloud API.

The Python `assistant-api` service also needs to be always-on for Tuesday 7 PM
and Wednesday 9 PM automation, because its scheduler runs inside the web
process. The blueprint sets `assistant-api` to `plan: starter` for that reason.

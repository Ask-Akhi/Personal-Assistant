# Personal AI Assistant — Phase 1 (Observability)

Trusted drafting + approval assistant. Built phase-by-phase: observe → draft → approve → (eventually) auto-send.

## What This Ships
- FastAPI gateway with `/healthz` and webhook stubs
- WhatsApp webhook replay safety scaffold:
  - inbound idempotency claim by event/message id
  - duplicate webhook detection
  - explicit 24-hour service-window helper for later outbound rules
- WhatsApp Cloud API observability:
  - webhook verification handshake
  - optional Meta `X-Hub-Signature-256` validation via `WHATSAPP_APP_SECRET`
  - normalized contact + inbound message storage
  - read-only Telegram mirror for inbound messages
- Telegram bot (long-polling) with:
  - Strict `user_id` allowlist
  - PIN-gated destructive commands
  - Session timeout
  - `/ping`, `/whoami`, `/audit`, `/inbox`, `/note`, `/notes`, `/memory`
- Structured memory (SQLAlchemy + Postgres / SQLite for local)
- Audit log for every action
- Cost-cap guard (monthly spend ceiling per provider)
- 30-second "undo" outbox primitive (used in later phases)
- Render `render.yaml` blueprint
- Pure-function `policy_engine` skeleton (unit-testable)

## Roadmap (revised)
0. **Command Center** ✅
1. **Observability** ← you are here
2. Drafting (Claude + approve/edit/regen)
3. Language + commitments (Hinglish, tone profiles, reminders)
4. Shadow eval (drafts in background, compare to your real replies)
5. Controlled auto-send (whitelist classes, quiet hours, 30s undo, kill switch)
6. Voice-in (gpt-4o-transcribe)
7. Baileys sidecar (isolated)
8. Voice-out (optional, ElevenLabs PVC)

## Local dev (Windows / pwsh)
```pwsh
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env   # then fill in values
python -m app.main
```

In a second terminal:
```pwsh
.\.venv\Scripts\Activate.ps1
python -m app.bot
```

## Key env vars
See `.env.example`. Required for Phase 1:
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_ALLOWED_USER_IDS` (comma-separated numeric IDs)
- `ADMIN_PIN` (6+ digits)
- `DATABASE_URL` (defaults to local SQLite)
- `UNDO_WINDOW_SECONDS` (defaults to `30`)
- `WHATSAPP_VERIFY_TOKEN` (required when connecting Meta webhook)
- `WHATSAPP_APP_SECRET` (recommended; enables request signature validation)
- `EVENT_DEFAULT_CUTOFF_HOURS` (defaults to `26`)
- `WHATSAPP_GROUP_SENDER_URL` (required for automatic posting to WhatsApp groups)
- `WHATSAPP_GROUP_SENDER_TOKEN` (optional bearer token for the group sender)

## Security posture
- Telegram allowlist is by **numeric user_id**, not @username.
- Destructive commands require fresh PIN auth (configurable TTL).
- All sensitive actions emit an `AuditLog` row with reasons.
- Secrets never logged. `.env` is gitignored.
- WhatsApp retries are deduplicated before later phases enqueue work.
- Time/date/money messages stay in human-review flow via the policy engine.

## Phase 1 notes
- Set Meta webhook callback URL to `https://<your-render-app>/webhooks/whatsapp`.
- Subscribe to WhatsApp `messages` webhooks.
- The API stores inbound messages and mirrors them to every numeric Telegram `user_id` in `TELEGRAM_ALLOWED_USER_IDS`.
- Use `/inbox` in Telegram to inspect the latest stored inbound messages.
- Use `/readyz` to check whether required webhook and Telegram settings are present without exposing secret values.
- Follow [docs/WHATSAPP_WEBHOOK_SETUP.md](docs/WHATSAPP_WEBHOOK_SETUP.md) for the Meta + Render connection steps.
- WhatsApp Cloud API is still used for webhook ingestion and direct messages. Automatic posting to WhatsApp groups needs a separate group-capable sender behind `WHATSAPP_GROUP_SENDER_URL`.

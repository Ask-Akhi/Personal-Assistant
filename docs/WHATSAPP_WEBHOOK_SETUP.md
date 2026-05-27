# WhatsApp Cloud Webhook Setup

This connects Meta WhatsApp Cloud API to the assistant's Phase 1 observability flow.

## 1. Prepare Secrets

Create `.env` locally from `.env.example`, or add these in the Render `assistant-secrets` env group:

```env
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ALLOWED_USER_IDS=123456789
ADMIN_PIN=123456
WHATSAPP_VERIFY_TOKEN=choose-a-long-random-string
WHATSAPP_APP_SECRET=meta-app-secret
WHATSAPP_CLOUD_TOKEN=meta-system-user-or-temporary-token
WHATSAPP_PHONE_NUMBER_ID=meta-phone-number-id
```

Notes:

- `WHATSAPP_VERIFY_TOKEN` is invented by you. Put the same value in Meta's webhook setup screen.
- `WHATSAPP_APP_SECRET` comes from Meta App Dashboard > App settings > Basic.
- `WHATSAPP_CLOUD_TOKEN` and `WHATSAPP_PHONE_NUMBER_ID` come from WhatsApp > API Setup.
- Keep the token values out of chat, screenshots, commits, and logs.

## 2. Deploy API

Deploy the Render blueprint, then open:

```text
https://<your-render-service>.onrender.com/healthz
https://<your-render-service>.onrender.com/readyz
```

`/healthz` should return `ok: true`.

`/readyz` should show the Telegram and WhatsApp values as present. It only returns booleans, not the secret values.

## 3. Configure Meta Webhook

In Meta for Developers:

1. Open your app.
2. Add or open the WhatsApp product.
3. Go to Configuration or Webhooks.
4. Set Callback URL:

```text
https://<your-render-service>.onrender.com/webhooks/whatsapp
```

5. Set Verify Token to the exact `WHATSAPP_VERIFY_TOKEN` value.
6. Click Verify and Save.
7. Subscribe to the `messages` webhook field.

Meta verifies by sending `hub.mode=subscribe`, `hub.verify_token`, and `hub.challenge` to `GET /webhooks/whatsapp`. The app responds with the challenge only if the token matches.

## 4. Confirm Inbound Flow

Send a WhatsApp message to the Cloud API number.

Expected result:

- Meta sends a POST webhook to `/webhooks/whatsapp`.
- The app validates `X-Hub-Signature-256` when `WHATSAPP_APP_SECRET` is set.
- The app stores or updates the contact.
- The app stores the inbound message once, even if Meta retries the webhook.
- Telegram receives a read-only mirror message.
- `/inbox` in Telegram shows the message.

## 5. Troubleshooting

If Meta verification fails:

- Confirm the Render URL is public and uses HTTPS.
- Confirm `WHATSAPP_VERIFY_TOKEN` in Render exactly matches Meta.
- Open `/readyz` and confirm `whatsapp_verify_token` is true.

If inbound messages do not arrive:

- Confirm the app is subscribed to WhatsApp `messages`.
- Confirm you are messaging the Cloud API phone number, not a different WhatsApp number.
- Check Render logs for `webhook:wa_cloud` audit events.
- Confirm `TELEGRAM_ALLOWED_USER_IDS` is a numeric Telegram ID, not a username.

If POST requests return `bad_signature`:

- Confirm `WHATSAPP_APP_SECRET` is the App Secret for the same Meta app.
- Do not modify the raw request body before signature validation.
- Temporarily remove `WHATSAPP_APP_SECRET` only for diagnosis, then restore it.

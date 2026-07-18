# Wassalny — WhatsApp-first ride dispatch backend

Flask backend + team dashboard for a ride/transportation business using the
**WhatsApp Cloud API**. Customers message you asking for a car, your team sees
their message in a real-time inbox, replies, then dispatches a captain over
WhatsApp — all from one place.

Also exposes a **JWT REST API** so you can plug in customer and captain mobile
apps later.

## Features

- 📥 **Real-time inbox** — two-way WhatsApp chat with customers and captains (Socket.IO)
- 👤 **Team accounts** — admin / dispatcher / agent roles
- 🚗 **Captains** — manage drivers and broadcast ride requests to them
- 📋 **Ride requests** — track pickup/dropoff, assign captains, mark completed
- ✉️ **Approved templates** — send Meta-approved template messages outside the 24h window
- 🔒 **Webhook signature verification** — validates incoming Meta webhooks with HMAC-SHA256
- 📱 **JWT REST API** at `/api/v1/*` for future mobile apps

## Quick start (local)

```bash
cd wassalny
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # then edit with your WhatsApp credentials
python wsgi.py
```

Then open http://localhost:5000 and sign in with the admin credentials from
`.env` (`ADMIN_EMAIL` / `ADMIN_PASSWORD`).

### Exposing your webhook to Meta (local testing)

Use `ngrok` to give Meta a public URL for your local Flask server:

```bash
ngrok http 5000
```

Take the `https://xxxx.ngrok.app` URL and set it as your webhook in Meta:

- Webhook URL: `https://xxxx.ngrok.app/webhook`
- Verify token: whatever you put in `WHATSAPP_VERIFY_TOKEN`
- Subscribe to the `messages` field

## Deployment (Railway or Render)

1. Push this folder to GitHub.
2. On Railway/Render: New project → deploy from GitHub → pick this repo.
3. Add a **PostgreSQL** plugin — `DATABASE_URL` will be injected automatically.
4. Add all environment variables from `.env.example` in the platform's env settings.
5. Deploy. The `Procfile` starts gunicorn with the eventlet worker (required for WebSockets).
6. In Meta developers → WhatsApp → Configuration → set your webhook URL to
   `https://your-app.up.railway.app/webhook`.

## WhatsApp API setup (Meta side)

1. Create a Meta developer account: https://developers.facebook.com
2. Create an app → add the **WhatsApp** product.
3. In *API Setup* copy: **Access Token**, **Phone number ID**, **Business Account ID**.
   Add them to your `.env`.
4. In *Configuration* → *Webhook*, add your webhook URL + verify token, subscribe
   to `messages`.
5. In *App Settings* → *Basic*, copy your **App Secret** → this is `WHATSAPP_APP_SECRET`.

## Cost expectations (Egypt, 2026)

At 500–1000 messages/day for a delivery business:
- Most messages are **Utility** (order confirmations, dispatch updates) → ~$0.0036 each
- Realistic monthly cost: **$50–$150** for utility-heavy traffic
- Free tier: unlimited service replies within the 24h window

## Project structure

```
wassalny/
├── app/
│   ├── __init__.py         # Flask factory + Socket.IO + JWT setup
│   ├── models/             # SQLAlchemy models
│   ├── routes/             # Web (dashboard) blueprints
│   ├── api/                # REST API for mobile apps
│   ├── services/           # whatsapp.py + inbox.py business logic
│   ├── sockets/            # Real-time WebSocket handlers
│   ├── templates/          # Jinja2 dashboard views
│   └── static/             # JS + CSS
├── config.py
├── wsgi.py
├── Procfile
├── requirements.txt
└── .env.example
```

## API endpoints

**Team dashboard (session auth):**
- `GET  /inbox` — conversation list + chat view
- `GET  /drivers` — captains list
- `GET  /rides` — ride requests
- `GET  /users` — team members (admin only)

**Meta webhook:**
- `GET  /webhook` — verification handshake
- `POST /webhook` — incoming messages + status updates

**Mobile / external API (JWT):**
- `POST /api/v1/auth/team/login`
- `POST /api/v1/auth/driver/login`
- `GET  /api/v1/conversations`
- `GET  /api/v1/conversations/:id/messages`
- `POST /api/v1/conversations/:id/messages`
- `GET  /api/v1/rides`
- `POST /api/v1/rides`
- `GET  /api/v1/driver/rides`
- `POST /api/v1/driver/status`

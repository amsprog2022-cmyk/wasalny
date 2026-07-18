# Wassalny — Deploy to Railway

Everything needed to push the backend to Railway and hand out the public captain-registration link.

## What you get after deploy

- Public captain signup at `https://YOUR-APP.up.railway.app/captain/register`
- Admin dashboard at `https://YOUR-APP.up.railway.app/`
- REST + WebSocket API at `/api/v1/*` (for the future Flutter apps)
- Meta WhatsApp webhook at `/webhook`

## Steps

### 1. Push the code to GitHub

```bash
cd ~/Desktop/my\ projects/wassalny
git init
git add .
git commit -m "Wassalny backend v1"
gh repo create wassalny --private --source=. --push
```

(Or use the GitHub UI if you prefer.)

### 2. Create the Railway project

1. Go to https://railway.app → **New Project** → **Deploy from GitHub repo**
2. Pick the `wassalny` repo
3. Railway detects the `Procfile` automatically and starts building

### 3. Add PostgreSQL

Inside the same project:
1. Click **+ New** → **Database** → **PostgreSQL**
2. Railway auto-injects `DATABASE_URL` into your web service

### 4. Add Redis

Same project:
1. Click **+ New** → **Database** → **Redis**
2. Copy the `REDIS_URL` from Redis → **Variables** → paste it into the web service's **Variables** tab

### 5. Set environment variables

In the web service → **Variables**, add these:

| Key | Value |
|---|---|
| `SECRET_KEY` | any long random string (e.g. `openssl rand -hex 32`) |
| `JWT_SECRET_KEY` | another random string |
| `ADMIN_EMAIL` | `admin@wassalny.com` (or whatever you want) |
| `ADMIN_PASSWORD` | strong password — this is YOUR admin login |
| `DEFAULT_CAPTAIN_PASSWORD` | e.g. `wassalny2026` — the default password every registered captain gets |
| `REDIS_URL` | (paste from step 4) |

**When your Facebook Business is ready**, add these too:

| Key | Value |
|---|---|
| `WHATSAPP_ACCESS_TOKEN` | from Meta API Setup |
| `WHATSAPP_PHONE_NUMBER_ID` | from Meta API Setup |
| `WHATSAPP_BUSINESS_ACCOUNT_ID` | from Meta API Setup |
| `WHATSAPP_APP_SECRET` | from Meta App Settings → Basic |
| `WHATSAPP_VERIFY_TOKEN` | any string you make up |
| `GEMINI_API_KEY` | from Google AI Studio when you have it |

### 6. Wait for the deploy to go green

Railway will build (~2 min), then boot. On first boot `db.create_all()` creates every table and the admin user from your env vars.

### 7. Seed the zones

Once deployed, run this once (Railway → your service → **Settings** → **Deploy** → **Run command**):

```bash
python -m ops.seed_zones
```

That creates the 5 test Benha zones + the 25 price rows. **Skip `seed_fake_data`** in production — that's for local dev only.

### 8. Set the Meta webhook (when WhatsApp is ready)

In Meta developers → WhatsApp → Configuration:
- Callback URL: `https://YOUR-APP.up.railway.app/webhook`
- Verify token: whatever you set in `WHATSAPP_VERIFY_TOKEN`

## Testing after deploy

1. Open `https://YOUR-APP.up.railway.app/captain/register` on your phone
2. Fill the form → submit
3. Open `https://YOUR-APP.up.railway.app/` on your laptop → login as admin
4. Go to **الكباتن** → you should see the new pending captain
5. Click him → **اقبل الكابتن**
6. Done — he can now log into the future captain app

## The public captain link

**This is what you share with drivers:**

```
https://YOUR-APP.up.railway.app/captain/register
```

Send it on WhatsApp, print it on flyers, whatever. Every submission shows up in your admin `/drivers?filter=pending`.

## Costs

Railway starter tier: **~$5/month** (small Postgres + small Redis + web).
Free tier gives you $5/month credit — enough for testing.

Bump to Pro plan (~$20/month) when you go live with real captains.

## Troubleshooting

| Problem | Fix |
|---|---|
| Build fails on `pip install` | Check `runtime.txt` says `python-3.11.9`, not something newer |
| 500 on first request | Check logs: probably `db.create_all()` failed — verify `DATABASE_URL` is set |
| Webhook returns 403 | `WHATSAPP_VERIFY_TOKEN` mismatch — must match exactly on both sides |
| Can't log in as admin | `ADMIN_EMAIL` / `ADMIN_PASSWORD` env vars weren't set on first boot; run `python -m ops.reset_admin` (or nuke the users table and reboot) |

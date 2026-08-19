# Deployment guide — hosting the Forklift Database for your team

This app is **Python (FastAPI)**. That one fact drives everything below, because
it determines whether a given host can run it.

---

## Step 0 — Figure out what you have at GoDaddy

GoDaddy sells several different things. Check which one you have (log in →
*My Products*):

| What you have | Can it run this app? | What to do |
|---|---|---|
| **Just a domain name** (e.g. yourcompany.com) | The domain doesn't run anything by itself | Get a small VPS (Step 1), then point the domain at it (Step 4). Keep the domain where it is. |
| **Web Hosting / cPanel / "Deluxe/Ultimate"** | ❌ No — this runs PHP, not Python apps | Keep it for email/website if you like, but host THIS app on a VPS (Step 1) and point a subdomain at it. |
| **Managed WordPress** | ❌ No | Same as above — use a VPS for this app. |
| **VPS / Dedicated Server** | ✅ Yes — full Linux | Deploy straight to it (Step 2). |

**Bottom line:** the only GoDaddy product that can run this app is a **VPS or
Dedicated Server**. Everything else = "keep the domain, run the app on a small
Linux server, point the domain at it." That server can be a GoDaddy VPS or any
provider (DigitalOcean, Hetzner, Linode, AWS Lightsail — typically $5–7/month).

---

## Step 1 — Get a Linux server (skip if you already have a VPS)

Provision a small Ubuntu 22.04+ server (1 shared CPU / 1 GB RAM is plenty for a
small team). Note its **public IP address**. SSH in and install Docker:

```bash
curl -fsSL https://get.docker.com | sh
```

## Step 2 — Put the app on the server

Copy this project folder to the server (git clone, `scp`, or an upload). Then:

```bash
cd forklift-db
cp .env.example .env
```

## Step 3 — Configure `.env`

Edit `.env` and set at least:

- `APP_DOMAIN` — the address you'll use, e.g. `forklifts.yourcompany.com`
- `SESSION_SECRET` — run `python3 -c "import secrets; print(secrets.token_urlsafe(48))"` and paste
- `SESSION_HTTPS_ONLY=true`
- `ALLOWED_EMAIL_DOMAIN` — your company domain, e.g. `yourcompany.com` (only these emails can sign in)
- `ADMIN_EMAILS` — your email, so you become the first admin
- `ANTHROPIC_API_KEY` and `TAVILY_API_KEY` — for the AI features
- **One** sign-in provider (Google or Microsoft) from Step 5
- Leave `DEV_LOGIN=false`

## Step 4 — Point your domain at the server (DNS)

In GoDaddy → your domain → **DNS** → add an **A record**:

- **Type:** A · **Name:** `forklifts` (for `forklifts.yourcompany.com`) or `@` for the root domain
- **Value:** your server's public IP · **TTL:** default

DNS can take a few minutes to a couple of hours to propagate.

## Step 5 — Create the sign-in (OAuth) app — pick Google OR Microsoft

You (or your IT admin) create an OAuth app so employees can log in with their
existing work accounts. The **redirect URI** must be exactly:

```
https://APP_DOMAIN/auth/google/callback      (for Google)
https://APP_DOMAIN/auth/microsoft/callback   (for Microsoft)
```
(replace `APP_DOMAIN` with your real domain)

### Google (for Google Workspace companies)
1. <https://console.cloud.google.com> → create a project.
2. *APIs & Services → OAuth consent screen* → **Internal** (limits to your Workspace) → fill required fields.
3. *APIs & Services → Credentials → Create credentials → OAuth client ID* → type **Web application**.
4. Add the **Authorized redirect URI** above.
5. Copy the **Client ID** and **Client secret** into `.env` (`GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`).

### Microsoft (for Microsoft 365 companies)
1. <https://portal.azure.com> → *Microsoft Entra ID → App registrations → New registration*.
2. Supported account types: **Accounts in this organizational directory only**.
3. Redirect URI: **Web** → the Microsoft URI above.
4. *Certificates & secrets → New client secret* → copy the **Value**.
5. Put the **Application (client) ID**, secret, and your **Directory (tenant) ID**
   into `.env` (`MICROSOFT_CLIENT_ID`, `MICROSOFT_CLIENT_SECRET`, `MICROSOFT_TENANT`).

## Step 6 — Launch

```bash
docker compose up -d --build
```

Caddy automatically fetches a free HTTPS certificate for `APP_DOMAIN`. Visit
`https://APP_DOMAIN` — you'll get the sign-in page. Sign in with your work
account; because your email is in `ADMIN_EMAILS`, you'll be an admin. Everyone
else at your company who signs in becomes a **viewer** until you promote them on
the **Users** page.

### Bringing your existing data
The 400+ forklifts, kits, and years live in `data/forklifts.db`. Copy that file
into the server's `data/` volume before launch (or `docker cp` it into the
running `app` container at `/app/data/forklifts.db`) to keep everything you've
built. Otherwise the app starts with an empty database.

---

## Everyday operations

```bash
docker compose logs -f app     # view logs
docker compose restart app     # restart after an .env change
docker compose pull && docker compose up -d --build   # update after code changes
```

Back up the SQLite database periodically — it's a single file in the
`forklift-data` Docker volume (`docker compose cp app:/app/data/forklifts.db ./backup.db`).

## What only you/your client can do (not doable from the dev environment)
1. Provision the server / VPS.
2. Create the Google or Microsoft OAuth app (needs your Workspace/365 admin).
3. Set the domain's DNS A record.

Everything else — the app, auth, roles, HTTPS, containers — is built and ready.

## Scaling up later
For many users or heavier load, switch `DATABASE_URL` to Postgres (add a
`postgres` service to `docker-compose.yml`) — no application code changes needed.

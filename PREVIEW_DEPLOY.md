# Preview deploy — a stable link for review (Render)

Goal: get a professional `https://…onrender.com` link your reviewer can open from
anywhere, showing the full app with all your data, gated by a simple password. If they
approve, this same setup becomes production (just add your domain + real logins later).

You do the account steps yourself — **I never need your GitHub or Render password.**
Everything code-side is already prepared (Docker, the `render.yaml` blueprint, your data
baked into `deploy/seed.db`, and a password login).

---

## Part 1 — Put the code on GitHub (~5 min)

1. On <https://github.com> → **New repository** → name it e.g. `forklift-database` →
   **Private** (it contains your kit catalog) → **Create repository**. Don't add a README.
2. Copy the repo URL GitHub shows (e.g. `https://github.com/you/forklift-database.git`).
3. In a terminal, from the project folder, run these (replace the URL):

```bash
cd "C:\Users\Jacob Barnhill\Downloads\forklift-db"
git init
git add .
git commit -m "Forklift database — initial"
git branch -M main
git remote add origin https://github.com/you/forklift-database.git
git push -u origin main
```

The first `git push` will ask you to sign in to GitHub (browser or the credential
popup) — that's you authenticating as yourself. Your API keys are **not** included
(`.env` is git-ignored); they go into Render in Part 2.

> Sanity check before pushing: `git status` should NOT list `.env`. It shouldn't —
> it's ignored — but confirm.

---

## Part 2 — Deploy on Render (~5 min)

1. Go to <https://render.com> → sign up (free) → connect your GitHub when asked.
2. **New +** → **Blueprint** → pick your `forklift-database` repo. Render reads
   `render.yaml` and proposes the service automatically.
3. It will ask you to fill the secret values (the ones marked "will be set"):
   - **ACCESS_PASSWORD** — the review password you'll give the reviewer (pick anything, e.g. `ForkliftReview2026`).
   - **ANTHROPIC_API_KEY** — your Claude key (for the AI features).
   - **TAVILY_API_KEY** — your Tavily key.
   - `SESSION_SECRET` is generated for you; `SESSION_HTTPS_ONLY` and `DEV_LOGIN` are preset.
4. **Apply / Create** → Render builds the Docker image and deploys. First build takes
   a few minutes; watch the logs until it says *Live*.
5. Your link appears at the top: `https://forklift-database-XXXX.onrender.com`.

---

## Part 3 — Share it

Send the reviewer:
- the **link**, and
- the **ACCESS_PASSWORD** you chose.

They open the link → enter the password → browse the full app (view-only). They see all
416 forklifts, the kits, the OEM grouping, and can try the plain-English query box.

---

## Good to know
- **Free tier sleeps** after ~15 min idle, so the first visit after a pause takes ~30s to
  wake. Fine for a review; upgrade to a paid instance ($7/mo) for no sleep if needed.
- **Edits won't persist** on the free preview (the filesystem resets on redeploy/sleep).
  That's fine for viewing. For real production you'd add a persistent disk or Postgres —
  a small change when you're ready.
- **Updating the preview:** make changes, then `git add . && git commit -m "…" && git push`
  — Render auto-rebuilds.
- **If they approve:** we point your GoDaddy domain at it and switch the login from the
  shared password to real Google/Microsoft sign-in (all already built — see `DEPLOY.md`).

## What only you can do
Create the GitHub repo, create the Render account, and paste the secret values into
Render's dashboard. I've prepared everything else.

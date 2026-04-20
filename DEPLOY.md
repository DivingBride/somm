# Somm v2.0 — Deployment Runbook

This is a **step-by-step** guide for taking Somm from "works on my laptop"
to "live at somm.onrender.com". It assumes zero devops experience.

Every step is either **(You)** — something you click, paste, or type — or
**(Claude)** — something Claude has already done or will do when asked.

Total time if nothing goes wrong: **~45 minutes**, most of it waiting on
DNS verification and the first build.

---

## 0. What you will need before you start

Gather these in a password manager or notes file. Don't paste secrets
into chat.

- [ ] **GitHub account** with push access to `DivingBride/somm`.
- [ ] **Render account** — sign up at <https://render.com> using the
      same email you want to use for prod alerts (or a shared ops email).
- [ ] **Anthropic API key** — you already have one in `.env`. Get a
      fresh one for prod at <https://console.anthropic.com/settings/keys>
      if you want to rotate. (Optional but recommended.)
- [ ] **Resend account** — sign up at <https://resend.com>. Free tier
      covers ~100 emails/day, plenty for 20 users.
- [ ] **An email address** you control that will be the **founder admin**.
      This is the only account that can invite others at launch.
- [ ] **A domain** (optional at launch) — spec §13 says `somm.onrender.com`
      is fine for v2.0; custom domain is a later iteration.

---

## 1. Security housekeeping (You — 5 min)

### 1a. Your GitHub token is embedded in the repo's git remote.

This isn't in git history, but anyone with filesystem access to this
laptop can read it. Fix:

```bash
cd "/Users/nathalienayman/Desktop/Claude/Projects/Wine Pairings/somm-app"
git remote set-url origin https://github.com/DivingBride/somm.git
```

Now `git push` will prompt for credentials. On macOS, the
**osxkeychain** credential helper stores them securely:

```bash
git config --global credential.helper osxkeychain
```

The first push after that prompts for username (your GitHub login) and
password (use a **Personal Access Token** from
<https://github.com/settings/tokens>, not your actual password). macOS
keychain remembers it afterwards.

### 1b. Rotate the old PAT.

Go to <https://github.com/settings/tokens>, find the token starting with
`ghp_hZZ5...`, click **Delete**. It's been sitting in your git config
in plaintext; no reason to trust it anymore.

### 1c. Confirm `.env` is not committed.

```bash
git check-ignore -v .env
```

Expected output: `.gitignore:1:.env   .env`. If that fails, stop and
ping Claude.

---

## 2. Commit Phase 2–5 work (You — 2 min)

Claude wrote Phases 2–5 but didn't commit. Do it now:

```bash
cd "/Users/nathalienayman/Desktop/Claude/Projects/Wine Pairings/somm-app"
git add -A
git status    # visually confirm .env is NOT listed
git commit -m "Somm v2.0 — chats, preferences, Chartier library, saved pairings, hardening"
git push origin main
```

If `git push` is rejected because the remote has diverged:

```bash
git pull --rebase origin main
git push origin main
```

---

## 3. Generate your secrets (You — 3 min)

Open a terminal and run each of these. **Save the output in a password
manager** — you'll paste them into Render in step 5.

### 3a. Session secret

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(64))"
```

Label this: `SESSION_SECRET`.

### 3b. Anthropic API key

Either reuse the one in `.env` (copy it out — **don't commit `.env`**),
or generate a fresh one at
<https://console.anthropic.com/settings/keys>.

Label this: `ANTHROPIC_API_KEY`.

### 3c. Resend API key

1. Sign in to <https://resend.com>.
2. **API Keys** → **Create API Key**.
3. Name it `somm-prod`, permission **Full access**, scope **All domains**.
4. Copy the key **immediately** — Resend won't show it again.

Label this: `RESEND_API_KEY`.

---

## 4. Dry-run locally (You — 5 min)

Before pushing anything, prove the app boots with prod-shaped config:

```bash
cd "/Users/nathalienayman/Desktop/Claude/Projects/Wine Pairings/somm-app"
./scripts/prod-dryrun.sh
```

Expected output ends with:

```
=== /health ===
{"status":"ok"}
=== /healthz ===
{ "status": "degraded", ... }   ← degraded is fine locally (Resend disabled)
✅ App is up. Open http://127.0.0.1:8765 in a browser.
```

Open <http://127.0.0.1:8765> → you should see the login page. Ctrl-C to
stop. If anything errors, **stop and paste the output to Claude**.

---

## 5. Provision on Render (You — 15 min)

### 5a. Create the service via Blueprint

1. Go to <https://dashboard.render.com>.
2. Click **New** → **Blueprint**.
3. Connect your GitHub account if you haven't; authorize Render to see
   the `DivingBride/somm` repo.
4. Pick `DivingBride/somm` from the list.
5. Render reads `render.yaml` and shows a preview: **1 Web Service
   (somm)** + **1 Disk (somm-data, 1 GB)**.
6. Render prompts for the four `sync: false` secrets. Paste each:
   - `ANTHROPIC_API_KEY` — from step 3b
   - `SESSION_SECRET` — from step 3a
   - `RESEND_API_KEY` — from step 3c
   - `ADMIN_EMAIL` — the founder email you chose in step 0
7. Optional secrets (set them or leave blank for now):
   - `ADMIN_DISPLAY_NAME` — e.g. `Nathalie`
   - `ADMIN_ALERT_EMAILS` — e.g. `you@example.com,backup@example.com`
     (leave blank → alerts go to `ADMIN_EMAIL`)
8. Click **Apply**.

### 5b. Watch the first build

You'll land on the service's **Events** page. Click **Logs** and watch:

- `pip install -r requirements.txt` — ~2 min
- `alembic upgrade head` — ~2 sec
- `[somm] SOMM_DATA_PATH resolved to: ...` — startup
- `[somm.bootstrap] Seeded founder admin account: <your email>` — ⬅ this
  confirms ADMIN_EMAIL worked
- `Uvicorn running on http://0.0.0.0:10000`
- `Your service is live at https://somm.onrender.com`

If build fails, **copy the error and paste it to Claude.**

### 5c. Hit /healthz on the live URL

```bash
curl https://somm.onrender.com/healthz | python3 -m json.tool
```

Expected:
- `db.status: ok`
- `chartier_xlsx.status: ok`
- `email.status: ok` ← depends on RESEND_API_KEY being valid
- `admin_alerts.status: ok` (or `unconfigured` if you left it blank)
- Overall `status: ok` or `degraded`

---

## 6. Verify Resend sending domain (You — 10–30 min)

**Skip this if you're happy with the default `onboarding@resend.dev`
sender.** That's fine for launch with 3–5 trusted users — magic links
will arrive from `Somm <onboarding@resend.dev>`.

For real deliverability (avoids spam folders), verify your own domain:

1. In Resend, **Domains** → **Add Domain** → e.g. `mail.yourdomain.com`.
2. Resend shows three DNS records (SPF, DKIM, DMARC).
3. Add them at your DNS provider (GoDaddy / Cloudflare / Namecheap /
   whoever hosts the domain). Each provider has slightly different UI —
   Resend links to vendor-specific guides.
4. Click **Verify**. DNS propagation is usually <5 min but can take up
   to an hour.
5. Once verified, in Render dashboard:
   `FROM_EMAIL` = `Somm <somm@mail.yourdomain.com>` (or similar).
   Click **Save Changes** → Render redeploys automatically.

---

## 7. First sign-in (You — 3 min)

1. Open <https://somm.onrender.com/login> in a browser.
2. Enter the founder email (`ADMIN_EMAIL`).
3. Click **Send sign-in link**.
4. Check your inbox (and spam folder if Resend domain isn't verified).
   Click the link.
5. You should land on the chat page. Confirm:
   - You see your display name in the top right
   - There's an **Admin** link in the sidebar
   - Clicking **Admin** shows the System Status panel, Users, Invites,
     and Audit log
   - System Status shows all-green
6. Pick a pairing question (e.g. "wine for roast lamb") and send. You
   should get a response in ~10–15 sec.

If you don't receive the email within 2 min, check Render logs — search
for `Magic link for`. In dev mode Resend failures get logged.

---

## 8. Invite your first real user (You — 2 min)

1. On `/admin`, under **Invites**, fill in the form:
   - **Pin to email**: their email (recommended — scopes the invite to
     one person)
   - **Expires in days**: 7
2. Click **Create invite** → a share URL appears.
3. Copy the share URL and send it to them via text / DM / email.
4. They open it, enter their email + display name → get a magic link →
   sign in.
5. They'll appear in the Users table with role `user`.

---

## 9. Configure backups (You — 1 min)

Render takes daily disk snapshots automatically on the Starter plan.
Verify:

1. Service dashboard → **Disks** tab → click `somm-data`.
2. You should see **Automatic snapshots: Enabled**.

That's it. Off-site backup to Backblaze B2 is deferred per spec §13.2.

---

## 10. Test the alert path (You — 2 min, optional)

Confirm that 5xx alerts actually land in your inbox:

```bash
# Replace with your actual URL
curl https://somm.onrender.com/__this_route_does_not_exist
```

That returns 404 (no alert). To test the 500 path you'd need to
deliberately crash the app, which isn't worth the risk in prod.
Instead, trust the smoke test — check `66` in `smoke_test_auth.py`
proves the handler fires end-to-end.

---

## Rollback

If a deploy goes sideways:

1. Render dashboard → service → **Deploys** tab.
2. Find the last known-good deploy → click **⋯** → **Redeploy**.

Render swaps traffic back in ~30 sec. The persistent disk (DB) is not
rolled back — schema migrations are forward-only. If a migration is
the problem, ping Claude.

---

## Day-2 operations quick reference

| I want to…                                         | Where                                                                         |
|----------------------------------------------------|-------------------------------------------------------------------------------|
| See what's happening right now                     | Render **Logs** tab — streams access log + errors                             |
| Check system health                                | `/admin` → System Status panel, or `curl /healthz`                            |
| Revoke a user                                      | `/admin` → Users → **Revoke**                                                 |
| Re-sync the Chartier library after editing xlsx    | Push a new commit with the updated file OR call `POST /api/admin/chartier/sync` |
| Rotate `ANTHROPIC_API_KEY`                         | Anthropic console → new key → Render dashboard env vars → Save → auto-redeploy |
| Rotate `SESSION_SECRET`                            | **Warning:** invalidates every active session (everyone signs in again)       |
| Change `FROM_EMAIL`                                | Render env vars → Save                                                        |
| See the audit log                                  | `/admin` → Audit log section (filterable by action / actor)                   |

---

## When to ping Claude

- **Build fails on Render** — paste the full error.
- **`alembic upgrade head` fails** — this is a migration bug, needs code.
- **`/healthz` shows `fail` for a subsystem** — paste the JSON response.
- **User reports "my magic link doesn't work"** — usually Resend deliverability;
  check Resend dashboard "Logs" and paste any error.
- **You want a feature change** — v2.0.1 / v2.1 territory; see
  `Somm_v2_Spec.md` §14 for the roadmap.

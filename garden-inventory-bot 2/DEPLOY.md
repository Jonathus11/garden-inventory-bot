# Hosting the bot 24/7

Once hosted, the bot stays online for **everyone in your Discord channel** —
they just post a screenshot and it tracks their inventory. You don't need your
own computer running.

The recommended host is **Railway** (easiest for Discord bots). Render and
Replit notes are at the bottom.

---

## First: get your two secrets ready
You'll paste these into the host as environment variables (never in the code):

- `DISCORD_BOT_TOKEN` — Discord Developer Portal → your app → Bot → Reset Token
- `ANTHROPIC_API_KEY` — https://console.anthropic.com → API Keys

Also make sure **MESSAGE CONTENT INTENT** is ON (Developer Portal → Bot), and
that you invited the bot to your server (see `README.md`, steps 1–2).

---

## Option A — Railway (recommended)

### 1. Put the code on GitHub
1. Create a free account at https://github.com and click **New repository**
   (name it e.g. `garden-inventory-bot`, keep it Private).
2. Upload every file in this folder **except** `.env` and `inventory.json`
   (the `.gitignore` already blocks those). You can drag-and-drop the files on
   GitHub's "uploading an existing file" page if you don't use git.

### 2. Deploy on Railway
1. Sign up at https://railway.app with your GitHub account.
2. **New Project → Deploy from GitHub repo →** pick your repo.
3. Railway detects Python and installs `requirements.txt` automatically, then
   runs the `Procfile` (`python bot.py`).
4. Open the service → **Variables** tab → add:
   - `DISCORD_BOT_TOKEN` = your token
   - `ANTHROPIC_API_KEY` = your key
5. It redeploys. Check **Deployments → Logs**; you want to see
   `Ready. Post an inventory screenshot to track it.`

That's it — go to your Discord channel and drop in a screenshot.

### 3. (Important) Keep your data from resetting
Railway's disk is wiped on each redeploy, which would erase `inventory.json`.
To keep inventory permanently:
1. In your service → **Settings → Volumes → New Volume**.
2. Mount path: `/data`
3. Add a variable `INVENTORY_PATH=/data/inventory.json` *(optional — see note
   below).*

> The bot currently saves `inventory.json` next to the code. If you add a
> volume, tell me and I'll switch it to read `INVENTORY_PATH` so your data lives
> on the volume. For casual use you can skip this and just re-post screenshots
> after a redeploy.

---

## Option B — Render (free tier)
1. Push the code to GitHub (same as above).
2. https://render.com → **New → Web Service** → connect the repo.
3. Runtime: Python 3. Start command: `python bot.py`.
4. Add the two environment variables (`DISCORD_BOT_TOKEN`, `ANTHROPIC_API_KEY`).
5. Render's free tier sleeps idle web services — the bot already includes a
   keep-alive web server that binds Render's `PORT`, which helps, but the free
   tier can still nap. For rock-solid uptime, Railway or a paid Render plan is
   better.

## Option C — Replit (easy to test)
1. https://replit.com → **Create Repl → Import from GitHub** (or upload files).
2. Add your secrets in the **Secrets** (lock icon) panel:
   `DISCORD_BOT_TOKEN`, `ANTHROPIC_API_KEY`, and set `KEEP_ALIVE` = `1`.
3. Click **Run**. For true 24/7 you need Replit's paid **Always On**.

---

## Costs
- **Discord bot:** free.
- **Hosting:** Railway ~free-to-a-few-dollars/month for a small bot; Render/Replit
  have free tiers with the sleep caveats above.
- **AI vision:** a small Anthropic charge per screenshot read — typically a
  fraction of a cent each.

## Troubleshooting
- **Bot online but ignores images:** MESSAGE CONTENT INTENT isn't enabled, or the
  bot lacks "View Channel"/"Read Message History" permission in that channel.
- **Crashes on start:** a missing/typo'd env variable — check the host's logs.
- **Reads items wrong:** use `!set store 40 dragon breath` to correct it.

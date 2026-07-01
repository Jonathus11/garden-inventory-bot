# Quickstart — the easy path (about 10 minutes, mostly copy-paste)

You don't edit any code. You just create a few free accounts and paste in 2 keys.
Do these 5 things in order.

---

## 1. Make the Discord bot (2 min)
1. Go to https://discord.com/developers/applications → **New Application** → name it → Create.
2. Left menu → **Bot** → **Reset Token** → **Copy**. Paste it somewhere for a sec. ← this is your `DISCORD_BOT_TOKEN`
3. Scroll down on that same Bot page → turn ON **MESSAGE CONTENT INTENT** → **Save Changes**.

## 2. Invite the bot to your server (1 min)
1. Left menu → **OAuth2** → scroll to **OAuth2 URL Generator**.
2. Tick **bot**. In the permissions box that appears, tick: **View Channels**,
   **Send Messages**, **Read Message History**, **Attach Files**.
3. Copy the link at the bottom → open it in a new tab → pick your server → **Authorize**.

## 3. Get an AI vision key (2 min)
1. Go to https://console.anthropic.com → sign up → **API Keys** → **Create Key** → **Copy**.
   ← this is your `ANTHROPIC_API_KEY`  (add a little billing credit; each screenshot read costs a fraction of a cent.)

## 4. Put the code on GitHub (2 min, no coding)
1. Go to https://github.com → sign up → **New repository** → name it `garden-inventory-bot` → **Create**.
2. On the new repo page click **uploading an existing file**.
3. Unzip the file I gave you, then **drag all the files** from inside the folder into the upload box → **Commit changes**.

## 5. Deploy on Railway (2 min) — this makes it run 24/7
1. Go to https://railway.app → **Login with GitHub**.
2. **New Project → Deploy from GitHub repo →** pick `garden-inventory-bot`.
3. Open the project → **Variables** tab → **New Variable**, add these two:
   - `DISCORD_BOT_TOKEN` = the token from step 1
   - `ANTHROPIC_API_KEY` = the key from step 3
4. It builds and starts automatically. Open **Deployments → View Logs** and wait for:
   `Ready. Post an inventory screenshot to track it.`

**Done.** Go to your Discord channel and drop in an inventory screenshot. 🎉

---

### Using it
- Post a screenshot → it reads your items and saves them under your name (account inventory).
- Add the word **store** in the message to file it under the store inventory instead.
- `!inv` shows your stuff · `!store` shows everyone's store · `!who dragon breath` finds who has an item.
- Wrong read? Fix it: `!set store 40 dragon breath`

### Optional: never lose data on updates
Railway wipes files when it redeploys. To keep inventory forever:
1. Project → **Settings → Volumes → New Volume** → mount path `/data`.
2. **Variables** → add `INVENTORY_PATH` = `/data/inventory.json`.
That's already supported in the code — nothing else to do.

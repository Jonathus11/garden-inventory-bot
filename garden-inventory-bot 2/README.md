# Grow a Garden 2 — Inventory Tracker Bot

A Discord bot: post a screenshot of your inventory, and it uses Claude AI vision
to read the items + quantities and tracks them **per user** across two
inventories — **account** and **store** — so you can see who has what.

> Note: what you asked for needs a **bot**, not a plain webhook. Webhooks can only
> *send* messages into Discord; they can't receive images or read them. This bot does.

---

## What it does

- You post an inventory screenshot in a channel the bot can see.
- Add the word **`store`** in the message to file it under the store inventory.
  Otherwise it goes to your **account** inventory.
- The bot reads the image with Claude vision, extracts `item — quantity`, and
  adds it to your totals.
- Everyone's data lives in `inventory.json` next to the bot.

## Commands

| Command | What it does |
|---|---|
| *(post an image)* | Reads it and adds to your account inventory (or store, if you type "store") |
| `!inv [@user] [account\|store]` | Show an inventory (default: you, account) |
| `!accounts` | Everyone's account items, combined |
| `!store` | Everyone's store items, combined |
| `!who <item>` | Who has an item, and how much |
| `!set <account\|store> <qty> <item>` | Manually set/correct a quantity |
| `!remove <account\|store> <item>` | Remove an item |
| `!clear <account\|store>` | Wipe your own inventory |
| `!help` | Show help |

---

## Setup (about 10 minutes)

### 1. Create the Discord bot
1. Go to https://discord.com/developers/applications → **New Application**, name it.
2. Left sidebar → **Bot** → **Reset Token** → copy the token (you'll paste it in `.env`).
3. On the same Bot page, scroll to **Privileged Gateway Intents** and turn ON
   **MESSAGE CONTENT INTENT**. Save. *(Required — the bot can't read messages/images without it.)*

### 2. Invite the bot to your server
1. Left sidebar → **OAuth2** → **URL Generator**.
2. Scopes: check **`bot`**.
3. Bot Permissions: check **Read Messages/View Channels**, **Send Messages**,
   **Read Message History**, **Attach Files**.
4. Copy the generated URL at the bottom, open it, and add the bot to your server.

### 3. Get an Anthropic API key
1. Go to https://console.anthropic.com/ → **API Keys** → create one.
2. Copy it for `.env`. (Vision calls cost a small amount per image — usually a
   fraction of a cent each.)

### 4. Configure & run
```bash
cd garden-inventory-bot

# copy the example env file and fill in your two keys
cp .env.example .env
#   open .env and paste DISCORD_BOT_TOKEN and ANTHROPIC_API_KEY

# install dependencies (Python 3.9+)
pip install -r requirements.txt

# run it
python bot.py
```

When you see `Ready. Post an inventory screenshot to track it.`, go to your
Discord server and drop in a screenshot.

---

## Tips
- **Keep it running:** the bot only works while `python bot.py` is running. For
  24/7 use, run it on a spare machine, a Raspberry Pi, or a cheap VPS.
- **Fix mistakes fast:** vision isn't perfect on messy screenshots. Use
  `!set store 40 dragon breath` to correct anything it misreads.
- **Backups:** all data is in `inventory.json` — copy that file to back up.
- **Multiple images at once:** you can attach several screenshots in one message;
  it reads them all and sums the quantities.

"""
Grow a Garden 2 — Inventory Tracker Discord Bot
=================================================
Post a screenshot of your inventory in Discord and this bot uses Claude
vision to read the items + quantities, then tracks them per user across
two inventories: "account" and "store".

Usage in Discord (after inviting the bot):
  - Post an image with the word "account" or "store" in the message to say
    which inventory it belongs to (defaults to "account" if you say nothing).
  - Reply "confirm" is NOT needed — vision runs automatically (auto mode).

Commands:
  !inv [@user] [account|store]  -> show a user's inventory (default: you, account)
  !store                        -> aggregate store inventory across everyone
  !accounts                     -> aggregate account inventory across everyone
  !who <item name>              -> who has this item and how much
  !set <account|store> <qty> <item>  -> manually set/correct a quantity
  !remove <account|store> <item>     -> remove an item
  !clear <account|store>        -> wipe your own inventory
  !help                         -> show this help

Data is stored in inventory.json next to this file.
"""

import os
import re
import json
import base64
import asyncio
import threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer

import discord
import aiohttp
import anthropic
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
load_dotenv()

DISCORD_TOKEN = (os.getenv("DISCORD_BOT_TOKEN") or "").strip()
ANTHROPIC_API_KEY = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
VISION_MODEL = os.getenv("VISION_MODEL", "claude-sonnet-5")

if not DISCORD_TOKEN:
    raise SystemExit("Missing DISCORD_BOT_TOKEN in .env")
if not ANTHROPIC_API_KEY:
    raise SystemExit("Missing ANTHROPIC_API_KEY in .env")

# Storage location. Defaults to a file next to the bot; on a host with a
# persistent volume, set INVENTORY_PATH (e.g. /data/inventory.json) so data
# survives redeploys.
DATA_FILE = Path(os.getenv("INVENTORY_PATH") or Path(__file__).with_name("inventory.json"))
VALID_KINDS = ("account", "store")

# Default game tools every player always has — never track these as inventory.
# Add more via the IGNORE_ITEMS env var (comma-separated), e.g. "shovel,build,basket".
_DEFAULT_IGNORE = {"shovel", "build", "hammer", "trowel", "basket", "wrench"}
IGNORE_ITEMS = _DEFAULT_IGNORE | {
    i.strip().lower() for i in os.getenv("IGNORE_ITEMS", "").split(",") if i.strip()
}

# max_retries lets the SDK automatically retry transient connection errors,
# timeouts, and rate limits with backoff; timeout guards against big images.
anthropic_client = anthropic.Anthropic(
    api_key=ANTHROPIC_API_KEY,
    max_retries=4,
    timeout=90.0,
)

# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------
# Structure:
# {
#   "users": {
#       "<username>": {
#           "account": { "<item>": qty, ... },
#           "store":   { "<item>": qty, ... }
#       }
#   }
# }
_lock = asyncio.Lock()


def _load() -> dict:
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return {"users": {}}


def _save(data: dict) -> None:
    DATA_FILE.write_text(json.dumps(data, indent=2, sort_keys=True))


def _user_bucket(data: dict, username: str, kind: str) -> dict:
    user = data["users"].setdefault(username, {"account": {}, "store": {}})
    return user.setdefault(kind, {})


def normalize_item(name: str) -> str:
    """Lowercase, strip, collapse whitespace so 'Dragon  Breath' == 'dragon breath'."""
    return re.sub(r"\s+", " ", name.strip().lower())


# ---------------------------------------------------------------------------
# Vision: read an inventory screenshot into {item: qty}
# ---------------------------------------------------------------------------
VISION_PROMPT = (
    "This is a screenshot from the Roblox game 'Grow a Garden 2'. Extract two things "
    "and return STRICT JSON only, no prose:\n"
    "1. The current PLAYER'S OWN username. There is a leaderboard panel (usually "
    "top-right, headed 'People' / 'Sheckles'). The current player's row is highlighted "
    "— brighter/whiter than the others. Return that username. If a leaderboard row's "
    "Sheckles number matches the coin count shown at the bottom-left of the screen, "
    "that row is the current player. If you truly cannot tell, return \"\".\n"
    "2. The inventory items with quantities. Items appear in inventory panels and in the "
    "hotbar/quickbar along the bottom (slots numbered 1-6). IMPORTANT: count each hotbar "
    "slot separately — if the same item (e.g. 'Black Dragon') fills 4 separate slots, "
    "that is a quantity of 4. If an item shows a stack number (xN), use that number.\n"
    "Return exactly this shape:\n"
    '{"username": "StashlySpecial", "items": [{"name": "black dragon", "qty": 4}]}\n'
    "Rules: lowercase item names; if a quantity is unreadable use 1; ignore currency, "
    "coins, sheckles, UI buttons, and the default tools (shovel, build/hammer, trowel, "
    "basket); do not invent items you cannot see."
)


async def read_inventory_from_image(image_bytes: bytes, media_type: str):
    """Return (in_game_username or "", {normalized_item: qty}). Blocking call in a thread."""
    b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

    def _call():
        return anthropic_client.messages.create(
            model=VISION_MODEL,
            max_tokens=1500,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": b64,
                            },
                        },
                        {"type": "text", "text": VISION_PROMPT},
                    ],
                }
            ],
        )

    resp = await asyncio.to_thread(_call)
    text = "".join(block.text for block in resp.content if block.type == "text")

    # Pull the first {...} JSON object out of the response defensively.
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return "", {}
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return "", {}

    username = str(parsed.get("username", "") or "").strip()

    result: dict[str, int] = {}
    for entry in parsed.get("items", []):
        name = normalize_item(str(entry.get("name", "")))
        if not name or name in IGNORE_ITEMS:
            continue
        try:
            qty = int(entry.get("qty", 1))
        except (TypeError, ValueError):
            qty = 1
        result[name] = result.get(name, 0) + max(qty, 0)
    return username, result


# ---------------------------------------------------------------------------
# Discord bot
# ---------------------------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True  # REQUIRED: enable in the Developer Portal too
client = discord.Client(intents=intents)


def detect_kind(text: str) -> str:
    low = text.lower()
    if "store" in low:
        return "store"
    return "account"  # default


def fmt_inventory(inv: dict) -> str:
    if not inv:
        return "_(empty)_"
    lines = [f"• {name} — {qty}" for name, qty in sorted(inv.items())]
    return "\n".join(lines)


@client.event
async def on_ready():
    print(f"Logged in as {client.user} (id: {client.user.id})")
    print("Ready. Post an inventory screenshot to track it.")


@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    content = message.content.strip()

    # --- Commands -----------------------------------------------------------
    if content.startswith("!"):
        await handle_command(message, content)
        return

    # --- Image posts --------------------------------------------------------
    images = [
        a for a in message.attachments
        if (a.content_type or "").startswith("image/")
        or a.filename.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))
    ]
    if not images:
        return

    kind = detect_kind(content)

    async with message.channel.typing():
        added_total: dict[str, int] = {}
        game_username = ""
        for att in images:
            data = await att.read()
            media_type = att.content_type or "image/png"
            if media_type not in ("image/png", "image/jpeg", "image/webp", "image/gif"):
                media_type = "image/png"
            try:
                uname, items = await read_inventory_from_image(data, media_type)
            except Exception as exc:  # noqa: BLE001
                # Log full detail to the private server logs only. NEVER echo raw
                # error text to Discord — it can contain secrets like API keys.
                cause_bits = []
                cur = exc
                seen = 0
                while cur is not None and seen < 5:
                    cause_bits.append(f"{type(cur).__name__}: {cur}")
                    cur = cur.__cause__ or cur.__context__
                    seen += 1
                print(f"[vision error] {' <- '.join(cause_bits)}", flush=True)
                await message.reply(
                    f"⚠️ Couldn't read that image ({type(exc).__name__}). "
                    "The admin can check the server logs for details."
                )
                continue
            if uname and not game_username:
                game_username = uname
            for name, qty in items.items():
                added_total[name] = added_total.get(name, 0) + qty

    if not added_total:
        await message.reply(
            "I couldn't read any items from that screenshot. "
            "Try a clearer/cropped image, or use `!set` to add items manually."
        )
        return

    # Track under the in-game username read from the leaderboard. Fall back to the
    # Discord poster's name only if vision couldn't find one.
    username = game_username or message.author.display_name
    from_note = "" if game_username else (
        "\n_(couldn't read the in-game name off the leaderboard — filed under your "
        "Discord name. Use `!set` or include a clearer leaderboard next time.)_"
    )

    async with _lock:
        data = _load()
        bucket = _user_bucket(data, username, kind)
        for name, qty in added_total.items():
            bucket[name] = bucket.get(name, 0) + qty
        _save(data)

    summary = fmt_inventory(added_total)
    await message.reply(
        f"✅ Added to **{username}**'s **{kind}** inventory "
        f"(posted by {message.author.display_name}):\n{summary}{from_note}"
    )


async def handle_command(message: discord.Message, content: str):
    parts = content.split()
    cmd = parts[0].lower()
    args = parts[1:]

    if cmd in ("!help", "!commands"):
        await message.reply(HELP_TEXT)
        return

    if cmd == "!inv":
        # !inv [@user] [account|store]
        target = message.author.display_name
        if message.mentions:
            target = message.mentions[0].display_name
        kind = "account"
        for a in args:
            if a.lower() in VALID_KINDS:
                kind = a.lower()
        data = _load()
        user = data["users"].get(target)
        if not user:
            await message.reply(f"No inventory tracked for **{target}** yet.")
            return
        inv = user.get(kind, {})
        await message.reply(
            f"**{target}** — {kind} inventory:\n{fmt_inventory(inv)}"
        )
        return

    if cmd in ("!store", "!accounts"):
        kind = "store" if cmd == "!store" else "account"
        data = _load()
        agg: dict[str, int] = {}
        for user in data["users"].values():
            for name, qty in user.get(kind, {}).items():
                agg[name] = agg.get(name, 0) + qty
        await message.reply(
            f"**Aggregate {kind} inventory (everyone):**\n{fmt_inventory(agg)}"
        )
        return

    if cmd == "!who":
        if not args:
            await message.reply("Usage: `!who <item name>`")
            return
        query = normalize_item(" ".join(args))
        data = _load()
        hits = []
        for uname, user in data["users"].items():
            for kind in VALID_KINDS:
                for name, qty in user.get(kind, {}).items():
                    if query in name:
                        hits.append(f"• {uname} ({kind}): {name} — {qty}")
        if not hits:
            await message.reply(f"Nobody has anything matching **{query}**.")
        else:
            await message.reply(f"**Holders of '{query}':**\n" + "\n".join(hits))
        return

    if cmd == "!set":
        # !set <account|store> <qty> <item name...>
        if len(args) < 3 or args[0].lower() not in VALID_KINDS:
            await message.reply("Usage: `!set <account|store> <qty> <item name>`")
            return
        kind = args[0].lower()
        try:
            qty = int(args[1])
        except ValueError:
            await message.reply("Quantity must be a number.")
            return
        name = normalize_item(" ".join(args[2:]))
        username = message.author.display_name
        async with _lock:
            data = _load()
            bucket = _user_bucket(data, username, kind)
            bucket[name] = qty
            _save(data)
        await message.reply(f"✅ Set **{username}** {kind}: {name} = {qty}")
        return

    if cmd == "!remove":
        # !remove <account|store> <item name...>
        if len(args) < 2 or args[0].lower() not in VALID_KINDS:
            await message.reply("Usage: `!remove <account|store> <item name>`")
            return
        kind = args[0].lower()
        name = normalize_item(" ".join(args[1:]))
        username = message.author.display_name
        async with _lock:
            data = _load()
            bucket = _user_bucket(data, username, kind)
            if name in bucket:
                del bucket[name]
                _save(data)
                await message.reply(f"🗑️ Removed {name} from **{username}** {kind}.")
            else:
                await message.reply(f"{name} not found in your {kind} inventory.")
        return

    if cmd == "!clear":
        if not args or args[0].lower() not in VALID_KINDS:
            await message.reply("Usage: `!clear <account|store>`")
            return
        kind = args[0].lower()
        username = message.author.display_name
        async with _lock:
            data = _load()
            user = data["users"].get(username)
            if user:
                user[kind] = {}
                _save(data)
        await message.reply(f"🧹 Cleared **{username}**'s {kind} inventory.")
        return

    await message.reply("Unknown command. Try `!help`.")


HELP_TEXT = (
    "**Grow a Garden 2 Inventory Bot**\n"
    "Post an inventory screenshot (add the word `store` to file it under the store "
    "inventory; otherwise it goes to your account inventory). I'll read it with AI vision.\n\n"
    "**Commands**\n"
    "`!inv [@user] [account|store]` — show an inventory\n"
    "`!accounts` — everyone's account items, combined\n"
    "`!store` — everyone's store items, combined\n"
    "`!who <item>` — who has an item\n"
    "`!set <account|store> <qty> <item>` — set/correct a quantity\n"
    "`!remove <account|store> <item>` — remove an item\n"
    "`!clear <account|store>` — wipe your own inventory\n"
)


# ---------------------------------------------------------------------------
# Keep-alive web server (for hosts like Render / Replit that require an open
# port, or that "sleep" a service unless something answers HTTP). Railway does
# not need this, but it's harmless there. It starts only if PORT is set or
# KEEP_ALIVE=1.
# ---------------------------------------------------------------------------
class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Garden inventory bot is alive.")

    def log_message(self, *_args):  # silence request logging
        pass


def start_keep_alive():
    port = os.getenv("PORT")
    if not port and os.getenv("KEEP_ALIVE") != "1":
        return
    port = int(port or 8080)
    server = HTTPServer(("0.0.0.0", port), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"Keep-alive web server listening on port {port}")


if __name__ == "__main__":
    start_keep_alive()
    client.run(DISCORD_TOKEN)

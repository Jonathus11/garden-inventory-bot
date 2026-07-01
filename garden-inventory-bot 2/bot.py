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

# Optional: a channel where fulfilled orders are posted. Messages there are parsed
# and deducted from total stock. Right-click the channel in Discord (Developer Mode
# on) -> Copy Channel ID, and set it as ORDERS_CHANNEL_ID in Railway.
try:
    ORDERS_CHANNEL_ID = int(os.getenv("ORDERS_CHANNEL_ID", "0") or "0")
except ValueError:
    ORDERS_CHANNEL_ID = 0

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


ORDER_PROMPT = (
    "Extract the items and quantities being ordered/sold from this text. Ignore "
    "prices, dollar amounts, currency, totals, and words like 'total'. Return STRICT "
    "JSON only: {\"items\": [{\"name\": \"moon bloom\", \"qty\": 50}]}. Lowercase names. "
    "If a line says e.g. '50 moon bloom-17.5$' that is qty 50 of moon bloom."
)


async def parse_order_text(text: str) -> dict:
    """Use the model to pull {item: qty} out of a messy order message."""
    def _call():
        return anthropic_client.messages.create(
            model=VISION_MODEL,
            max_tokens=800,
            messages=[{"role": "user", "content": f"{ORDER_PROMPT}\n\nORDER:\n{text}"}],
        )

    resp = await asyncio.to_thread(_call)
    out = "".join(b.text for b in resp.content if b.type == "text")
    match = re.search(r"\{.*\}", out, re.DOTALL)
    if not match:
        return {}
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}
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
    return result


def effective_stock(data: dict) -> dict:
    """Total store stock across all accounts, minus fulfilled-order deductions."""
    agg: dict[str, int] = {}
    for user in data["users"].values():
        for name, qty in user.get("store", {}).items():
            agg[name] = agg.get(name, 0) + qty
    for name, qty in data.get("deductions", {}).items():
        agg[name] = agg.get(name, 0) - qty
    return {n: max(v, 0) for n, v in agg.items()}


# ---------------------------------------------------------------------------
# Discord bot
# ---------------------------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True  # REQUIRED: enable in the Developer Portal too
client = discord.Client(intents=intents)


def detect_kind(text: str) -> str:
    low = text.lower()
    if "store" in low or "stock" in low:
        return "store"
    return "account"  # default


# Item -> emoji. Matched by substring (first hit wins), so "black dragon" and
# "dragon breath" both catch "dragon". Order matters: put specific before general.
_EMOJI_RULES = [
    ("ice serpent", "🐍"), ("serpent", "🐍"), ("venom", "🐍"), ("spitter", "🐍"),
    ("black dragon", "🐲"), ("dragon breath", "🌶️"), ("dragfly", "🪰"),
    ("dragonfly", "🪰"), ("dragon", "🐉"),
    ("ghost pepper", "👻"), ("pepper", "🌶️"),
    ("hypno", "🌀"), ("moon bloom", "🌙"), ("moon", "🌙"), ("bloom", "🌸"),
    ("venus", "🪤"), ("fly trap", "🪤"),
    ("rainbow", "🌈"), ("gold", "🪙"), ("mega", "💥"),
    ("super sprinkler", "💦"), ("legendary sprinkler", "💦"), ("sprinkler", "💦"),
    ("watering", "🚿"), ("can", "🚿"),
    ("raccoon", "🦝"), ("racoon", "🦝"), ("unicorn", "🦄"), ("bear", "🐻"),
    ("monkey", "🐵"), ("robin", "🐦"), ("owl", "🦉"), ("bee", "🐝"),
    ("deer", "🦌"), ("frog", "🐸"), ("bunny", "🐰"), ("cactus", "🌵"),
    ("corn", "🌽"), ("banana", "🍌"), ("cherry", "🍒"), ("grape", "🍇"),
    ("mango", "🥭"), ("coconut", "🥥"), ("acorn", "🌰"), ("pineapple", "🍍"),
    ("pomegranate", "🍎"), ("mushroom", "🍄"), ("sunflower", "🌻"),
    ("seed", "🌱"),
]


# Custom (picture) emojis built from the shop art in ./emojis. Keyword -> emoji
# file/name (without .png). Specific multi-word keys first so "black dragon" and
# "dragon breath" don't collide with a generic "dragon".
EMOJI_DIR = Path(__file__).with_name("emojis")
_CUSTOM_ITEMS = [
    ("black dragon", "black_dragon"), ("ice serpent", "ice_serpent"),
    ("dragon breath", "dragon_breath"), ("dragon fruit", "dragon_fruit"),
    ("ghost pepper", "ghost_pepper"), ("hypno", "hypno_bloom"),
    ("venom", "venom_spitter"), ("spitter", "venom_spitter"),
    ("venus", "venus_fly_trap"), ("fly trap", "venus_fly_trap"),
    ("dragfly", "dragonfly"), ("dragonfly", "dragonfly"),
    ("gold seed", "gold_seed"), ("gold", "gold_seed"),
    ("mega seed", "mega_seed"), ("mega", "mega_seed"),
    ("watering", "watering_can"), ("sheckle", "sheckles"),
    ("raccoon", "raccoon"), ("racoon", "raccoon"),
    ("unicorn", "unicorn"), ("bear", "bear"), ("robin", "robin"),
    ("owl", "owl"), ("bee", "bee"), ("deer", "deer"), ("frog", "frog"),
    ("bunny", "bunny"), ("cactus", "cactus"), ("corn", "corn"),
    ("banana", "banana"), ("cherry", "cherry"), ("grape", "grape"),
    ("mango", "mango"), ("coconut", "coconut"), ("acorn", "acorn"),
    ("pineapple", "pineapple"), ("pomegranate", "pomegranate"),
    ("mushroom", "mushroom"), ("sunflower", "sunflower"),
    ("green bean", "greenbean"), ("greenbean", "greenbean"),
]
# Filled at startup: emoji_name -> "<:name:id>" markdown once uploaded to a guild.
_custom_resolved: dict[str, str] = {}


def item_emoji(name: str) -> str:
    low = name.lower()
    # Prefer a real picture emoji if we have one uploaded.
    for key, ename in _CUSTOM_ITEMS:
        if key in low and ename in _custom_resolved:
            return _custom_resolved[ename]
    for key, emo in _EMOJI_RULES:
        if key in low:
            return emo
    return "📦"


async def ensure_emojis():
    """Upload bundled item pictures as custom server emojis (once), then map them.
    Needs the 'Manage Expressions' permission; if missing, we silently fall back
    to unicode emojis."""
    if not EMOJI_DIR.is_dir():
        return
    files = sorted(EMOJI_DIR.glob("*.png"))
    for guild in client.guilds:
        existing = {e.name: e for e in guild.emojis}
        for f in files:
            ename = f.stem
            if ename in existing:
                _custom_resolved.setdefault(ename, str(existing[ename]))
                continue
            if len(guild.emojis) >= getattr(guild, "emoji_limit", 50):
                continue  # server is full; leave the rest on unicode
            try:
                created = await guild.create_custom_emoji(name=ename, image=f.read_bytes())
                _custom_resolved.setdefault(ename, str(created))
                existing[ename] = created
            except discord.Forbidden:
                print("[emoji] Missing 'Manage Expressions' permission — using unicode.", flush=True)
                break
            except Exception as e:  # noqa: BLE001
                print(f"[emoji] failed to upload {ename}: {e}", flush=True)
    print(f"[emoji] {len(_custom_resolved)} custom item emojis ready.", flush=True)


async def _send_chunks(message, header: str, blocks: list) -> None:
    """Send blocks joined under a header, splitting to respect Discord's 2000 cap."""
    chunk = header
    for block in blocks:
        piece = block + "\n\n"
        if len(chunk) + len(piece) > 1900:
            await message.reply(chunk.rstrip())
            chunk = ""
        chunk += piece
    if chunk.strip():
        await message.reply(chunk.rstrip())


def fmt_inventory(inv: dict) -> str:
    if not inv:
        return "_(empty)_"
    lines = [f"{item_emoji(name)} {name} — **{qty}**" for name, qty in sorted(inv.items())]
    return "\n".join(lines)


@client.event
async def on_ready():
    print(f"Logged in as {client.user} (id: {client.user.id})")
    try:
        await ensure_emojis()
    except Exception as e:  # noqa: BLE001
        print(f"[emoji] setup skipped: {e}", flush=True)
    if ORDERS_CHANNEL_ID:
        print(f"Watching orders channel {ORDERS_CHANNEL_ID} for stock deductions.")
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

    # --- Fulfilled orders -> deduct from stock ------------------------------
    if ORDERS_CHANNEL_ID and message.channel.id == ORDERS_CHANNEL_ID:
        await handle_order(message)
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


async def handle_order(message: discord.Message):
    """A message in the orders channel = a fulfilled order. Parse its items
    (from text and/or screenshots) and deduct them from total stock."""
    images = [
        a for a in message.attachments
        if (a.content_type or "").startswith("image/")
        or a.filename.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))
    ]
    ordered: dict[str, int] = {}

    async with message.channel.typing():
        # Text part of the order.
        text = message.content.strip()
        if text:
            try:
                for n, q in (await parse_order_text(text)).items():
                    ordered[n] = ordered.get(n, 0) + q
            except Exception as e:  # noqa: BLE001
                print(f"[order text error] {e}", flush=True)
        # Image part(s) of the order.
        for att in images:
            raw = await att.read()
            mt = att.content_type or "image/png"
            if mt not in ("image/png", "image/jpeg", "image/webp", "image/gif"):
                mt = "image/png"
            try:
                _u, items = await read_inventory_from_image(raw, mt)
                for n, q in items.items():
                    ordered[n] = ordered.get(n, 0) + q
            except Exception as e:  # noqa: BLE001
                print(f"[order image error] {e}", flush=True)

    if not ordered:
        await message.add_reaction("❓")
        return

    async with _lock:
        data = _load()
        ded = data.setdefault("deductions", {})
        for n, q in ordered.items():
            ded[n] = ded.get(n, 0) + q
        _save(data)
        remaining = effective_stock(data)

    lines = []
    for n, q in sorted(ordered.items()):
        left = remaining.get(n, 0)
        flag = "  ⚠️ **OUT**" if left <= 0 else (f"  ⚠️ low" if left <= 10 else "")
        lines.append(f"{item_emoji(n)} {n} −{q}  →  {left} left{flag}")
    await message.reply("🧾 **Order logged. Stock updated:**\n" + "\n".join(lines))


def _norm_kind(token: str) -> str:
    """Map a user-typed kind token to a bucket name; 'stock' -> 'store'."""
    t = token.lower()
    if t in ("store", "stock"):
        return "store"
    if t == "account":
        return "account"
    return ""


async def handle_command(message: discord.Message, content: str):
    parts = content.split()
    cmd = parts[0].lower()
    args = parts[1:]

    if cmd in ("!help", "!commands"):
        await message.reply(HELP_TEXT)
        return

    if cmd in ("!inv", "!accountslist"):
        # !inv                       -> list every tracked account
        # !inv <name> [account|store] -> show that account's inventory
        data = _load()
        kind = "account"
        name_args = []
        for a in args:
            if a.lower() in VALID_KINDS or a.lower() == "stock":
                kind = "store" if a.lower() in ("store", "stock") else "account"
            else:
                name_args.append(a)

        # No account name given -> list all tracked accounts.
        if not name_args and not message.mentions:
            names = sorted(data["users"].keys())
            if not names:
                await message.reply("No accounts tracked yet. Post an inventory screenshot to start.")
            else:
                listing = "\n".join(f"• {n}" for n in names)
                await message.reply(
                    f"**Tracked accounts ({len(names)}):**\n{listing}\n"
                    "Use `!inv <name> [account|store]` to see one."
                )
            return

        target = message.mentions[0].display_name if message.mentions else " ".join(name_args)
        # Case-insensitive match against tracked game usernames.
        user = data["users"].get(target)
        if user is None:
            for k in data["users"]:
                if k.lower() == target.lower():
                    target, user = k, data["users"][k]
                    break
        if not user:
            await message.reply(f"No inventory tracked for **{target}** yet.")
            return
        label = "stock" if kind == "store" else "account"
        await message.reply(
            f"**{target}** — {label}:\n{fmt_inventory(user.get(kind, {}))}"
        )
        return

    if cmd in ("!store", "!stock"):
        # Everything combined across all accounts, minus fulfilled orders.
        data = _load()
        stock = effective_stock(data)
        sold = sum(data.get("deductions", {}).values())
        note = f"\n_({sold} items sold since last recount — `!sold` for details)_" if sold else ""
        await message.reply(
            f"**📦 Total stock (everyone combined):**\n{fmt_inventory(stock)}{note}"
        )
        return

    if cmd == "!sold":
        # Items deducted by fulfilled orders since the last recount.
        data = _load()
        ded = data.get("deductions", {})
        if not ded:
            await message.reply("No orders logged since the last recount.")
        else:
            await message.reply(
                "🧾 **Sold since last recount:**\n" + fmt_inventory(ded)
            )
        return

    if cmd in ("!recount", "!resetstock"):
        # Start a fresh inventory check: wipe all stock snapshots + the sold ledger,
        # so the next round of stock screenshots becomes the new source of truth.
        async with _lock:
            data = _load()
            for user in data["users"].values():
                user["store"] = {}
            data["deductions"] = {}
            _save(data)
        await message.reply(
            "🔄 **Stock reset for a fresh inventory check.** Sold-counter zeroed and all "
            "stock cleared. Now re-post each account's stock screenshots (include the word "
            "`stock`), and orders will deduct from the new totals."
        )
        return

    if cmd == "!accounts":
        # Each account and the items it holds.
        data = _load()
        users = data["users"]
        if not users:
            await message.reply("No accounts tracked yet. Post an inventory screenshot to start.")
            return
        blocks = []
        for name in sorted(users, key=str.lower):
            u = users[name]
            acct = u.get("account", {})
            stock = u.get("store", {})
            lines = [f"__**{name}**__"]
            if acct:
                lines.append("• " + ",  ".join(
                    f"{item_emoji(n)} {n} ×{q}" for n, q in sorted(acct.items())))
            if stock:
                lines.append("• _stock:_ " + ",  ".join(
                    f"{item_emoji(n)} {n} ×{q}" for n, q in sorted(stock.items())))
            if not acct and not stock:
                lines.append("_(empty)_")
            blocks.append("\n".join(lines))
        # Discord has a 2000-char message cap; send in chunks if needed.
        header = f"**👥 Accounts ({len(users)}):**\n"
        await _send_chunks(message, header, blocks)
        return

    if cmd == "!low":
        # !low [threshold]  -> stock items at or below threshold (default 10)
        try:
            threshold = int(args[0]) if args else 10
        except ValueError:
            threshold = 10
        data = _load()
        agg: dict[str, int] = {}
        for user in data["users"].values():
            for name, qty in user.get("store", {}).items():
                agg[name] = agg.get(name, 0) + qty
        low = {n: q for n, q in agg.items() if q <= threshold}
        if not low:
            await message.reply(f"✅ Nothing at or below {threshold} in stock.")
        else:
            await message.reply(
                f"⚠️ **Low stock (≤ {threshold}):**\n{fmt_inventory(low)}"
            )
        return

    if cmd in ("!who", "!find"):
        if not args:
            await message.reply("Usage: `!find <item name>`")
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
        # !set <account|stock> <qty> <item name...>  (also accepts a leading @name/name)
        kind = _norm_kind(args[0]) if args else ""
        if len(args) < 3 or not kind:
            await message.reply("Usage: `!set <account|stock> <qty> <item name>`")
            return
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
        await message.reply(f"✅ Set **{username}** {kind}: {item_emoji(name)} {name} = {qty}")
        return

    if cmd == "!remove":
        # !remove <account|stock> <item name...>
        kind = _norm_kind(args[0]) if args else ""
        if len(args) < 2 or not kind:
            await message.reply("Usage: `!remove <account|stock> <item name>`")
            return
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
        kind = _norm_kind(args[0]) if args else ""
        if not kind:
            await message.reply("Usage: `!clear <account|stock>`")
            return
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
    "Post an inventory screenshot — I read the in-game username off the leaderboard "
    "and the items with AI vision. Add the word `stock` (or `store`) in the message to "
    "file it as sellable stock; otherwise it's tracked as that account's inventory.\n\n"
    "**Commands**\n"
    "`!inv` — list every tracked account\n"
    "`!inv <name> [account|stock]` — show one account's inventory\n"
    "`!accounts` — each account and the items it holds\n"
    "`!stock` — total stock combined (minus fulfilled orders)\n"
    "`!low [n]` — stock at or below n (default 10)\n"
    "`!find <item>` — who has an item\n"
    "`!sold` — items sold since the last recount\n"
    "`!recount` — reset stock for a fresh inventory check\n"
    "`!set <account|stock> <qty> <item>` — set/correct a quantity (uses your name)\n"
    "`!remove <account|stock> <item>` — remove an item\n"
    "`!clear <account|stock>` — wipe your own inventory\n\n"
    "**Orders:** post fulfilled orders (text or screenshot) in the orders channel and "
    "I'll deduct them from total stock automatically."
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

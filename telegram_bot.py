#!/usr/bin/env python3
"""
Anna's Archive Telegram Bot
Multi-user, with download + upload progress bars, admin controls,
configurable search modes, welcome messages, and group/PM routing.
"""

import asyncio
import logging
import os
import re
import json
import time
import urllib.request
import uuid
from pathlib import Path
from html.parser import HTMLParser
from urllib.parse import urljoin
from concurrent.futures import ThreadPoolExecutor

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ChatMemberAdministrator,
    ChatMemberOwner,
)
from telegram.error import NetworkError, TimedOut, BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
from bs4 import BeautifulSoup

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.WARNING,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
C_DIR = os.path.dirname(os.path.realpath(__file__))


def _load_env_file():
    env_path = os.path.join(C_DIR, ".env")
    if not os.path.isfile(env_path):
        return
    try:
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    except Exception as e:
        print(f"[env] Failed to load .env: {e}")


_load_env_file()

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")


def _parse_env_owner_ids():
    raw = (os.environ.get("OWNER_IDS", "") or os.environ.get("OWNER_ID", "")).strip()
    ids = []
    for part in raw.replace(",", " ").split():
        if part.isdigit():
            ids.append(int(part))
    return ids


ENV_OWNER_IDS = _parse_env_owner_ids()

# MTProto credentials (my.telegram.org) used to backfill chat history for /index.
TG_API_ID = os.environ.get("TG_API_ID", "")
TG_API_HASH = os.environ.get("TG_API_HASH", "")

# Optional MongoDB persistence (config + PM users). Disabled when MONGO_URI is not set.
MONGO_URI = os.environ.get("MONGO_URI", "")
mongo_client = None
settings_col = None
users_col = None
db_sources_col = None
indexed_col = None

if MONGO_URI:
    try:
        import pymongo
        mongo_client = pymongo.MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        db = mongo_client["annas_downloader"]
        settings_col = db["settings"]
        users_col = db["users"]
        db_sources_col = db["db_sources"]
        indexed_col = db["indexed_books"]
        try:
            indexed_col.create_index([("file_uid", pymongo.ASCENDING)], unique=True, sparse=True)
        except Exception:
            pass
        print("[mongodb] Connected successfully to Atlas cluster")
    except Exception as e:
        print(f"[mongodb] Error initializing connection: {e}")

CONFIG_PATH = os.path.join(C_DIR, "config.json")
PM_USERS_PATH = os.path.join(C_DIR, "pm_users.json")
DL_PATH = os.path.join(C_DIR, "assets")
os.makedirs(DL_PATH, exist_ok=True)

executor = ThreadPoolExecutor(max_workers=20)

# Semaphore to limit concurrent downloads (max 3 at once)
dl_semaphore = asyncio.Semaphore(3)

# In-memory per-user state:  user_id -> {"results": [...]}
user_sessions: dict = {}

# Background index jobs:  user_id -> asyncio.Task
_active_index_jobs: dict = {}

# Cached set of db-source chat ids so the chat gate can allow multiple db groups
# without hitting Mongo on every update.
_db_source_ids: set = set()
_db_source_ids_loaded = False

# Safety cap: max history batches per index run (100 messages each)
MAX_INDEX_BATCHES = 500

# Lazily-initialized Telethon client for reading chat history (MTProto).
_tg_client = None
_tg_client_lock = asyncio.Lock()

# ---------------------------------------------------------------------------
# Domain auto-discovery
# ---------------------------------------------------------------------------
DOMAIN_SOURCE = "https://shadowlibraries.github.io/DirectDownloads/AnnasArchive/"
FALLBACK_DOMAINS = [
    "https://annas-archive.gl",
    "https://annas-archive.pk",
    "https://annas-archive.gd",
    "https://annas-archive.org",
]
LIBGEN_SOURCE = "https://shadowlibraries.github.io/DirectDownloads/libgen/"
LIBGEN_FALLBACK_DOMAINS = [
    "https://libgen.li",
    "https://libgen.vg",
    "https://libgen.la",
    "https://libgen.bz",
    "https://libgen.gl",
]


class _LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            for name, value in attrs:
                if name == "href" and value:
                    self.links.append(value)


def _fetch_html_links(url: str) -> list:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; annadl/1.0)"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        html = resp.read().decode("utf-8", errors="replace")
    p = _LinkParser()
    p.feed(html)
    return p.links


def _test_domain(domain: str) -> bool:
    try:
        req = urllib.request.Request(
            domain + "/", headers={"User-Agent": "Mozilla/5.0 (compatible; annadl/1.0)"}
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            return resp.status < 400
    except Exception:
        return False


def _domain_works(base: str) -> bool:
    """Strict probe: the search page must be reachable and contain the results marker."""
    try:
        import http_client
        r = http_client.get(
            base.rstrip("/") + "/search?index=&page=1&sort=&src=lgli&display=&q=test",
            timeout=12,
            allow_redirects=True,
        )
        return r.status_code == 200 and "js-aarecord-list-outer" in r.text
    except Exception:
        return False


def _discover_annas_domains() -> list:
    """Ordered, deduped candidate Anna's Archive domains for self-healing.

    Sources, best first: cached config domain, current BASE_URL, the
    shadowlibraries DirectDownloads mirror list, the `domain` module resolver,
    and the static fallback list.
    """
    cands = []

    def _add(d):
        d = (d or "").strip().rstrip("/")
        if d.startswith("http") and d not in cands:
            cands.append(d)

    _add(_get_cfg_value("base_url"))
    cur = globals().get("BASE_URL")
    if cur:
        _add(cur)
    try:
        from domain import get_base_url as _resolve_base
        _add(_resolve_base())
    except Exception:
        pass
    try:
        for h in _fetch_html_links(DOMAIN_SOURCE):
            h = h.split("?")[0].rstrip("/")
            host_path = h.split("//", 1)[-1]
            if (
                h.startswith("http")
                and "annas-archive" in h
                and "shadowlibraries" not in h
                and "/" not in host_path
            ):
                _add(h)
    except Exception:
        pass
    for d in FALLBACK_DOMAINS:
        _add(d)
    return cands


def get_active_domain() -> str:
    # Anna's Archive is no longer used for search/download (Libgen is), so the
    # domain is only a cosmetic fallback. Avoid slow network probes at startup.
    cfg = _load_cfg()
    cached = cfg.get("base_url", "").rstrip("/")
    if cached:
        return cached
    return FALLBACK_DOMAINS[0]


def _discover_libgen_domains() -> list:
    """Ordered, deduped candidate Libgen domains for self-healing.

    Sources, best first: cached config domain, current LIBGEN_BASE, the
    shadowlibraries DirectDownloads libgen mirror list, and the static
    fallback list.
    """
    cands = []

    def _add(d):
        d = (d or "").strip().rstrip("/")
        if d.startswith("http") and d not in cands:
            cands.append(d)

    _add(_get_cfg_value("libgen_base_url"))
    cur = globals().get("LIBGEN_BASE")
    if cur:
        _add(cur)
    try:
        for h in _fetch_html_links(LIBGEN_SOURCE):
            h = h.split("?")[0].rstrip("/")
            host_path = h.split("//", 1)[-1]
            if (
                h.startswith("http")
                and "libgen" in h
                and "shadowlibraries" not in h
                and "/" not in host_path
            ):
                _add(h)
    except Exception:
        pass
    for d in LIBGEN_FALLBACK_DOMAINS:
        _add(d)
    return cands


def get_active_libgen_domain() -> str:
    cfg = _load_cfg()
    cached = cfg.get("libgen_base_url", "").rstrip("/")
    if cached and _test_domain(cached):
        return cached
    for d in _discover_libgen_domains():
        if d == cached:
            continue
        if _test_domain(d):
            cfg["libgen_base_url"] = d
            _save_cfg(cfg)
            return d
    return LIBGEN_FALLBACK_DOMAINS[0]


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

_cfg_cache = {}

def _init_cfg():
    global _cfg_cache
    local_cfg = {}
    if Path(CONFIG_PATH).is_file():
        try:
            with open(CONFIG_PATH) as f:
                local_cfg = json.load(f)
        except Exception:
            pass

    db_cfg = {}
    if settings_col is not None:
        try:
            doc = settings_col.find_one({"_id": "global_config"})
            if doc:
                db_cfg = {k: v for k, v in doc.items() if k != "_id"}
                print("[mongodb] Loaded config from database")
        except Exception as e:
            print(f"[mongodb] Error loading config from database: {e}")

    merged = {
        "owner_ids": [],
        "service_enabled": True,
        "search_mode": "all",
        "delivery_mode": "group",
        "read_button": True,
        "welcome": {},
        "auto_delete": False,
        "delete_time": 120,
        "dump_channel": None,
        "pm_search": True,
        "pm_enabled": True,
        "no_result_msg": None,
    }
    merged.update(local_cfg)
    merged.update(db_cfg)
    _cfg_cache = merged

    try:
        with open(CONFIG_PATH, "w") as f:
            json.dump(_cfg_cache, f, indent=2)
    except Exception:
        pass

    if settings_col is not None:
        try:
            settings_col.replace_one({"_id": "global_config"}, _cfg_cache, upsert=True)
            print("[mongodb] Synced local & remote config")
        except Exception as e:
            print(f"[mongodb] Error syncing config to database on init: {e}")

_init_cfg()

def _load_cfg() -> dict:
    global _cfg_cache
    return _cfg_cache


def _save_cfg(cfg: dict):
    global _cfg_cache
    _cfg_cache = cfg
    try:
        with open(CONFIG_PATH, "w") as f:
            json.dump(_cfg_cache, f, indent=2)
    except Exception:
        pass

    if settings_col is not None:
        def _bg_save():
            try:
                settings_col.replace_one({"_id": "global_config"}, cfg, upsert=True)
            except Exception as e:
                logging.error(f"[mongodb] Error saving config in background: {e}")
        executor.submit(_bg_save)


def _get_cfg_value(key: str, default=None):
    return _load_cfg().get(key, default)


def _set_cfg_value(key: str, value):
    cfg = dict(_load_cfg())
    cfg[key] = value
    _save_cfg(cfg)


# PM User Tracking Cache and Helpers
_pm_users_cache = {}

def _init_pm_users():
    global _pm_users_cache
    if Path(PM_USERS_PATH).is_file():
        try:
            with open(PM_USERS_PATH) as f:
                data = json.load(f)
                _pm_users_cache = {int(k): v for k, v in data.items()}
        except Exception:
            pass

def _save_pm_users_local():
    global _pm_users_cache
    try:
        with open(PM_USERS_PATH, "w") as f:
            json.dump(_pm_users_cache, f, indent=2)
    except Exception:
        pass

_init_pm_users()

def _db_register_pm_user(user_id: int, username: str, first_name: str):
    global _pm_users_cache
    _pm_users_cache[user_id] = True
    _save_pm_users_local()
    if users_col is not None:
        try:
            users_col.update_one(
                {"user_id": user_id},
                {
                    "$set": {
                        "user_id": user_id,
                        "username": username or "",
                        "first_name": first_name or "",
                        "started_at": time.time()
                    }
                },
                upsert=True
            )
        except Exception as e:
            logging.error(f"[mongodb] Error registering user: {e}")

def _db_has_started_pm(user_id: int) -> bool:
    global _pm_users_cache
    if user_id in _pm_users_cache:
        return _pm_users_cache[user_id]

    if users_col is not None:
        try:
            doc = users_col.find_one({"user_id": user_id})
            if doc:
                _pm_users_cache[user_id] = True
                return True
        except Exception as e:
            logging.error(f"[mongodb] Error checking user in DB: {e}")

    return False

async def _register_pm_user(user_id: int, username: str, first_name: str):
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _db_register_pm_user, user_id, username, first_name)

async def _has_started_pm(user_id: int) -> bool:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _db_has_started_pm, user_id)

def _register_pm_if_private(update: Update):
    if update and update.effective_chat and update.effective_chat.type == "private":
        user = update.effective_user
        if user:
            executor.submit(_db_register_pm_user, user.id, user.username or "", user.first_name or "")


BASE_URL = get_active_domain()
LIBGEN_BASE = get_active_libgen_domain()
print(f"[domain] {BASE_URL}")
print(f"[libgen] {LIBGEN_BASE}")

# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------

def make_bar(current: int, total: int, width: int = 18) -> str:
    if total <= 0:
        return "⏳ …"
    pct = current / total
    filled = int(width * pct)
    bar = "█" * filled + "░" * (width - filled)
    done_mb = current / 1_048_576
    tot_mb = total / 1_048_576
    return f"[{bar}] {pct*100:.1f}%  ({done_mb:.1f} / {tot_mb:.1f} MB)"


def _esc(text: str) -> str:
    """Escape Telegram Markdown v1 special characters in dynamic content."""
    for ch in ("_", "*", "`", "["):
        text = text.replace(ch, f"\\{ch}")
    return text


# ---------------------------------------------------------------------------
# Admin / state helpers
# ---------------------------------------------------------------------------

def _get_owner_ids():
    owner_ids = _get_cfg_value("owner_ids", []) or []
    if isinstance(owner_ids, (int, str)):
        owner_ids = [owner_ids]
    ids = set()
    for oid in owner_ids:
        try:
            ids.add(int(str(oid).strip()))
        except (ValueError, TypeError):
            pass
    ids.update(ENV_OWNER_IDS)
    return ids


async def _is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user_id = update.effective_user.id
    if user_id in _get_owner_ids():
        return True
    chat = update.effective_chat
    if chat and chat.type in ("group", "supergroup"):
        try:
            member = await context.bot.get_chat_member(chat.id, user_id)
            return isinstance(member, (ChatMemberAdministrator, ChatMemberOwner))
        except Exception:
            pass
    return False


def _is_service_enabled() -> bool:
    return _get_cfg_value("service_enabled", True)


def _get_search_mode() -> str:
    """Returns: all | slash | hashtag | text"""
    return _get_cfg_value("search_mode", "all")


def _get_delivery_mode() -> str:
    """Returns: pm | group"""
    return _get_cfg_value("delivery_mode", "group")


def _get_read_button() -> bool:
    return _get_cfg_value("read_button", True)


def _get_connected_group():
    val = _get_cfg_value("connected_group_id")
    return int(val) if val else None


def _is_chat_allowed(update: Update, allow_connect: bool = False) -> bool:
    if not update or not update.effective_chat:
        return True
    chat = update.effective_chat
    if chat.type in ("group", "supergroup"):
        if not _db_source_ids_loaded:
            _load_db_source_ids()
        if chat.id in _db_source_ids:
            return True
        connected = _get_connected_group()
        if connected is None:
            return allow_connect
        return (chat.id == connected) or allow_connect
    return True


# ---------------------------------------------------------------------------
# Sync worker: search
# ---------------------------------------------------------------------------

def _sync_search(query: str, count: int = 10) -> list:
    query = (query or "").strip()
    if not query:
        return []

    # Direct MD5 lookup (deep links / fallback resolution by hash).
    if re.fullmatch(r"[a-fA-F0-9]{32}", query):
        md5 = query.lower()
        try:
            from book_info import fetch_book_info
            info = fetch_book_info(md5)
            if info and info.get("book_name"):
                return [
                    dict(
                        url=f"https://libgen.li/md5/{md5}",
                        title=info.get("book_name") or "Unknown",
                        author=info.get("author") or "Unknown",
                        year=info.get("year") or "Unknown",
                        language="Unknown",
                        format="Unknown",
                        size="Unknown",
                        md5=md5,
                    )
                ]
        except Exception:
            pass
        return []

    from libgen_search import search_libgen
    try:
        books = search_libgen(query, page=1)
    except Exception:
        books = []

    results = []
    for b in books:
        if len(results) >= count:
            break
        md5 = (b.get("md5") or "").lower()
        if not md5:
            continue
        results.append(
            dict(
                url=f"https://libgen.li/md5/{md5}",
                title=b.get("title") or "Unknown",
                author=b.get("author") or "Unknown",
                year=b.get("year") or "Unknown",
                language=b.get("language") or "Unknown",
                format=(b.get("format") or "Unknown").upper(),
                size=b.get("size") or "Unknown",
                md5=md5,
            )
        )
    return results


# ---------------------------------------------------------------------------
# Sync worker: resolve libgen direct download link
# ---------------------------------------------------------------------------

def _sync_get_direct_url(book_url: str) -> str:
    global LIBGEN_BASE
    md5_m = re.search(r"/md5/([a-fA-F0-9]{32})", book_url)
    if not md5_m:
        raise ValueError("Could not extract MD5 from book URL")
    md5 = md5_m.group(1)

    from libgen_li_handler import get_libgen_li_direct_link

    ads_url = f"{LIBGEN_BASE.rstrip('/')}/ads.php?md5={md5}"
    direct_url = get_libgen_li_direct_link(ads_url, timeout=25)
    if not direct_url:
        raise ValueError("Could not find get.php link in libgen ads page")

    # Self-heal the active domain to whichever mirror actually served the link.
    host = re.match(r"https?://([^/]+)", direct_url)
    if host:
        new_base = f"https://{host.group(1)}"
        if new_base != LIBGEN_BASE:
            LIBGEN_BASE = new_base
            try:
                cfg = _load_cfg()
                cfg["libgen_base_url"] = new_base
                _save_cfg(cfg)
            except Exception:
                pass
            print(f"[libgen] Self-healed: Updated active Libgen domain to: {new_base}")

    return direct_url


# ---------------------------------------------------------------------------
# Sync worker: stream download with progress callback
# ---------------------------------------------------------------------------

def _sync_download(url: str, dest_dir: str, on_progress=None) -> tuple:
    from urllib.parse import urlparse as _urlparse
    _origin = _urlparse(url)
    _referer = f"{_origin.scheme}://{_origin.netloc}/"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; annadl/1.0)",
            "Referer": _referer,
        },
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        total = int(resp.headers.get("Content-Length", 0))
        cd = resp.headers.get("Content-Disposition", "")
        m = re.search(r'filename[^;=\n]*=[ ]*["\']?([^"\';\n]+)', cd)
        fname = (
            m.group(1).strip().strip('"').strip("'")
            if m
            else (url.split("?")[0].rstrip("/").split("/")[-1] or "download")
        )

        name_part, ext_part = os.path.splitext(fname)
        truncated_name = name_part[:150] + ext_part
        dest_fname = f"{uuid.uuid4().hex}_{truncated_name}"
        dest = os.path.join(dest_dir, dest_fname)
        downloaded = 0
        last_pct = -1

        with open(dest, "wb") as out:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                out.write(chunk)
                downloaded += len(chunk)
                if on_progress and total:
                    pct = int(downloaded / total * 100)
                    if pct != last_pct and pct % 5 == 0:
                        last_pct = pct
                        on_progress(downloaded, total)

    return dest, fname


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _register_pm_if_private(update)

    if context.args and len(context.args) > 0:
        arg = context.args[0].strip()
        match = re.match(r'^md5_?([a-fA-F0-9]{32})$', arg, re.IGNORECASE)
        if match:
            if not _is_service_enabled():
                await update.message.reply_text("🔴 Bot service is currently offline.")
                return
            md5 = match.group(1)
            user = update.effective_user
            if user:
                await _register_pm_user(user.id, user.username or "", user.first_name or "")

            await _process_md5_download(md5, update, context)
            return

    cfg = _load_cfg()
    welcome = cfg.get("welcome", {})
    welcome_text = welcome.get("text", "")
    welcome_photo = welcome.get("photo_file_id", None)

    if not welcome_text:
        welcome_text = (
            "📚 *Anna's Archive Bot*\n\n"
            "Send me a book title or author name to search\\.\n"
            "Example: `Harry Potter`\n\n"
            "Use /help to see all available commands\\."
        )

    try:
        if welcome_photo:
            await update.message.reply_photo(
                photo=welcome_photo,
                caption=welcome_text,
                parse_mode="Markdown",
            )
        else:
            await update.message.reply_text(welcome_text, parse_mode="Markdown")
    except Exception:
        await update.message.reply_text(welcome_text)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _register_pm_if_private(update)
    search_mode = _get_search_mode()
    delivery_mode = _get_delivery_mode()
    service = "✅ online" if _is_service_enabled() else "🔴 offline"
    connected = _get_connected_group()
    connected_str = f"`{connected}`" if connected else "None"

    auto_delete = _get_cfg_value("auto_delete", False)
    auto_delete_str = "✅ enabled" if auto_delete else "❌ disabled"

    delete_time = _get_cfg_value("delete_time", 120)
    if delete_time >= 3600 and delete_time % 3600 == 0:
        delete_time_str = f"{delete_time // 3600}h"
    elif delete_time >= 60 and delete_time % 60 == 0:
        delete_time_str = f"{delete_time // 60}m"
    else:
        delete_time_str = f"{delete_time}s"

    dump_channel = _get_cfg_value("dump_channel")
    dump_channel_str = f"`{dump_channel}`" if dump_channel else "None"

    pm_search = _get_cfg_value("pm_search", True)
    pm_search_str = "✅ enabled" if pm_search else "❌ disabled"

    mode_desc = {
        "all": "All modes active",
        "slash": "Only /search command",
        "hashtag": "Only #request prefix",
        "text": "Only plain text messages",
    }.get(search_mode, search_mode)

    user_text = (
        "📚 *Anna's Archive Bot — Help*\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "*🔍 Search*\n"
        "• `/search <query>` — slash command search\n"
        "• `#request <book>` — hashtag search \\(groups\\)\n"
        "• `#bookrequest <book>` / `#Requestion <book>` — alternate hashtags\n"
        "• Plain text — send book name directly\n"
        "• `/md5_<md5>` — download a book by its MD5 link\n"
    )

    is_admin = await _is_admin(update, context)

    if is_admin:
        admin_text = (
            "\n━━━━━━━━━━━━━━━━━━━━\n"
            "*⚙️ Admin Commands*\n"
            "• `/mode pm|group` — set file delivery target\n"
            "• `/mode search slash|hashtag|text|all` — search mode\n"
            "• `/connect` — register this group as delivery target\n"
            "• `/disconnect` — disconnect the connected group\n"
            "• `/setwelcome <text>|off` — set or disable welcome message\n"
            "  _Reply to a photo to include an image_\n"
            "• `/service on|off` — enable or disable the bot\n"
            "• `/auto_delete on|off` — enable/disable auto-delete in PM\n"
            "• `/deletetime <time>` — set delete delay \\(e.g., `2m`, `120s`, `1h`\\)\n"
            "• `/pm_search on|off` — enable/disable searching in PM\n"
            "• `/enablepm|/disablepm` — accept/ignore private messages\n"
            "• `/read on|off` — enable/disable the read button setting\n"
            "• `/dump <channel_id>|off` — set or disconnect dump destination\n"
            "• `/broadcast <message>` — broadcast to all PM users\n"
            "  _Reply to any message to copy/forward it_\n"
            "• `/status` — show full status summary\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "*🗄️ Database Source*\n"
            "• `/connectdb` — connect this chat as a book database source\n"
            "• `/index <start_url> <last_url>` — index a range of posts \\[or run `/index` alone for a guided flow\\]\n"
            "• `/dbsearch <query>` — search only the indexed database\n"
            "• `/showdbs` — list connected sources and their book counts\n"
            "• `/removedb this\\|chat_id\\|title [--wipe]` — remove a source\n"
            "  _New files posted in a source chat are auto-indexed_\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "*📊 Current Status*\n"
            f"• Service: {service}\n"
            f"• Delivery mode: `{delivery_mode}`\n"
            f"• Search mode: `{search_mode}` — {mode_desc}\n"
            f"• PM searching: `{pm_search_str}`\n"
            f"• Connected group: {connected_str}\n"
            f"• Auto-delete in PM: `{auto_delete_str}`\n"
            f"• Delete delay: `{delete_time_str}` \\({delete_time}s\\)\n"
            f"• Dump destination: {dump_channel_str}\n"
        )
        await update.message.reply_text(user_text + admin_text, parse_mode="Markdown")
    else:
        await update.message.reply_text(user_text, parse_mode="Markdown")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _is_admin(update, context):
        await update.message.reply_text("❌ You don't have permission to use this command.")
        return

    cfg = _load_cfg()
    service = "✅ online" if _is_service_enabled() else "🔴 offline"
    search_mode = _get_search_mode()
    delivery_mode = _get_delivery_mode()
    connected = _get_connected_group()
    connected_str = f"`{connected}`" if connected else "None"
    pm_enabled = cfg.get("pm_enabled", True)
    pm_search = cfg.get("pm_search", True)
    auto_delete = cfg.get("auto_delete", False)
    delete_time = cfg.get("delete_time", 120)
    dump_channel = cfg.get("dump_channel")
    dump_str = f"`{dump_channel}`" if dump_channel else "None"
    owner_str = ", ".join(f"`{o}`" for o in sorted(_get_owner_ids())) or "None"
    welcome = cfg.get("welcome", {})
    welcome_status = "✅ set" if (welcome.get("text") or welcome.get("photo_file_id")) else "❌ not set"
    if not welcome.get("enabled", True):
        welcome_status += " \\(disabled\\)"
    read_button = "✅ on" if _get_read_button() else "❌ off"
    pm_users = len(_pm_users_cache)

    await update.message.reply_text(
        "*📊 Bot Status*\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"• Service: {service}\n"
        f"• Delivery mode: `{delivery_mode}`\n"
        f"• Search mode: `{search_mode}`\n"
        f"• Owners: {owner_str}\n"
        f"• PM enabled: {'✅ on' if pm_enabled else '❌ off'}\n"
        f"• PM searching: {'✅ on' if pm_search else '❌ off'}\n"
        f"• Read button: {read_button}\n"
        f"• Connected group: {connected_str}\n"
        f"• Auto-delete in PM: {'✅ on' if auto_delete else '❌ off'}\n"
        f"• Delete delay: `{delete_time}s`\n"
        f"• Dump destination: {dump_str}\n"
        f"• Welcome message: {welcome_status}\n"
        f"• PM users registered: `{pm_users}`\n"
        f"• Domain: `{BASE_URL}`\n"
        f"• Libgen: `{LIBGEN_BASE}`",
        parse_mode="Markdown"
    )


async def cmd_read(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _is_admin(update, context):
        await update.message.reply_text("❌ You don't have permission to use this command.")
        return

    args = context.args
    if not args or args[0].lower() not in ("on", "off"):
        current = "on" if _get_read_button() else "off"
        await update.message.reply_text(
            f"Usage: `/read on|off`\nCurrent: `{current}`",
            parse_mode="Markdown"
        )
        return

    enabled = args[0].lower() == "on"
    _set_cfg_value("read_button", enabled)
    status = "✅ enabled" if enabled else "❌ disabled"
    await update.message.reply_text(
        f"📖 Read Online button: {status}",
        parse_mode="Markdown"
    )


async def cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _is_admin(update, context):
        await update.message.reply_text("❌ You don't have permission to use this command.")
        return

    args = context.args
    if not args:
        delivery = _get_delivery_mode()
        search = _get_search_mode()
        await update.message.reply_text(
            f"*Current modes:*\n"
            f"• Delivery: `{delivery}` — use `/mode pm` or `/mode group`\n"
            f"• Search: `{search}` — use `/mode search slash|hashtag|text|all`",
            parse_mode="Markdown"
        )
        return

    if args[0].lower() in ("pm", "group"):
        mode = args[0].lower()
        _set_cfg_value("delivery_mode", mode)
        icon = "💬" if mode == "pm" else "👥"
        await update.message.reply_text(
            f"{icon} Delivery mode set to: `{mode}`",
            parse_mode="Markdown"
        )
        return

    if args[0].lower() == "search":
        valid = ("slash", "hashtag", "text", "all")
        if len(args) < 2 or args[1].lower() not in valid:
            current = _get_search_mode()
            await update.message.reply_text(
                f"Usage: `/mode search slash|hashtag|text|all`\nCurrent: `{current}`\n\n"
                f"• `slash` — only `/search <query>`\n"
                f"• `hashtag` — only `#request <query>` in groups\n"
                f"• `text` — only plain text messages\n"
                f"• `all` — all three modes active",
                parse_mode="Markdown"
            )
            return
        search_mode = args[1].lower()
        _set_cfg_value("search_mode", search_mode)
        await update.message.reply_text(
            f"🔍 Search mode set to: `{search_mode}`",
            parse_mode="Markdown"
        )
        return

    await update.message.reply_text(
        "Usage:\n"
        "• `/mode pm|group` — delivery target\n"
        "• `/mode search slash|hashtag|text|all` — search mode",
        parse_mode="Markdown"
    )


async def cmd_set_search_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _is_admin(update, context):
        await update.message.reply_text("❌ You don't have permission to use this command.")
        return

    mode_map = {
        "all": "all",
        "request": "hashtag",
        "direct": "text",
        "command": "slash",
    }

    args = context.args
    if not args or args[0].lower() not in mode_map:
        current = _get_search_mode()
        await update.message.reply_text(
            f"Usage: `/setsearchmode <all|request|direct|command>`\n"
            f"Current search mode: `{current}`\n\n"
            f"• `all` — every search method works\n"
            f"• `request` — only #request / #bookrequest / #Requestion\n"
            f"• `direct` — any plain text is a search\n"
            f"• `command` — only /search works",
            parse_mode="Markdown"
        )
        return

    target = mode_map[args[0].lower()]
    _set_cfg_value("search_mode", target)
    await update.message.reply_text(
        f"🔍 Search mode set to: `{target}`",
        parse_mode="Markdown"
    )


async def cmd_connect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _is_admin(update, context):
        await update.message.reply_text("❌ You don't have permission to use this command.")
        return

    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text(
            "❌ `/connect` must be run inside a group chat.",
            parse_mode="Markdown"
        )
        return

    _set_cfg_value("connected_group_id", chat.id)
    await update.message.reply_text(
        f"✅ This group (*{_esc(chat.title or str(chat.id))}*) is now connected as the delivery target\\.\n\n"
        f"Use `/mode group` to route downloads here\\.",
        parse_mode="Markdown"
    )


async def cmd_disconnect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _is_admin(update, context):
        await update.message.reply_text("❌ You don't have permission to use this command.")
        return

    _set_cfg_value("connected_group_id", None)
    await update.message.reply_text(
        "🔌 Connected group has been disconnected\\.",
        parse_mode="Markdown"
    )


async def cmd_enable_pm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _is_admin(update, context):
        await update.message.reply_text("❌ You don't have permission to use this command.")
        return
    _set_cfg_value("pm_enabled", True)
    await update.message.reply_text("✅ Private messages are now *enabled*.", parse_mode="Markdown")


async def cmd_disable_pm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _is_admin(update, context):
        await update.message.reply_text("❌ You don't have permission to use this command.")
        return
    _set_cfg_value("pm_enabled", False)
    await update.message.reply_text("✅ Private messages are now *disabled* (searches in PM are ignored).", parse_mode="Markdown")


async def cmd_setwelcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _is_admin(update, context):
        await update.message.reply_text("❌ You don't have permission to use this command.")
        return

    text = " ".join(context.args).strip()
    photo_file_id = None

    replied = update.message.reply_to_message
    if replied and replied.photo:
        photo_file_id = replied.photo[-1].file_id

    if text.lower() == "off":
        cfg = _load_cfg()
        welcome = cfg.get("welcome", {})
        welcome["enabled"] = False
        cfg["welcome"] = welcome
        _save_cfg(cfg)
        await update.message.reply_text("🔌 Welcome message has been disabled.")
        return

    if not text and not photo_file_id:
        cfg = _load_cfg()
        welcome = cfg.get("welcome", {})
        current_text = welcome.get("text") or "(not set)"
        current_photo = "yes 🖼️" if welcome.get("photo_file_id") else "no"
        enabled_status = "✅ enabled" if welcome.get("enabled", True) else "❌ disabled"
        await update.message.reply_text(
            f"Usage: `/setwelcome Your welcome text here`\n"
            f"To disable welcome: `/setwelcome off`\n"
            f"To include a photo: reply to a photo with `/setwelcome Your text`\n\n"
            f"Current status: {enabled_status}\n"
            f"Current text: _{_esc(current_text)}_\n"
            f"Has photo: {current_photo}\n\n"
            f"You can use `{{name}}` as a placeholder for the new member's name\\.",
            parse_mode="Markdown"
        )
        return

    cfg = _load_cfg()
    welcome = cfg.get("welcome", {})
    welcome["enabled"] = True
    if text:
        welcome["text"] = text
    if photo_file_id:
        welcome["photo_file_id"] = photo_file_id
    cfg["welcome"] = welcome
    _save_cfg(cfg)

    photo_note = " \\(with photo 🖼️\\)" if photo_file_id else ""
    preview = text or welcome.get("text", "")
    await update.message.reply_text(
        f"✅ Welcome message updated{photo_note}:\n\n_{_esc(preview)}_",
        parse_mode="Markdown"
    )


async def cmd_noresult(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _is_admin(update, context):
        await update.message.reply_text("❌ You don't have permission to use this command.")
        return

    current = _get_cfg_value("no_result_msg")
    args = context.args

    if not args:
        current_line = f"`off` \\(silent\\)" if not current else f"_{_esc(current)}_"
        await update.message.reply_text(
            f"Usage: `/noresult <message>` to set the fail message shown when online search finds nothing.\n"
            f"`/noresult off` to disable it \\(silent\\).\n"
            f"You can use `{{query}}` as a placeholder for the user's search\\.\n\n"
            f"Current: {current_line}",
            parse_mode="Markdown"
        )
        return

    if args[0].lower() in ("off", "none", "disable"):
        _set_cfg_value("no_result_msg", None)
        await update.message.reply_text(
            "✅ Fail message *disabled*\\. Online searches with no results are silent again\\.",
            parse_mode="Markdown"
        )
        return

    text = " ".join(args).strip()
    _set_cfg_value("no_result_msg", text)
    await update.message.reply_text(
        f"✅ Fail message set\\. Will be shown when online search finds nothing:\n\n_{_esc(text)}_",
        parse_mode="Markdown"
    )


async def cmd_service(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _is_admin(update, context):
        await update.message.reply_text("❌ You don't have permission to use this command.")
        return

    args = context.args
    if not args or args[0].lower() not in ("on", "off"):
        current = "on" if _is_service_enabled() else "off"
        await update.message.reply_text(
            f"Usage: `/service on|off`\nCurrent: `{current}`",
            parse_mode="Markdown"
        )
        return

    enabled = args[0].lower() == "on"
    _set_cfg_value("service_enabled", enabled)
    status = "✅ Service is now *online*" if enabled else "🔴 Service is now *offline*"
    await update.message.reply_text(status, parse_mode="Markdown")


async def cmd_auto_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _is_admin(update, context):
        await update.message.reply_text("❌ You don't have permission to use this command.")
        return

    args = context.args
    if not args or args[0].lower() not in ("on", "off"):
        current = "on" if _get_cfg_value("auto_delete", False) else "off"
        await update.message.reply_text(
            f"Usage: `/auto_delete on|off`\nCurrent auto-delete setting: `{current}`",
            parse_mode="Markdown"
        )
        return

    enabled = args[0].lower() == "on"
    _set_cfg_value("auto_delete", enabled)
    status = "✅ Auto-delete in PM mode is now *enabled*" if enabled else "❌ Auto-delete in PM mode is now *disabled*"
    await update.message.reply_text(status, parse_mode="Markdown")


async def cmd_deletetime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _is_admin(update, context):
        await update.message.reply_text("❌ You don't have permission to use this command.")
        return

    args = context.args
    if not args:
        current_seconds = _get_cfg_value("delete_time", 120)
        if current_seconds >= 3600 and current_seconds % 3600 == 0:
            formatted = f"{current_seconds // 3600}h"
        elif current_seconds >= 60 and current_seconds % 60 == 0:
            formatted = f"{current_seconds // 60}m"
        else:
            formatted = f"{current_seconds}s"
        await update.message.reply_text(
            f"Usage: `/deletetime <time>` (e.g. `2m`, `120s`, `1h`)\nCurrent delete time: `{formatted}` ({current_seconds} seconds)",
            parse_mode="Markdown"
        )
        return

    time_str = args[0].strip().lower()
    match = re.match(r'^(\d+)\s*([smh]?)$', time_str)
    if not match:
        await update.message.reply_text(
            "❌ Invalid time format. Please use a number followed by `s` (seconds), `m` (minutes), or `h` (hours).\n"
            "Examples: `120s`, `2m`, `1h`",
            parse_mode="Markdown"
        )
        return

    value = int(match.group(1))
    unit = match.group(2) or "s"

    if unit == "h":
        seconds = value * 3600
    elif unit == "m":
        seconds = value * 60
    else:
        seconds = value

    if seconds <= 0:
        await update.message.reply_text("❌ Delete time must be greater than 0.")
        return

    _set_cfg_value("delete_time", seconds)

    if seconds >= 3600 and seconds % 3600 == 0:
        formatted = f"{seconds // 3600}h"
    elif seconds >= 60 and seconds % 60 == 0:
        formatted = f"{seconds // 60}m"
    else:
        formatted = f"{seconds}s"

    await update.message.reply_text(
        f"⏳ Delete time successfully set to: `{formatted}` ({seconds} seconds).",
        parse_mode="Markdown"
    )


async def cmd_dump(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _is_admin(update, context):
        await update.message.reply_text("❌ You don't have permission to use this command.")
        return

    args = context.args
    if not args:
        current = _get_cfg_value("dump_channel")
        await update.message.reply_text(
            f"Usage: `/dump <channel_id_or_group_id>` (e.g. `/dump -1001234567890`) or `/dump off` to disconnect\n"
            f"Current dump destination: `{current or 'None'}`",
            parse_mode="Markdown"
        )
        return

    target = args[0].strip()
    if target.lower() in ("off", "none", "disable", "disconnect"):
        _set_cfg_value("dump_channel", None)
        await update.message.reply_text(
            "🔌 Dump destination disconnected successfully.",
            parse_mode="Markdown"
        )
        return

    try:
        if target.startswith("-"):
            channel_id = int(target)
        else:
            channel_id = int(target) if target.isdigit() else target
    except ValueError:
        channel_id = target

    _set_cfg_value("dump_channel", channel_id)

    await update.message.reply_text(
        f"✅ Dump channel successfully set to: `{channel_id}`.\n\n"
        f"📚 Files sent/forwarded to the dump channel will now be *auto-indexed* "
        f"into the search database.\n"
        f"⚠️ **IMPORTANT REMINDER:**\n"
        f"Please make sure you have added the bot to this channel/group as an **Administrator** with permission to post/send messages and documents! "
        f"If the bot is not an admin, it will not be able to dump books there.",
        parse_mode="Markdown"
    )


async def cmd_pm_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _is_admin(update, context):
        await update.message.reply_text("❌ You don't have permission to use this command.")
        return

    args = context.args
    if not args or args[0].lower() not in ("on", "off"):
        current = "on" if _get_cfg_value("pm_search", True) else "off"
        await update.message.reply_text(
            f"Usage: `/pm_search on|off`\nCurrent PM search setting: `{current}`",
            parse_mode="Markdown"
        )
        return

    enabled = args[0].lower() == "on"
    _set_cfg_value("pm_search", enabled)
    status = "✅ Searching in PM is now *enabled*" if enabled else "❌ Searching in PM is now *disabled*"
    await update.message.reply_text(status, parse_mode="Markdown")


# ---------------------------------------------------------------------------
# Broadcast command (async non-blocking background task)
# ---------------------------------------------------------------------------

async def _run_broadcast_task(bot, sender_chat_id, sender_message_id, text, user_ids):
    success = 0
    failed = 0
    for uid in user_ids:
        try:
            if text:
                await bot.send_message(chat_id=uid, text=text, parse_mode="Markdown")
            else:
                await bot.copy_message(chat_id=uid, from_chat_id=sender_chat_id, message_id=sender_message_id)
            success += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1
    try:
        await bot.send_message(
            chat_id=sender_chat_id,
            text=f"📢 *Broadcast Completed*\n\n"
                 f"• Delivered to: `{success}` users\n"
                 f"• Failed/Blocked: `{failed}` users",
            parse_mode="Markdown"
        )
    except Exception:
        pass


async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _is_admin(update, context):
        await update.message.reply_text("❌ You don't have permission to use this command.")
        return

    text = " ".join(context.args).strip()
    replied = update.message.reply_to_message

    if not text and not replied:
        await update.message.reply_text(
            "Usage: `/broadcast Your message here`\n"
            "Or: reply to any message \\(text, photo, document, etc\\.\\) with `/broadcast` to mirror it\\.",
            parse_mode="Markdown"
        )
        return

    uids = list(_pm_users_cache.keys())
    if not uids:
        await update.message.reply_text("❌ No registered PM users found to broadcast to.")
        return

    await update.message.reply_text(
        f"📢 *Broadcast initiated in the background to {len(uids)} users\\.*",
        parse_mode="Markdown"
    )

    sender_chat_id = update.effective_chat.id
    sender_message_id = replied.message_id if replied else None

    asyncio.create_task(
        _run_broadcast_task(
            context.bot,
            sender_chat_id,
            sender_message_id,
            text,
            uids
        )
    )


# ---------------------------------------------------------------------------
# Database source: indexed Telegram channels/groups
# ---------------------------------------------------------------------------

async def _delayed_delete(bot, chat_id: int, message_id: int, delay: int):
    try:
        await asyncio.sleep(delay)
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception as e:
        logging.error(f"[auto_delete] Failed to delete message {message_id} in {chat_id}: {e}")


def _load_db_source_ids():
    """Refresh the cached set of db-source chat ids (sync; call at startup/connect/removal)."""
    global _db_source_ids, _db_source_ids_loaded
    if db_sources_col is None:
        _db_source_ids = set()
    else:
        try:
            ids = [doc["_id"] for doc in db_sources_col.find({}, {"_id": 1})]
            _db_source_ids = set(ids)
        except Exception as e:
            logging.error(f"[db] Failed to load db source ids: {e}")
    _db_source_ids_loaded = True


def _is_db_source(chat_id) -> bool:
    if db_sources_col is None:
        return False
    try:
        return db_sources_col.find_one({"_id": int(chat_id)}) is not None
    except Exception:
        return False


def _db_source_info(chat_id):
    if db_sources_col is None:
        return None
    try:
        return db_sources_col.find_one({"_id": int(chat_id)})
    except Exception:
        return None


def _norm_tokens(text: str) -> list:
    """Lowercase alphanumeric tokens, punctuation-agnostic (strips _ ; ' " : etc.)."""
    return [t for t in re.findall(r"[a-zA-Z0-9]+", text.lower()) if len(t) >= 2]


def _norm_text(text: str) -> str:
    """Lowercased text with every non-alphanumeric run collapsed to a single space."""
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


_ZLIB_SEP = r"[,\s._()_-]"
_ZLIB_FULL = r"(?:z[_-]?library|z[_-]?lib|zlibrary|1lib|libgen)"
_ZLIB_TLD = r"(?:sk|li|is|za|ru)"
_ZLIB_PARTIAL = r"(?:z[_-]?l[a-z0-9_-]*|z[a-z0-9_-]*|1l[a-z0-9_-]*|libg[a-z0-9_-]*)"
_ZLIB_RUN = re.compile(
    r"(?:^|" + _ZLIB_SEP + r")"
    + _ZLIB_FULL + r"(?:[.,_\s()-]*" + _ZLIB_TLD + r")?"
    + r"(?:" + _ZLIB_SEP + r"+" + _ZLIB_FULL + r"(?:[.,_\s()-]*" + _ZLIB_TLD + r")?)*"
    + r"(?:" + _ZLIB_SEP + r"+(?:" + _ZLIB_PARTIAL + r"|[a-z0-9]{6}))?"
    + _ZLIB_SEP + r"*$",
    re.IGNORECASE,
)


def _strip_zlib_suffix(title: str) -> str:
    """Strip a trailing zlib/libgen mirror block from filenames/titles.

    Handles full markers ("...z_library_sk,_1lib_sk,"), parenthesized and
    dotted forms ("(z-library.sk, 1lib.sk, z-lib.sk)"), truncated tails
    ("_z", "_z_", "_z_l", "_z_lib_s") and the 6-char single-serve hash
    ("...z_library_ce2ef9").
    """
    if not title:
        return title
    stripped = _ZLIB_RUN.sub("", title)
    return stripped.rstrip(" ,._-") or title


_TRAILING_AUTHOR_PAREN = re.compile(
    r"\s+\(([A-Z][A-Za-z.'-]*(?:\s+[A-Z][A-Za-z.'-]*){1,2})\)\s*$"
)


def _clean_display_title(title: str) -> str:
    """Clean a title for display: underscores → spaces, collapse whitespace,
    and strip a trailing parenthetical that looks like an author name
    (e.g. "GOD OF FURY (Rina Kent)" → "GOD OF FURY")."""
    title = (title or "").replace("_", " ")
    m = _TRAILING_AUTHOR_PAREN.search(title)
    if m and not m.group(1).lower().split(" ", 1)[0] in ("the", "a", "an"):
        title = title[: m.start()]
    return re.sub(r"\s+", " ", title).strip()


def _dedupe_db_hits(hits: list, limit: int = 10) -> list:
    seen = set()
    out = []
    for h in hits:
        key = h.get("title_norm") or ""
        if not key:
            key = h.get("_id") or ""
        if key in seen:
            continue
        seen.add(key)
        out.append(h)
        if len(out) >= limit:
            break
    return out


def _db_search(query: str, limit: int = 10) -> list:
    if indexed_col is None:
        return []
    try:
        tokens = _norm_tokens(query)
        hits = []
        if tokens:
            hits = list(indexed_col.find({"tokens": {"$all": tokens}}).limit(limit))
        if not hits and tokens:
            min_score = max(1, (len(tokens) + 1) // 2)
            pipe = [
                {"$match": {"tokens": {"$in": tokens}}},
                {"$addFields": {"score": {"$size": {"$setIntersection": ["$tokens", tokens]}}}},
                {"$match": {"score": {"$gte": min_score}}},
                {"$sort": {"score": -1, "indexed_at": -1}},
                {"$limit": max(limit * 3, 30)},
            ]
            hits = list(indexed_col.aggregate(pipe))
        if not hits:
            norm = _norm_text(query)
            if norm:
                hits = list(indexed_col.find({"title_norm": {"$regex": re.escape(norm)}}).limit(limit))
        return _dedupe_db_hits(hits, limit)
    except Exception as e:
        logging.error(f"[db] Search error: {e}")
        return []


def _strip_author(query: str) -> str:
    """Strip a trailing \" by <author>\" so DB search matches the title only.

    e.g. \"harry potter by j.k. rowling\" -> \"harry potter\"
    The full query (with author) is still used for online search.
    """
    parts = re.split(r"\s+by\s+", query.strip(), flags=re.IGNORECASE)
    if len(parts) > 1:
        title = " by ".join(parts[:-1]).strip()
        return title or query.strip()
    return query.strip()


def _derive_title(message) -> str:
    if hasattr(message, "message") and not hasattr(message, "caption"):
        caption = (message.message or "").strip()
        fname = _tg_file_name(message) or ""
    else:
        caption = (message.caption or "").strip()
        fname = ""
        if message.document:
            fname = message.document.file_name or ""
        elif message.video:
            fname = message.video.file_name or ""
        elif message.audio:
            fname = (message.audio.title or message.audio.performer or "") or ""
    if not caption and fname:
        title = os.path.splitext(fname)[0]
    elif caption:
        title = caption
    else:
        title = "Untitled"
    title = re.sub(r"\s+", " ", title).strip()
    title = title.replace("_", " ")
    title = re.sub(r"\s+", " ", title).strip()
    title = _strip_zlib_suffix(title).strip()[:200] or "Untitled"
    return title


def _message_file_uid(message):
    """Stable file identifier used to detect duplicate uploads across posts."""
    if message.document:
        return getattr(message.document, "file_unique_id", None) or None
    if message.video:
        return getattr(message.video, "file_unique_id", None) or None
    if message.audio:
        return getattr(message.audio, "file_unique_id", None) or None
    if message.photo:
        try:
            return getattr(message.photo[-1], "file_unique_id", None) or None
        except Exception:
            return None
    return None


def _build_tg_link(source_chat_id, message_id, source_username: str = "") -> str:
    source_username = (source_username or "").lstrip("@")
    if source_username:
        return f"https://t.me/{source_username}/{message_id}"
    cid = str(source_chat_id)
    if cid.startswith("-100"):
        cid = cid[4:]
    return f"https://t.me/c/{cid}/{message_id}"


def _save_indexed_book(source_chat_id, message_id, title, tg_link, source_title="", source_username="", file_uid=None):
    """Upsert an indexed book. Returns 'saved', 'duplicate' or 'error'."""
    if indexed_col is None:
        return "saved"
    try:
        sid = int(source_chat_id)
    except (TypeError, ValueError):
        sid = str(source_chat_id)
    tokens = _norm_tokens(title)
    doc = {
        "_id": f"{sid}:{int(message_id)}",
        "title": title,
        "title_lower": title.lower(),
        "title_norm": _norm_text(title),
        "tokens": tokens,
        "source_chat_id": sid,
        "source_title": source_title or "",
        "source_username": source_username or "",
        "message_id": int(message_id),
        "tg_link": tg_link,
        "file_uid": file_uid or None,
        "indexed_at": time.time(),
    }
    try:
        if file_uid:
            existing = indexed_col.find_one({"file_uid": file_uid})
            if existing:
                return "duplicate"
        indexed_col.replace_one({"_id": doc["_id"]}, doc, upsert=True)
        return "saved"
    except Exception as e:
        logging.error(f"[db] Error saving indexed book: {e}")
        return "error"


_TG_POST_RE = re.compile(r"t\.me/(?:c/(\d+)|([a-zA-Z0-9_]{5,}))/(\d+)")


def _extract_tg_post_ref(url: str):
    """Return (chat_ref, message_id). chat_ref is -100... int or @username."""
    m = _TG_POST_RE.search(url or "")
    if not m:
        return None
    if m.group(1):
        chat_ref = int("-100" + m.group(1))
    else:
        chat_ref = "@" + m.group(2)
    return chat_ref, int(m.group(3))


async def _resolve_chat_id(bot, chat_ref):
    if isinstance(chat_ref, int):
        return chat_ref
    chat = await bot.get_chat(chat_ref)
    return chat.id


async def _get_tg_client():
    """Return a connected Telethon (MTProto) client, or None if not configured."""
    global _tg_client
    if _tg_client is not None:
        return _tg_client
    if not (TG_API_ID and TG_API_HASH):
        logging.warning("[mtproto] TG_API_ID / TG_API_HASH not set; history indexing disabled.")
        return None
    async with _tg_client_lock:
        if _tg_client is not None:
            return _tg_client
        try:
            from telethon import TelegramClient
            from telethon.sessions import SQLiteSession
            session = SQLiteSession(os.path.join(C_DIR, "tg_session"))
            client = TelegramClient(
                session,
                int(TG_API_ID),
                TG_API_HASH,
                device_model="AnnasDownloaderBot",
                app_version="1.0.0",
            )
            client.flood_sleep_threshold = 60
            await client.connect()
            if not await client.is_user_authorized():
                await client.start(bot_token=BOT_TOKEN)
            _tg_client = client
            logging.info("[mtproto] Telethon client ready (bot session)")
        except Exception as e:
            logging.error(f"[mtproto] Failed to init Telethon client: {e}")
            return None
    return _tg_client


def _tg_file_uid(m):
    """Stable file identifier from a Telethon message (unique per upload)."""
    try:
        if getattr(m, "document", None) is not None:
            return str(m.document.id)
        if getattr(m, "photo", None) is not None:
            return str(m.photo.id)
    except Exception:
        pass
    return None


def _tg_file_name(m):
    """Best-effort file name from a Telethon message."""
    try:
        fname = getattr(getattr(m, "file", None), "name", None)
        if fname:
            return fname
    except Exception:
        pass
    if getattr(m, "document", None) is not None:
        try:
            for attr in m.document.attributes:
                if hasattr(attr, "file_name") and attr.file_name:
                    return attr.file_name
                if hasattr(attr, "title") and attr.title:
                    return f"{attr.performer or ''} - {attr.title}" if attr.performer else attr.title
        except Exception:
            pass
    return None


async def _fetch_history(bot, chat_id, limit: int, marker: int) -> list:
    """Fetch up to `limit` messages with IDs <= marker (via MTProto).

    Bots are blocked from messages.getHistory, so — like VJ-FILTER-BOT and other
    auto-filter bots — we enumerate sequential message IDs and fetch them with
    messages.getMessages (allowed for bots). Deleted/missing IDs return None.
    """
    client = await _get_tg_client()
    if client is None:
        raise RuntimeError(
            "MTProto is not configured. Set TG_API_ID and TG_API_HASH in .env "
            "(from my.telegram.org) and restart the bot."
        )
    ids = list(range(marker, marker - limit, -1))
    try:
        raw = await asyncio.wait_for(client.get_messages(chat_id, ids=ids), timeout=40)
    except asyncio.TimeoutError:
        raise RuntimeError(f"History fetch timed out at {marker}")
    except Exception as e:
        etext = str(e)
        if "flood" in etext.lower() and "wait" in etext.lower():
            raise RuntimeError(f"Telegram rate limit hit at {marker}: {etext}")
        raise RuntimeError(f"{etext}")
    return [m for m in (raw or []) if m is not None] or []


async def _index_range(bot, chat_id, start_id, end_id, status_msg, cancel_flag) -> dict:
    """Index messages in [start_id, end_id]. Runs in a background task.

    Returns dict: saved, skipped, duplicates, scanned, cancelled, limit, error.
    """
    source = _db_source_info(chat_id) or {}
    source_title = source.get("title") or ""
    source_username = source.get("username") or ""
    marker = end_id
    saved = skipped = duplicates = scanned = 0
    batches = 0
    limit_reached = False
    error = None
    last_edit = [0.0]
    span = max(end_id - start_id, 1)

    while True:
        if cancel_flag.get("cancel"):
            break
        if batches >= MAX_INDEX_BATCHES:
            limit_reached = True
            break
        batches += 1

        try:
            msgs = await asyncio.wait_for(_fetch_history(bot, chat_id, 100, marker), timeout=40)
        except Exception as e:
            error = str(e)
            logging.error(f"[index] Batch fetch failed at {marker}: {e}")
            break
        if not msgs:
            break

        floor = min(m.id for m in msgs)
        for m in msgs:
            if not (start_id <= m.id <= end_id):
                continue
            scanned += 1
            if not (getattr(m, "document", None) or getattr(m, "photo", None)):
                skipped += 1
                continue
            title = _derive_title(m)
            link = _build_tg_link(chat_id, m.id, source_username)
            file_uid = _tg_file_uid(m)
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                executor,
                lambda: _save_indexed_book(
                    chat_id, m.id, title, link,
                    source_title, source_username, file_uid,
                ),
            )
            if result == "duplicate":
                duplicates += 1
            elif result == "saved":
                saved += 1
            else:
                skipped += 1

        if floor <= start_id:
            break
        marker = floor - 1

        now = time.monotonic()
        if now - last_edit[0] >= 2.5:
            last_edit[0] = now
            pct = max(0.0, min(100.0, (end_id - marker) / span * 100))
            try:
                await status_msg.edit_text(
                    f"🗄️ *Indexing…* `{pct:.0f}%`\n"
                    f"• Saved: `{saved}`\n"
                    f"• Duplicates skipped: `{duplicates}`\n"
                    f"• No file: `{skipped}`\n"
                    f"• Scanned: `{scanned}`\n"
                    f"• Current post: `{marker}`\n\n"
                    f"Send `/cancel` to stop\\.",
                    parse_mode="Markdown",
                )
            except Exception:
                pass

    return {
        "saved": saved, "skipped": skipped, "duplicates": duplicates,
        "scanned": scanned, "cancelled": bool(cancel_flag.get("cancel")),
        "limit": limit_reached, "error": error,
    }


async def _forward_index_range(bot, chat_id, start_id, end_id, status_msg, cancel_flag) -> dict:
    """Index a range by forwarding each message to the dump channel first.

    Used automatically when the bot is not an admin in the source chat (it only
    needs to be a member of it, and the chat must allow forwarding).
    Every message is forwarded to the dump channel; only messages that forward
    successfully and carry a file are indexed (from the dump copy), so the
    stored links point at the dump where the bot has full access.
    """
    client = await _get_tg_client()
    if client is None:
        raise RuntimeError(
            "MTProto is not configured. Set TG_API_ID and TG_API_HASH in .env "
            "(from my.telegram.org) and restart the bot."
        )
    dump_ref = _get_cfg_value("dump_channel")
    if not dump_ref:
        raise RuntimeError(
            "Forward index mode needs a dump channel. Set one with /dump <channel_id> first."
        )
    dump_id, dump_username = await _resolve_dump_info(bot, dump_ref)
    source = _db_source_info(chat_id) or {}
    source_title = source.get("title") or ""
    source_username = source.get("username") or ""

    marker = end_id
    saved = skipped = duplicates = scanned = 0
    batches = 0
    limit_reached = False
    error = None
    last_edit = [0.0]
    span = max(end_id - start_id, 1)
    BATCH = 10

    async def _process_forwarded(fm):
        nonlocal saved, skipped, duplicates, scanned
        scanned += 1
        if not any(getattr(fm, a, None) for a in ("document", "video", "audio", "photo")):
            try:
                await client.delete_messages(dump_id, [fm.id])
            except Exception:
                pass
            skipped += 1
            return
        title = _derive_title(fm)
        link = _build_tg_link(dump_id, fm.id, dump_username)
        file_uid = _tg_file_uid(fm)
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            executor,
            lambda: _save_indexed_book(
                dump_id, fm.id, title, link,
                "", dump_username, file_uid,
            ),
        )
        if result == "duplicate":
            duplicates += 1
        elif result == "saved":
            saved += 1
        else:
            skipped += 1

    while True:
        if cancel_flag.get("cancel"):
            break
        if batches >= MAX_INDEX_BATCHES:
            limit_reached = True
            break
        batches += 1

        ids = [i for i in range(marker, marker - BATCH, -1) if i >= start_id]
        if not ids:
            break
        marker = ids[0] - 1

        try:
            sent = await asyncio.wait_for(
                client.forward_messages(dump_ref, messages=ids, from_peer=chat_id),
                timeout=60,
            )
        except asyncio.TimeoutError:
            error = f"Forward timed out at {ids[0]}"
            logging.error(f"[index:fwd] {error}")
            break
        except Exception as e:
            # Batch may contain missing IDs → fall back to per-ID forwarding.
            for i in ids:
                if cancel_flag.get("cancel"):
                    break
                try:
                    fm = await asyncio.wait_for(
                        client.forward_messages(dump_ref, messages=i, from_peer=chat_id),
                        timeout=60,
                    )
                except asyncio.TimeoutError:
                    error = f"Forward timed out at {i}"
                    logging.error(f"[index:fwd] {error}")
                    break
                except Exception as ie:
                    skipped += 1
                    continue
                await _process_forwarded(fm)
            continue

        if not isinstance(sent, (list, tuple)):
            sent = [sent]
        for fm in (sent or []):
            if fm is None:
                continue
            if cancel_flag.get("cancel"):
                break
            await _process_forwarded(fm)

        if marker < start_id:
            break

        now = time.monotonic()
        if now - last_edit[0] >= 2.5:
            last_edit[0] = now
            pct = max(0.0, min(100.0, (end_id - marker) / span * 100))
            try:
                await status_msg.edit_text(
                    f"🗄️ *Indexing \\(forward mode\\)…* `{pct:.0f}%`\n"
                    f"• Saved: `{saved}`\n"
                    f"• Duplicates skipped: `{duplicates}`\n"
                    f"• No file: `{skipped}`\n"
                    f"• Forwarded: `{scanned}`\n"
                    f"• Current post: `{marker}`\n\n"
                    f"Send `/cancel` to stop\\.",
                    parse_mode="Markdown",
                )
            except Exception:
                pass

    return {
        "saved": saved, "skipped": skipped, "duplicates": duplicates,
        "scanned": scanned, "cancelled": bool(cancel_flag.get("cancel")),
        "limit": limit_reached, "error": error,
    }


async def _run_index(update: Update, context: ContextTypes.DEFAULT_TYPE, start_ref, end_ref):
    bot = context.bot
    user_id = update.effective_user.id
    status = await update.message.reply_text("🗄️ *Starting index…*", parse_mode="Markdown")
    try:
        chat_id = await _resolve_chat_id(bot, start_ref[0])
        chat_id2 = await _resolve_chat_id(bot, end_ref[0])
        if chat_id != chat_id2:
            await status.edit_text("❌ Starting and last URLs must be from the same chat.")
            return
        start_id, end_id = start_ref[1], end_ref[1]
        if start_id > end_id:
            start_id, end_id = end_id, start_id
        if start_id == end_id:
            await status.edit_text("❌ Start and last post are the same message.")
            return
        if not _is_db_source(chat_id):
            info = _db_source_info(chat_id)
            if info is None and db_sources_col is not None:
                try:
                    chat = await bot.get_chat(chat_id)
                    db_sources_col.update_one(
                        {"_id": chat_id},
                        {"$set": {
                            "title": chat.title or chat.username or str(chat_id),
                            "username": chat.username or "",
                            "added_by": update.effective_user.id,
                            "added_at": time.time(),
                            "enabled": True,
                        }},
                        upsert=True,
                    )
                    _load_db_source_ids()
                except Exception:
                    pass
    except BadRequest as e:
        await status.edit_text(f"❌ *Index failed:* {_esc(str(e))}", parse_mode="Markdown")
        return
    except Exception as e:
        await status.edit_text(f"❌ *Index failed:* {_esc(str(e))}", parse_mode="Markdown")
        return

    existing = _active_index_jobs.get(user_id)
    if existing and not existing.done():
        await status.edit_text(
            "⚠️ An index job is already running\\. Send `/cancel` first\\.", parse_mode="Markdown"
        )
        return

    cancel_flag = {"cancel": False}
    user_sessions.setdefault(user_id, {})["index_cancel"] = cancel_flag

    try:
        bot_me = await bot.get_me()
        bot_member = await bot.get_chat_member(chat_id, bot_me.id)
        bot_admin = isinstance(bot_member, (ChatMemberAdministrator, ChatMemberOwner))
    except Exception:
        bot_admin = True

    use_forward = not bot_admin
    if use_forward and not _get_cfg_value("dump_channel"):
        await status.edit_text(
            "❌ *The bot is not an admin in this chat, so forward indexing is required\\.*\n"
            "A dump channel is needed for that\\. Set one first with `/dump <channel_id>`"
            " and make sure the bot has joined this chat\\. Then run `/index` again\\.",
            parse_mode="Markdown",
        )
        return

    async def _job():
        try:
            if use_forward:
                result = await _forward_index_range(bot, chat_id, start_id, end_id, status, cancel_flag)
            else:
                result = await _index_range(bot, chat_id, start_id, end_id, status, cancel_flag)
        except asyncio.CancelledError:
            cancel_flag["cancel"] = True
            result = {
                "saved": 0, "skipped": 0, "duplicates": 0, "scanned": 0,
                "cancelled": True, "limit": False, "error": None,
            }
        except Exception as e:
            logging.error(f"[index] Background job crashed: {e}")
            result = {
                "saved": 0, "skipped": 0, "duplicates": 0, "scanned": 0,
                "cancelled": False, "limit": False, "error": str(e),
            }

        lines = []
        if result.get("cancelled"):
            lines.append("🚫 *Index stopped \\(cancelled\\).*")
        elif result.get("error"):
            lines.append(f"⚠️ *Index stopped with errors*: _{_esc(result['error'])}_")
        elif result.get("limit"):
            lines.append("⚠️ *Index stopped*: batch safety limit reached\\.")
        else:
            lines.append("✅ *Index complete!*")
        lines.append(f"• Saved: `{result['saved']}`")
        lines.append(f"• Duplicates skipped: `{result['duplicates']}`")
        lines.append(f"• No file: `{result['skipped']}`")
        lines.append(f"• Scanned: `{result['scanned']}`")
        try:
            await status.edit_text("\n".join(lines), parse_mode="Markdown")
        except Exception:
            pass
        _active_index_jobs.pop(user_id, None)
        user_sessions.get(user_id, {}).pop("index_cancel", None)

    _active_index_jobs[user_id] = asyncio.create_task(_job())
    method_note = "forward \\(via dump\\)" if use_forward else "direct"
    await status.edit_text(
        f"🗄️ *Indexing started in background…*\n"
        f"• Method: `{method_note}`\n"
        f"• Range: `{start_id}` → `{end_id}`\n"
        f"• Progress will appear here\\.\n"
        f"Send `/cancel` to stop\\.",
        parse_mode="Markdown",
    )


async def cmd_index(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _register_pm_if_private(update)
    if not await _is_admin(update, context):
        await update.message.reply_text("❌ You don't have permission to use this command.")
        return
    if not _is_service_enabled():
        await update.message.reply_text("🔴 Bot service is currently offline.")
        return
    if db_sources_col is None or indexed_col is None:
        await update.message.reply_text("❌ MongoDB is not configured. Set `MONGO_URI` and restart.", parse_mode="Markdown")
        return

    args = context.args
    if len(args) >= 2:
        start_ref = _extract_tg_post_ref(args[0])
        end_ref = _extract_tg_post_ref(args[1])
        if not start_ref or not end_ref:
            await update.message.reply_text("❌ Invalid post URLs. Example: `/index https://t.me/c/123/10 https://t.me/c/123/500`", parse_mode="Markdown")
            return
        await _run_index(update, context, start_ref, end_ref)
        return

    context.user_data["index_flow"] = {"step": "start"}
    await update.message.reply_text(
        "🗄️ *Index mode started.*\n\n"
        "Send the *starting* post URL (the first book post to index):\n"
        "e.g. `https://t.me/c/123456789/100`",
        parse_mode="Markdown",
    )


async def _index_flow_step(update: Update, context: ContextTypes.DEFAULT_TYPE, flow: dict):
    text = (update.message.text or "").strip()
    if not text:
        return
    if flow.get("step") == "start":
        start_ref = _extract_tg_post_ref(text)
        if not start_ref:
            await update.message.reply_text("❌ That doesn't look like a Telegram post link. Try again (or send `/cancel`).")
            return
        flow["start_ref"] = start_ref
        flow["step"] = "end"
        context.user_data["index_flow"] = flow
        await update.message.reply_text("Now send the *last* post URL.", parse_mode="Markdown")
        return

    if flow.get("step") == "end":
        end_ref = _extract_tg_post_ref(text)
        if not end_ref:
            await update.message.reply_text("❌ That doesn't look like a Telegram post link. Try again.")
            return
        flow["end_ref"] = end_ref
        context.user_data.pop("index_flow", None)
        await _run_index(update, context, flow["start_ref"], flow["end_ref"])


async def cmd_cancel_index(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    flow_cancelled = bool(context.user_data.pop("index_flow", None))

    flag = (user_sessions.get(user_id) or {}).get("index_cancel")
    if flag:
        flag["cancel"] = True
    task = _active_index_jobs.pop(user_id, None)
    if task and not task.done():
        task.cancel()

    if flag or flow_cancelled:
        await update.message.reply_text("🚫 Index cancelled.")
    else:
        await update.message.reply_text("Nothing to cancel.")


async def cmd_connectdb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _is_admin(update, context):
        await update.message.reply_text("❌ You don't have permission to use this command.")
        return
    if db_sources_col is None:
        await update.message.reply_text("❌ MongoDB is not configured. Set `MONGO_URI` and restart.", parse_mode="Markdown")
        return

    chat = update.effective_chat
    if chat.type not in ("channel", "group", "supergroup"):
        await update.message.reply_text("❌ Run `/connectdb` inside the channel/group you want to use as the database source.", parse_mode="Markdown")
        return

    title = chat.title or chat.username or str(chat.id)
    db_sources_col.update_one(
        {"_id": chat.id},
        {"$set": {
            "title": title,
            "username": chat.username or "",
            "added_by": update.effective_user.id,
            "added_at": time.time(),
            "enabled": True,
        }},
        upsert=True,
    )
    _load_db_source_ids()
    await update.message.reply_text(
        f"✅ Connected *{_esc(title)}* as a database source\\.\n\n"
        f"Use `/index` here to index a range of posts\\.\n"
        f"More than one database group can be connected\\.",
        parse_mode="Markdown",
    )


async def cmd_showdbs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _register_pm_if_private(update)
    if not await _is_admin(update, context):
        await update.message.reply_text("❌ You don't have permission to use this command.")
        return
    if db_sources_col is None:
        await update.message.reply_text("❌ MongoDB is not configured.", parse_mode="Markdown")
        return

    sources = list(db_sources_col.find().sort("added_at", -1))
    if not sources:
        await update.message.reply_text("🗄️ No database sources connected yet. Use `/connectdb` in a channel/group.", parse_mode="Markdown")
        return

    lines = ["🗄️ *Database sources:*\n"]
    for s in sources:
        sid = s.get("_id")
        count = 0
        if indexed_col is not None:
            try:
                count = indexed_col.count_documents({"source_chat_id": int(sid)})
            except Exception:
                pass
        title = s.get("title") or sid
        lines.append(f"• *{_esc(str(title))}*\n  `{sid}` — {count} books")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_removedb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _is_admin(update, context):
        await update.message.reply_text("❌ You don't have permission to use this command.")
        return
    if db_sources_col is None:
        await update.message.reply_text("❌ MongoDB is not configured.", parse_mode="Markdown")
        return

    arg = " ".join(context.args).strip().lower()
    wipe = "--wipe" in arg
    arg = arg.replace("--wipe", "").strip()

    target_id = None
    if arg in ("this", "here"):
        target_id = update.effective_chat.id
    elif arg and (arg.lstrip("-").isdigit()):
        target_id = int(arg)
    elif arg:
        doc = db_sources_col.find_one({"title": {"$regex": re.escape(arg), "$options": "i"}})
        if doc:
            target_id = doc["_id"]

    if not target_id:
        await update.message.reply_text(
            "Usage: `/removedb this` (current chat), `/removedb <chat_id>`, `/removedb <title>`\n"
            "Add `--wipe` to also delete all indexed books: `/removedb this --wipe`",
            parse_mode="Markdown",
        )
        return

    res = db_sources_col.delete_one({"_id": int(target_id)})
    if res.deleted_count == 0:
        await update.message.reply_text("❌ Source not found.")
        return
    wiped = 0
    if wipe and indexed_col is not None:
        wiped = indexed_col.delete_many({"source_chat_id": int(target_id)}).deleted_count
    _load_db_source_ids()
    await update.message.reply_text(
        f"✅ Removed database source.\n• Books removed: `{wiped}`" if wipe
        else f"✅ Removed database source (`{target_id}`).\nTip: add `--wipe` to also delete its books.",
        parse_mode="Markdown",
    )


async def cmd_dbsearch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _register_pm_if_private(update)
    if not _is_service_enabled():
        await update.message.reply_text("🔴 Bot service is currently offline.")
        return

    query = " ".join(context.args).strip()
    if not query:
        await update.message.reply_text(
            "Usage: `/dbsearch <query>`\nSearches only the indexed database.",
            parse_mode="Markdown",
        )
        return

    loop = asyncio.get_running_loop()
    db_query = _strip_author(query)
    hits = await loop.run_in_executor(executor, lambda: _db_search(db_query, 10))
    if not hits:
        await update.message.reply_text(f"❌ No database results for *{_esc(db_query)}*.", parse_mode="Markdown")
        return

    await _send_db_results(update, context, db_query, hits, full_query=query)


def _db_keyboard(hits: list) -> InlineKeyboardMarkup:
    kb = []
    for hit in hits:
        title = _clean_display_title(hit.get("title") or "Book")[:40]
        key = hit.get("_id") or ""
        if not key:
            cid = hit.get("source_chat_id")
            mid = hit.get("message_id")
            if cid is not None and mid is not None:
                key = f"{cid}:{mid}"
        if key:
            kb.append([InlineKeyboardButton(f"📥 {title}", callback_data=f"db:{key}")])
    kb.append([
        InlineKeyboardButton("🔎 Search Online", callback_data="db_online"),
        InlineKeyboardButton("❌ Close", callback_data="db_close"),
    ])
    return InlineKeyboardMarkup(kb)


async def _send_db_results(update: Update, context: ContextTypes.DEFAULT_TYPE, query: str, hits: list, full_query: str = None):
    user_id = update.effective_user.id
    user_sessions[user_id] = {"last_query": full_query or query}

    lines = [f"🗄️ *Database results for \"{_esc(query)}\"*\n"]
    for i, hit in enumerate(hits):
        title = _clean_display_title(hit.get("title") or "Unknown")
        lines.append(f"*{i+1}.* {_esc(title)}")

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=_db_keyboard(hits),
    )


async def db_result_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data

    if data == "db_online":
        uid = query.from_user.id
        session = user_sessions.get(uid) or {}
        q = session.get("last_query")
        if not q:
            await query.answer("No query stored. Search again.", show_alert=True)
            return
        await _run_online_search(update, context, q)
        try:
            await query.message.delete()
        except Exception:
            pass
        return

    if data == "db_close":
        try:
            await query.message.delete()
        except Exception:
            pass
        return

    if data.startswith("db:"):
        key = data[len("db:"):]
        rec = None
        if indexed_col is not None:
            try:
                rec = indexed_col.find_one({"_id": key})
            except Exception:
                rec = None
        if not rec:
            try:
                cid_part, mid_part = key.rsplit(":", 1)
                cid_val = int(cid_part) if cid_part.lstrip("-").isdigit() else cid_part
                rec = {"source_chat_id": cid_val, "message_id": int(mid_part), "title": "Book"}
            except Exception:
                await query.answer("Invalid book reference.", show_alert=True)
                return
        await _forward_db_book(update, context, rec)


async def _resolve_dump_info(bot, dump_ref):
    """Resolve dump channel reference to (chat_id, username)."""
    try:
        chat = await bot.get_chat(dump_ref)
        return chat.id, (chat.username or "")
    except Exception:
        return dump_ref, str(dump_ref).lstrip("@").strip()


async def _index_dump_message(bot, dump_ref, sent_msg, title):
    """Index a file that was just posted to the dump channel."""
    if not sent_msg:
        return
    if not any(getattr(sent_msg, a, None) for a in ("document", "video", "audio", "photo")):
        return
    dump_id, dump_username = await _resolve_dump_info(bot, dump_ref)
    link = _build_tg_link(dump_id, sent_msg.message_id, dump_username)
    file_uid = _message_file_uid(sent_msg)

    def _do():
        return _save_indexed_book(
            dump_id, sent_msg.message_id, title, link,
            "", dump_username, file_uid,
        )

    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(executor, _do)
    except Exception as e:
        logging.error(f"[dump] Index failed: {e}")
        return
    if result == "saved":
        print(f"[dump] Indexed: {title}")
    elif result == "duplicate":
        print(f"[dump] Duplicate (skipped): {title}")


async def _forward_db_book(update: Update, context: ContextTypes.DEFAULT_TYPE, rec: dict):
    raw_source = rec.get("source_chat_id")
    try:
        source_id = int(raw_source)
    except (TypeError, ValueError):
        source_id = raw_source
    msg_id = int(rec["message_id"])
    title = rec.get("title") or "Book"
    chat_id = update.effective_chat.id
    clicker_id = update.effective_user.id

    delivery_mode = _get_delivery_mode()
    if delivery_mode == "pm":
        target_chat_id = clicker_id
    else:
        target_chat_id = _get_connected_group() or chat_id

    try:
        sent = await context.bot.copy_message(
            chat_id=target_chat_id,
            from_chat_id=source_id,
            message_id=msg_id,
        )
    except Exception as e:
        bot_info = await context.bot.get_me()
        bot_username = bot_info.username
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("🚀 Start Bot", url=f"https://t.me/{bot_username}")
        ]])
        text = (
            f"❌ *Could not deliver!*\n\n"
            f"Hi {update.effective_user.mention_markdown()},\n"
            f"Please start the bot in PM first to receive files."
        )
        if delivery_mode == "pm":
            text += "\n\nClick the button below to start the bot in PM."
            await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown", reply_markup=keyboard)
        else:
            await context.bot.send_message(chat_id=chat_id, text=f"❌ *Could not forward:* {_esc(str(e))}", parse_mode="Markdown")
        return

    if target_chat_id > 0:
        auto_delete_enabled = _get_cfg_value("auto_delete", False)
        if auto_delete_enabled:
            delete_time_secs = _get_cfg_value("delete_time", 120)
            asyncio.create_task(_delayed_delete(context.bot, target_chat_id, sent.message_id, delete_time_secs))

    dump_channel = _get_cfg_value("dump_channel")
    if dump_channel:
        try:
            dump_id, _ = await _resolve_dump_info(context.bot, dump_channel)
        except Exception:
            dump_id = dump_channel
        if str(dump_id) == str(source_id) or str(dump_id) == str(target_chat_id):
            logging.info("[dump] Dump channel equals the source or delivery target; skipping self-mirror.")
        else:
            try:
                dump_msg = await context.bot.forward_message(chat_id=dump_channel, from_chat_id=source_id, message_id=msg_id)
                await _index_dump_message(context.bot, dump_channel, dump_msg, title)
            except Exception as de:
                logging.error(f"[dump] Failed to mirror db book to {dump_channel}: {de}")

    await context.bot.send_message(
        chat_id=chat_id,
        text=f"✅ *{_esc(title)}* forwarded to your DM 💬" if target_chat_id > 0 else f"✅ *{_esc(title)}* sent to the group.",
        parse_mode="Markdown",
    )


async def attachment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_service_enabled():
        return
    if not update.message:
        return
    chat = update.effective_chat
    if not chat or chat.type not in ("channel", "group", "supergroup"):
        return
    if not (update.message.document or update.message.video or update.message.audio or update.message.photo):
        return

    loop = asyncio.get_running_loop()
    is_source = await loop.run_in_executor(executor, lambda: _is_db_source(chat.id))
    if not is_source:
        return

    title = _derive_title(update.message)
    info = await loop.run_in_executor(executor, lambda: _db_source_info(chat.id) or {})
    source_username = info.get("username") or (chat.username or "")
    link = _build_tg_link(chat.id, update.message.message_id, source_username)
    file_uid = _message_file_uid(update.message)

    result = await loop.run_in_executor(
        executor,
        lambda: _save_indexed_book(
            chat.id, update.message.message_id, title, link,
            info.get("title") or chat.title or "", source_username, file_uid,
        ),
    )
    if result == "saved":
        print(f"[db] Auto-indexed: {title}")
    elif result == "duplicate":
        print(f"[db] Skipped duplicate: {title}")


# ---------------------------------------------------------------------------
# Search handlers
# ---------------------------------------------------------------------------

async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _register_pm_if_private(update)
    if not _is_service_enabled():
        await update.message.reply_text("🔴 Bot service is currently offline.")
        return

    if update.effective_chat and update.effective_chat.type == "private":
        if not _get_cfg_value("pm_enabled", True):
            await update.message.reply_text("❌ Private messages are currently disabled.")
            return

    search_mode = _get_search_mode()
    if search_mode not in ("slash", "all"):
        await update.message.reply_text(
            f"❌ Slash search is disabled. Current mode: `{search_mode}`\n"
            f"An admin can change it with `/mode search all`",
            parse_mode="Markdown"
        )
        return

    query = " ".join(context.args).strip()
    if not query:
        await update.message.reply_text(
            "Usage: /search <book title or author>\nExample: /search Dune"
        )
        return
    await _run_search(update, context, query)


async def msg_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _register_pm_if_private(update)
    if not _is_service_enabled():
        return

    if not update.message:
        return

    if update.effective_chat and update.effective_chat.type == "private":
        if not _get_cfg_value("pm_enabled", True):
            return

    text = (update.message.text or "").strip()
    if not text:
        return

    flow = context.user_data.get("index_flow")
    if flow:
        await _index_flow_step(update, context, flow)
        return

    if update.effective_chat and update.effective_chat.type == "channel":
        return

    search_mode = _get_search_mode()

    if re.match(r"^#(?:request|bookrequest|requestion)\b", text, re.IGNORECASE):
        if search_mode in ("hashtag", "all"):
            query = re.sub(r"^#(?:request|bookrequest|requestion)\s*", "", text, flags=re.IGNORECASE).strip()
            if query:
                await _run_search(update, context, query)
        return

    if not text.startswith("/"):
        if search_mode in ("text", "all"):
            await _run_search(update, context, text)


async def _run_search(update: Update, context: ContextTypes.DEFAULT_TYPE, query: str):
    if update.effective_chat.type == "private":
        pm_search = _get_cfg_value("pm_search", True)
        if not pm_search:
            await update.message.reply_text(
                "❌ Searching in private messages is currently disabled by the administrator.",
                parse_mode="Markdown"
            )
            return

    user_id = update.effective_user.id
    status = await update.message.reply_text(
        f"🔍 Searching for *{_esc(query)}*…", parse_mode="Markdown"
    )

    loop = asyncio.get_running_loop()

    # 1) Try the indexed database first (author name omitted — DB has no authors)
    db_query = _strip_author(query)
    db_hits = await loop.run_in_executor(executor, lambda: _db_search(db_query, 8))
    if db_hits:
        try:
            await status.delete()
        except Exception:
            pass
        await _send_db_results(update, context, db_query, db_hits, full_query=query)
        return

    # 2) No db results → search online directly (full query, author included)
    await _run_online_search(update, context, query, status)


async def _run_online_search(update: Update, context: ContextTypes.DEFAULT_TYPE, query: str, status=None):
    user_id = update.effective_user.id
    if status is None:
        if update.callback_query is not None:
            reply_to = update.callback_query.message
        else:
            reply_to = update.effective_message
        status = await reply_to.reply_text(
            f"🔍 Searching for *{_esc(query)}*…", parse_mode="Markdown"
        )

    loop = asyncio.get_running_loop()
    try:
        results = await loop.run_in_executor(executor, lambda: _sync_search(query, 10))
    except Exception as e:
        try:
            await status.edit_text(f"❌ Search error: {e}")
        except Exception:
            pass
        return

    if not results:
        no_result_msg = _get_cfg_value("no_result_msg")
        if no_result_msg:
            msg = no_result_msg.replace("{query}", _esc(query))
            try:
                await status.edit_text(msg, parse_mode="Markdown")
            except Exception:
                try:
                    await status.edit_text(msg)
                except Exception:
                    pass
        else:
            try:
                await status.delete()
            except Exception:
                pass
        return

    user_sessions[user_id] = {"results": results}

    bot_info = await context.bot.get_me()
    bot_username = bot_info.username

    delivery_mode = _get_delivery_mode()
    if delivery_mode == "group":
        pm_started = True
    else:
        pm_started = (update.effective_chat.type == "private") or await _has_started_pm(user_id)

    lines = [f"📚 *Results for \"{_esc(query)}\"*\n"]

    for i, b in enumerate(results):
        fmt = b['format'].upper() if b['format'] else "UNKNOWN"
        size = b['size'].lower() if b['size'] else "unknown"
        md5 = b.get("md5", "")
        if md5:
            if pm_started:
                md5_link = f"/md5\\_{md5}"
            else:
                md5_link = f"[/start\\_md5\\_{md5}](https://t.me/{bot_username}?start=md5_{md5})"
        else:
            md5_link = ""

        block = (
            f"*{i+1}.* 📚 *{_esc(b['title'].upper())}*\n"
            f"   👤 {_esc(b['author'])}\n"
            f"   📦 {_esc(fmt)} · 📏 {_esc(size)} · 🌐 {_esc(b['language'])}"
        )
        if md5_link:
            block += f"\n   🔗 {md5_link}"
        lines.append(block + "\n")

    try:
        await status.edit_text(
            "\n".join(lines),
            parse_mode="Markdown",
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Download & Delivery Core Helper
# ---------------------------------------------------------------------------

async def _process_md5_download(md5: str, update: Update, context: ContextTypes.DEFAULT_TYPE, book: dict = None):
    chat_id = update.effective_chat.id
    clicker_id = update.effective_user.id
    loop = asyncio.get_running_loop()

    if not book:
        for sess in user_sessions.values():
            for b in sess.get("results", []):
                if b.get("md5") == md5:
                    book = b
                    break
            if book:
                break

    if not book:
        try:
            results = await loop.run_in_executor(executor, lambda: _sync_search(md5, 1))
            if results:
                book = results[0]
        except Exception:
            pass

    if not book:
        book = {
            "title": f"Book_{md5[:8]}",
            "author": "Unknown",
            "year": "Unknown",
            "language": "Unknown",
            "format": "Unknown",
            "size": "Unknown",
            "md5": md5,
            "url": f"{BASE_URL}/md5/{md5}"
        }

    delivery_mode = _get_delivery_mode()
    if delivery_mode == "pm":
        target_chat_id = clicker_id

        pm_started = await _has_started_pm(clicker_id)

        if not pm_started:
            try:
                await context.bot.send_message(
                    chat_id=target_chat_id,
                    text=f"⏳ *Preparing download for:* {_esc(book['title'])}",
                    parse_mode="Markdown"
                )
                await _register_pm_user(
                    clicker_id,
                    update.effective_user.username or "",
                    update.effective_user.first_name or ""
                )
            except Exception:
                bot_info = await context.bot.get_me()
                bot_username = bot_info.username
                keyboard = [
                    [
                        InlineKeyboardButton(
                            text="🚀 Start Bot & Get Book",
                            url=f"https://t.me/{bot_username}?start=md5_{md5}"
                        )
                    ]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)

                await context.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"❌ *Could not deliver to DM!*\n\n"
                        f"Hi {update.effective_user.mention_markdown()},\n"
                        f"Please start the bot in PM first to receive files.\n\n"
                        f"Click the button below to start the bot in PM and receive your file instantly!"
                    ),
                    parse_mode="Markdown",
                    reply_markup=reply_markup
                )
                return
        else:
            try:
                await context.bot.send_message(
                    chat_id=target_chat_id,
                    text=f"⏳ *Preparing download for:* {_esc(book['title'])}",
                    parse_mode="Markdown"
                )
            except Exception:
                bot_info = await context.bot.get_me()
                bot_username = bot_info.username
                keyboard = [
                    [
                        InlineKeyboardButton(
                            text="🚀 Start Bot & Get Book",
                            url=f"https://t.me/{bot_username}?start=md5_{md5}"
                        )
                    ]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)

                await context.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"❌ *Could not deliver to DM!*\n\n"
                        f"Hi {update.effective_user.mention_markdown()},\n"
                        f"Please start the bot in PM first to receive files.\n\n"
                        f"Click the button below to start the bot in PM and receive your file instantly!"
                    ),
                    parse_mode="Markdown",
                    reply_markup=reply_markup
                )
                return
    else:
        connected = _get_connected_group()
        target_chat_id = connected if connected else chat_id

    status = await context.bot.send_message(
        chat_id=chat_id,
        text=f"⏳ *Getting download link for:*\n{_esc(book['title'])}",
        parse_mode="Markdown",
    )

    try:
        direct_url = await loop.run_in_executor(
            executor, lambda: _sync_get_direct_url(book["url"])
        )
    except Exception as e:
        book_page_url = book.get("url") or f"{BASE_URL}/md5/{md5}"
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("📥 Download via Web", url=book_page_url)
        ]])
        await status.edit_text(
            f"⚠️ *Libgen unavailable* — get it directly from the web:\n_{_esc(book['title'])}_",
            parse_mode="Markdown",
            reply_markup=keyboard
        )
        return

    async with dl_semaphore:
        last_bar: list = [""]
        last_edit_time: list = [0.0]
        download_active = [True]

        def on_dl_progress(done: int, total: int):
            bar = make_bar(done, total)
            if bar == last_bar[0]:
                return
            last_bar[0] = bar

            async def _edit():
                if not download_active[0]:
                    return
                now = time.monotonic()
                if now - last_edit_time[0] < 2.5:
                    return
                last_edit_time[0] = now
                try:
                    await status.edit_text(
                        f"⬇️ *Downloading:* {_esc(book['title'])}\n{bar}",
                        parse_mode="Markdown",
                    )
                except Exception:
                    pass

            asyncio.run_coroutine_threadsafe(_edit(), loop)

        await status.edit_text(
            f"⬇️ *Downloading:* {_esc(book['title'])}\n{make_bar(0, 1)}",
            parse_mode="Markdown",
        )

        try:
            file_path, fname = await loop.run_in_executor(
                executor,
                lambda: _sync_download(direct_url, DL_PATH, on_dl_progress),
            )
        except Exception as e:
            download_active[0] = False
            book_page_url = book.get("url") or f"{BASE_URL}/md5/{md5}"
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("📥 Direct Download", url=direct_url)],
                [InlineKeyboardButton("📖 Book Page", url=book_page_url)]
            ])
            await status.edit_text(
                f"⚠️ *Download failed* — get it via browser:\n_{_esc(book['title'])}_",
                parse_mode="Markdown",
                reply_markup=keyboard
            )
            return
        finally:
            download_active[0] = False

    file_size = os.path.getsize(file_path)
    ul_last_time = [0.0]
    ul_last_pct = [-1]

    async def on_ul_progress(current: int, total: int):
        pct = int(current / total * 100) if total else 0
        if pct == ul_last_pct[0]:
            return
        now = time.monotonic()
        if now - ul_last_time[0] < 2.5 and pct != 100:
            return
        ul_last_time[0] = now
        ul_last_pct[0] = pct
        bar = make_bar(current, total)
        try:
            await status.edit_text(
                f"⬆️ *Uploading:* {_esc(fname)}\n{bar}",
                parse_mode="Markdown",
            )
        except Exception:
            pass

    await status.edit_text(
        f"✅ Download complete!\n⬆️ *Uploading:* {_esc(fname)}\n{make_bar(0, file_size)}",
        parse_mode="Markdown",
    )

    if target_chat_id != chat_id:
        dest_label = "your DM 💬" if delivery_mode == "pm" else "the connected group 👥"
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"📨 Sending *{_esc(fname)}* to {dest_label}…",
                parse_mode="Markdown",
            )
        except Exception:
            pass

    try:
        auto_delete_enabled = _get_cfg_value("auto_delete", False)
        delete_time_secs = _get_cfg_value("delete_time", 120)
        caption_suffix = ""
        is_pm_delivery = (target_chat_id > 0)
        if is_pm_delivery and auto_delete_enabled:
            if delete_time_secs >= 3600 and delete_time_secs % 3600 == 0:
                fmt_time = f"{delete_time_secs // 3600} hours"
            elif delete_time_secs >= 60 and delete_time_secs % 60 == 0:
                fmt_time = f"{delete_time_secs // 60} minutes"
            else:
                fmt_time = f"{delete_time_secs} seconds"
            caption_suffix = (
                f"\n\n⚠️ *This file will be automatically deleted in {fmt_time}. *"
                f"Please download it or forward it to your Saved Messages to keep it!"
            )

        with open(file_path, "rb") as fh:
            sent_message = await context.bot.send_document(
                chat_id=target_chat_id,
                document=fh,
                filename=fname,
                caption=(
                    f"📚 *{_esc(book['title'])}*\n"
                    f"👤 {_esc(book['author'])}  •  📅 {book['year']}"
                    f"{caption_suffix}"
                ),
                parse_mode="Markdown",
                read_timeout=600,
                write_timeout=600,
                connect_timeout=30,
                pool_timeout=60,
            )

        dump_channel = _get_cfg_value("dump_channel")
        if dump_channel and sent_message and sent_message.document:
            try:
                dump_id, _ = await _resolve_dump_info(context.bot, dump_channel)
            except Exception:
                dump_id = dump_channel
            if str(dump_id) == str(target_chat_id):
                logging.info("[dump] Dump channel equals the delivery target; skipping extra copy.")
            else:
                try:
                    dump_msg = await context.bot.send_document(
                        chat_id=dump_channel,
                        document=sent_message.document.file_id,
                        filename=fname,
                        caption=(
                            f"📚 *{_esc(book['title'])}*\n"
                            f"👤 {_esc(book['author'])}  •  📅 {book['year']}"
                        ),
                        parse_mode="Markdown",
                    )
                    await _index_dump_message(context.bot, dump_channel, dump_msg, book["title"])
                except Exception as de:
                    logging.error(f"[dump] Failed to send copy to dump channel {dump_channel}: {de}")

        if is_pm_delivery and auto_delete_enabled and sent_message:
            asyncio.create_task(_delayed_delete(context.bot, target_chat_id, sent_message.message_id, delete_time_secs))

        if is_pm_delivery:
            try:
                await context.bot.send_message(
                    chat_id=target_chat_id,
                    text=f"✅ *{_esc(book['title'])}* has been successfully delivered to your DM!",
                    parse_mode="Markdown"
                )
            except Exception:
                pass
            await status.edit_text(
                f"✅ *Done!* {_esc(fname)} has been successfully sent to your DM 💬",
                parse_mode="Markdown"
            )
        else:
            await status.edit_text(f"✅ *Done!* {_esc(fname)} sent.", parse_mode="Markdown")
    except Exception as e:
        book_page_url = book.get("url") or f"{BASE_URL}/md5/{md5}"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📥 Direct Download", url=direct_url)],
            [InlineKeyboardButton("📖 Book Page", url=book_page_url)]
        ])
        await status.edit_text(
            f"⚠️ *Could not send file via Telegram* — download directly:\n_{_esc(book['title'])}_",
            parse_mode="Markdown",
            reply_markup=keyboard
        )
    finally:
        try:
            os.remove(file_path)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# MD5 Direct Command
# ---------------------------------------------------------------------------

async def md5_download_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _register_pm_if_private(update)
    if not _is_service_enabled():
        if update.message:
            await update.message.reply_text("🔴 Bot service is currently offline.")
        return

    if not update.message:
        return

    text = (update.message.text or "").strip()
    match = re.search(r'/(?:start_)?md5_?([a-fA-F0-9]{32})', text)
    if not match:
        return

    md5 = match.group(1)
    await _process_md5_download(md5, update, context)


# ---------------------------------------------------------------------------
# Welcome message on new members joining
# ---------------------------------------------------------------------------

async def new_member_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_service_enabled():
        return

    if not update.message:
        return

    cfg = _load_cfg()
    welcome = cfg.get("welcome", {})
    if not welcome.get("enabled", True):
        return

    welcome_text = welcome.get("text", "")
    welcome_photo = welcome.get("photo_file_id", None)

    if not welcome_text and not welcome_photo:
        return

    for member in update.message.new_chat_members:
        if member.is_bot:
            continue

        name = member.full_name or member.first_name or "there"
        personalized = welcome_text.replace("{name}", name).replace("{first_name}", name)

        try:
            if welcome_photo:
                await update.message.reply_photo(
                    photo=welcome_photo,
                    caption=personalized or None,
                    parse_mode="Markdown",
                )
            elif personalized:
                await update.message.reply_text(personalized, parse_mode="Markdown")
        except Exception as ex:
            logger.warning("Welcome message failed: %s", ex)


# ---------------------------------------------------------------------------
# Error handler
# ---------------------------------------------------------------------------

async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    err = context.error
    if isinstance(err, (NetworkError, TimedOut)):
        logger.warning("Transient network error (auto-retry): %s", err)
        return
    logger.error("Unhandled exception", exc_info=err)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    _load_db_source_ids()

    def wrap_gated(func, allow_connect=False):
        from functools import wraps
        @wraps(func)
        async def wrapper(update, context, *args, **kwargs):
            if not _is_chat_allowed(update, allow_connect=allow_connect):
                return
            return await func(update, context, *args, **kwargs)
        return wrapper

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .build()
    )

    # Core commands
    app.add_handler(CommandHandler("start", wrap_gated(cmd_start)))
    app.add_handler(CommandHandler("help", wrap_gated(cmd_help)))
    app.add_handler(CommandHandler("status", wrap_gated(cmd_status)))
    app.add_handler(CommandHandler("search", wrap_gated(cmd_search), block=False))

    # Admin commands
    app.add_handler(CommandHandler("mode", wrap_gated(cmd_mode)))
    app.add_handler(CommandHandler("setsearchmode", wrap_gated(cmd_set_search_mode)))
    app.add_handler(CommandHandler("connect", wrap_gated(cmd_connect, allow_connect=True)))
    app.add_handler(CommandHandler("disconnect", wrap_gated(cmd_disconnect)))
    app.add_handler(CommandHandler("enablepm", wrap_gated(cmd_enable_pm)))
    app.add_handler(CommandHandler("disablepm", wrap_gated(cmd_disable_pm)))
    app.add_handler(CommandHandler("setwelcome", wrap_gated(cmd_setwelcome)))
    app.add_handler(CommandHandler("noresult", wrap_gated(cmd_noresult)))
    app.add_handler(CommandHandler("service", wrap_gated(cmd_service)))
    app.add_handler(CommandHandler("auto_delete", wrap_gated(cmd_auto_delete)))
    app.add_handler(CommandHandler("deletetime", wrap_gated(cmd_deletetime)))
    app.add_handler(CommandHandler("dump", wrap_gated(cmd_dump)))
    app.add_handler(CommandHandler("pm_search", wrap_gated(cmd_pm_search)))
    app.add_handler(CommandHandler("read", wrap_gated(cmd_read)))
    app.add_handler(CommandHandler("broadcast", wrap_gated(cmd_broadcast)))

    # Database source commands
    app.add_handler(CommandHandler("connectdb", wrap_gated(cmd_connectdb, allow_connect=True)))
    app.add_handler(CommandHandler("index", wrap_gated(cmd_index)))
    app.add_handler(CommandHandler("cancel", wrap_gated(cmd_cancel_index)))
    app.add_handler(CommandHandler("dbsearch", wrap_gated(cmd_dbsearch), block=False))
    app.add_handler(CommandHandler("showdbs", wrap_gated(cmd_showdbs)))
    app.add_handler(CommandHandler("removedb", wrap_gated(cmd_removedb)))

    # Welcome on new members joining
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, wrap_gated(new_member_handler)))

    # MD5 direct download command handler (must be registered before msg_handler)
    app.add_handler(MessageHandler(filters.Regex(r'^/(?:start_)?md5_?([a-fA-F0-9]{32})(?:@\w+)?$'), wrap_gated(md5_download_handler), block=False))

    # Database result callbacks
    app.add_handler(CallbackQueryHandler(db_result_cb, pattern=r'^(db:|db_online|db_close)'))

    # Auto-index attachments posted into connected database source chats
    app.add_handler(MessageHandler(filters.ATTACHMENT, attachment_handler, block=False))

    # Text / hashtag search
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, wrap_gated(msg_handler), block=False))

    app.add_error_handler(_error_handler)

    print("🤖 Bot is running…")
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()

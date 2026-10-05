import os
import random
import sqlite3
import asyncio
import threading
import re
import textwrap
import requests
from datetime import datetime, timezone, timedelta
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont

import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None


# =========================================================
# CONFIG
# =========================================================

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
SUPPORT_CHANNEL_ID = int(os.getenv("SUPPORT_CHANNEL_ID", "0") or 0)
BOT_OWNER_ID = 1013140093105623060

# Auto role: only enabled for the configured main/original server.
def _env_int(name: str) -> int:
    value = os.getenv(name, "").strip()
    if not value:
        return 0
    try:
        return int(value)
    except ValueError:
        print(f"[CONFIG] {name} must be a numeric Discord ID.")
        return 0

AUTO_ROLE_GUILD_ID = _env_int("AUTO_ROLE_GUILD_ID")
AUTO_ROLE_ID = _env_int("AUTO_ROLE_ID")

# FREE: giữ AI cũ của bot (Gemini).
# Gemini Free: hỗ trợ tối đa 100 API key và tự chuyển key khi quota/rate-limit hết.
# Nếu .env cũ chỉ có GEMINI_API_KEY thì vẫn dùng được bình thường.
GEMINI_API_KEYS = []
for _i in range(1, 101):
    _key = os.getenv(f"GEMINI_API_KEY_{_i}", "").strip()
    if _key:
        GEMINI_API_KEYS.append((_i, _key))

# Tương thích ngược: nếu chưa có GEMINI_API_KEY_1..100 thì dùng key cũ.
if not GEMINI_API_KEYS:
    _legacy_key = os.getenv("GEMINI_API_KEY", "").strip()
    if _legacy_key:
        GEMINI_API_KEYS = [(1, _legacy_key)]

# Vị trí key hiện tại. Không đổi key sau mỗi tin nhắn; chỉ chuyển khi key hiện tại
# gặp lỗi quota/rate-limit hoặc lỗi xác thực khiến key không dùng được.
GEMINI_KEY_INDEX = 0
GEMINI_KEY_LOCK = threading.Lock()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite").strip()

# STANDARD/PREMIUM: AI mới qua OpenAI. Đổi model bằng .env.
STANDARD_MODEL = os.getenv("STANDARD_MODEL", "gpt-6-sol")
PREMIUM_MODEL = os.getenv("PREMIUM_MODEL", "gpt-6-astra")

MIN_INTERVAL = 5
MAX_INTERVAL = 15
DEFAULT_INTERVAL = 6
EARLY_REPLY_CHANCE = 0.10

DB_FILE = os.getenv("DB_FILE", "memory.db")

MEMORY_TRIGGER = 270
MEMORY_KEEP = 100
MAX_LEARNED_FACTS = 100
MAX_GENZ_TERMS = 300

# GIF:
# Có thể thêm GIF vào database bằng /gif_add <từ_khóa> <url>
# Sau đó AI có thể dùng [GIF:từ_khóa].
MAX_GIF_RESULTS = 5


# =========================================================
# DISCORD
# =========================================================

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
bot.remove_command("help")


# =========================================================
# RUNTIME
# =========================================================

ai_enabled = {}
ai_channels = {}  # Legacy: most recently configured channel per server
ai_channel_states = {}  # (guild_id, channel_id) -> enabled
reply_intervals = {}
message_counts = {}
learning_tasks = {}
temporary_ban_tasks = {}


# =========================================================
# DATABASE
# =========================================================

def get_db():
    return sqlite3.connect(DB_FILE)


def init_db():
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER,
            channel_id INTEGER,
            user_id INTEGER,
            username TEXT,
            content TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS learned_memory (
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            summary TEXT,
            PRIMARY KEY (guild_id, channel_id)
        )
    """)

    # Migrate the old server-wide summary table to channel-based summaries.
    cur.execute("PRAGMA table_info(learned_memory)")
    memory_columns = [row[1] for row in cur.fetchall()]
    if memory_columns and "channel_id" not in memory_columns:
        cur.execute("ALTER TABLE learned_memory RENAME TO learned_memory_legacy")
        cur.execute("""
            CREATE TABLE learned_memory (
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                summary TEXT,
                PRIMARY KEY (guild_id, channel_id)
            )
        """)
        cur.execute("""
            INSERT INTO learned_memory (guild_id, channel_id, summary)
            SELECT guild_id, 0, summary FROM learned_memory_legacy
        """)
        cur.execute("DROP TABLE learned_memory_legacy")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS learned_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER,
            channel_id INTEGER DEFAULT 0,
            user_id INTEGER,
            username TEXT,
            fact TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Migrate older server-wide facts to channel-aware facts.
    cur.execute("PRAGMA table_info(learned_facts)")
    fact_columns = [row[1] for row in cur.fetchall()]
    if fact_columns and "channel_id" not in fact_columns:
        cur.execute("ALTER TABLE learned_facts ADD COLUMN channel_id INTEGER DEFAULT 0")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS genz_terms (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER,
            term TEXT,
            meaning TEXT,
            example TEXT,
            confidence REAL DEFAULT 0.5,
            usage_count INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(guild_id, term)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS gifs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER,
            keyword TEXT,
            url TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS gif_memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            url TEXT NOT NULL,
            source_message_id INTEGER,
            sender_id INTEGER,
            context TEXT,
            uses INTEGER DEFAULT 0,
            created_at REAL NOT NULL,
            last_used REAL,
            UNIQUE(guild_id, url)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            guild_id INTEGER PRIMARY KEY,
            ai_enabled INTEGER DEFAULT 0,
            ai_channel_id INTEGER,
            reply_interval INTEGER DEFAULT 6
        )
    """)

    # Per-channel AI configuration; adding a channel must not replace another.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ai_channel_settings (
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (guild_id, channel_id)
        )
    """)
    cur.execute("""
        INSERT OR IGNORE INTO ai_channel_settings (guild_id, channel_id, enabled)
        SELECT guild_id, ai_channel_id, ai_enabled FROM settings
        WHERE ai_channel_id IS NOT NULL
    """)

    # Language is stored per channel. Migrate the old guild-level table safely.
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='language_settings'")
    language_table_exists = cur.fetchone() is not None

    if language_table_exists:
        columns = [row[1] for row in cur.execute("PRAGMA table_info(language_settings)").fetchall()]
        if "channel_id" not in columns:
            # Keep the old data as a backup, but do not use guild_id to decide language.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS language_settings_legacy (
                    guild_id INTEGER PRIMARY KEY,
                    language TEXT NOT NULL DEFAULT 'en'
                )
            """)
            cur.execute("""
                INSERT OR REPLACE INTO language_settings_legacy (guild_id, language)
                SELECT guild_id, language FROM language_settings
            """)
            cur.execute("DROP TABLE language_settings")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS language_settings (
            channel_id INTEGER PRIMARY KEY,
            guild_id INTEGER NOT NULL,
            language TEXT NOT NULL DEFAULT 'en'
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS premium_plans (
            guild_id INTEGER PRIMARY KEY,
            plan TEXT NOT NULL DEFAULT 'free',
            expires_at TEXT,
            source TEXT DEFAULT 'manual',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Temporary bans are persisted so they survive bot restarts.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS temporary_bans (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            expires_at REAL NOT NULL,
            reason TEXT,
            PRIMARY KEY (guild_id, user_id)
        )
    """)

    conn.commit()
    conn.close()


# =========================================================\n# PREMIUM / PLAN SYSTEM
# =========================================================
PLAN_FREE = "free"
PLAN_STANDARD = "standard"
PLAN_PREMIUM = "premium"
PLAN_ORDER = {PLAN_FREE: 0, PLAN_STANDARD: 1, PLAN_PREMIUM: 2}
TIER_LIMITS = {
    PLAN_FREE: {"min_interval": 5, "max_interval": 15, "memory_trigger": 270, "memory_keep": 100, "max_facts": 100, "max_genz": 300, "recent": 35, "facts": 100, "genz": 120, "gifs": 25},
    PLAN_STANDARD: {"min_interval": 3, "max_interval": 12, "memory_trigger": 400, "memory_keep": 180, "max_facts": 200, "max_genz": 500, "recent": 60, "facts": 160, "genz": 220, "gifs": 40},
    PLAN_PREMIUM: {"min_interval": 2, "max_interval": 10, "memory_trigger": 600, "memory_keep": 300, "max_facts": 500, "max_genz": 800, "recent": 100, "facts": 300, "genz": 350, "gifs": 60},
}

def get_plan(guild_id):
    if not guild_id:
        return PLAN_FREE
    conn = get_db()
    row = conn.execute("SELECT plan, expires_at FROM premium_plans WHERE guild_id = ?", (guild_id,)).fetchone()
    conn.close()
    if not row or row[0] not in PLAN_ORDER:
        return PLAN_FREE
    if row[1]:
        try:
            from datetime import datetime, timezone
            exp = datetime.fromisoformat(row[1].replace("Z", "+00:00"))
            if exp.tzinfo is None: exp = exp.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) >= exp:
                return PLAN_FREE
        except ValueError:
            pass
    return row[0]

def get_tier_limits(guild_id):
    return TIER_LIMITS.get(get_plan(guild_id), TIER_LIMITS[PLAN_FREE])

def plan_label(plan):
    return {PLAN_FREE: "🆓 Free", PLAN_STANDARD: "🔹 Standard", PLAN_PREMIUM: "💎 Premium"}.get(plan, "🆓 Free")

def plan_model(guild_id):
    plan = get_plan(guild_id)
    if plan == PLAN_PREMIUM: return PREMIUM_MODEL
    if plan == PLAN_STANDARD: return STANDARD_MODEL
    return GEMINI_MODEL

def set_plan(guild_id, plan, expires_at=None, source="manual"):
    if plan not in PLAN_ORDER: raise ValueError("Invalid plan")
    conn = get_db()
    conn.execute("""
        INSERT INTO premium_plans (guild_id, plan, expires_at, source, updated_at)
        VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(guild_id) DO UPDATE SET
            plan=excluded.plan, expires_at=excluded.expires_at, source=excluded.source, updated_at=CURRENT_TIMESTAMP
    """, (guild_id, plan, expires_at, source))
    conn.commit(); conn.close()

def remove_plan(guild_id):
    conn = get_db(); conn.execute("DELETE FROM premium_plans WHERE guild_id = ?", (guild_id,)); conn.commit(); conn.close()

def get_plan_info(guild_id):
    conn = get_db(); row = conn.execute("SELECT plan, expires_at, source FROM premium_plans WHERE guild_id = ?", (guild_id,)).fetchone(); conn.close()
    if not row: return {"plan": PLAN_FREE, "expires_at": None, "source": "default"}
    return {"plan": row[0], "expires_at": row[1], "source": row[2]}

def openai_available():
    return OpenAI is not None and bool(OPENAI_API_KEY)

OPENAI_CLIENT = OpenAI(api_key=OPENAI_API_KEY) if openai_available() else None

# =========================================================
# LANGUAGE
# =========================================================

LANG_EN = "en"
LANG_VI = "vi"
LANGUAGE_LABELS = {LANG_EN: "English", LANG_VI: "Tiếng Việt"}

def get_language(channel_id):
    """Return the language configured for this channel. New channels default to English."""
    if not channel_id:
        return LANG_EN
    conn = get_db()
    row = conn.execute(
        "SELECT language FROM language_settings WHERE channel_id = ?",
        (channel_id,),
    ).fetchone()
    conn.close()
    return row[0] if row and row[0] in (LANG_EN, LANG_VI) else LANG_EN


def set_language(channel_id, guild_id, language):
    """Set language only for one channel."""
    language = language if language in (LANG_EN, LANG_VI) else LANG_EN
    conn = get_db()
    conn.execute("""
        INSERT INTO language_settings (channel_id, guild_id, language)
        VALUES (?, ?, ?)
        ON CONFLICT(channel_id) DO UPDATE SET
            guild_id = excluded.guild_id,
            language = excluded.language
    """, (channel_id, guild_id, language))
    conn.commit()
    conn.close()


def tr(channel_id, english, vietnamese):
    return vietnamese if get_language(channel_id) == LANG_VI else english


def channel_id_of(obj):
    if isinstance(obj, discord.Interaction):
        return obj.channel.id if obj.channel else None
    return getattr(getattr(obj, "channel", None), "id", None)


# =========================================================
# SETTINGS
# =========================================================

def save_settings(guild_id):
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO settings
        (guild_id, ai_enabled, ai_channel_id, reply_interval)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(guild_id)
        DO UPDATE SET
            ai_enabled = excluded.ai_enabled,
            ai_channel_id = excluded.ai_channel_id,
            reply_interval = excluded.reply_interval
    """, (
        guild_id,
        1 if ai_enabled.get(guild_id, False) else 0,
        ai_channels.get(guild_id),
        reply_intervals.get(guild_id, DEFAULT_INTERVAL)
    ))

    conn.commit()
    conn.close()


def load_settings():
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        SELECT guild_id, ai_enabled, ai_channel_id, reply_interval
        FROM settings
    """)

    for guild_id, enabled, channel_id, interval in cur.fetchall():
        ai_enabled[guild_id] = bool(enabled)
        ai_channels[guild_id] = channel_id
        reply_intervals[guild_id] = interval
        message_counts[guild_id] = 0

    for guild_id, channel_id, enabled in cur.execute(
        "SELECT guild_id, channel_id, enabled FROM ai_channel_settings"
    ).fetchall():
        ai_channel_states[(guild_id, channel_id)] = bool(enabled)

    conn.close()


def save_ai_channel_state(guild_id, channel_id, enabled):
    ai_channel_states[(guild_id, channel_id)] = bool(enabled)
    conn = get_db()
    conn.execute(
        """INSERT INTO ai_channel_settings (guild_id, channel_id, enabled)
           VALUES (?, ?, ?)
           ON CONFLICT(guild_id, channel_id) DO UPDATE SET enabled=excluded.enabled""",
        (guild_id, channel_id, 1 if enabled else 0),
    )
    conn.commit()
    conn.close()


# =========================================================
# MEMORY
# =========================================================

def save_message(message):
    if not message.guild:
        return

    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO messages
        (guild_id, channel_id, user_id, username, content)
        VALUES (?, ?, ?, ?, ?)
    """, (
        message.guild.id,
        message.channel.id,
        message.author.id,
        message.author.display_name,
        message.content
    ))

    conn.commit()
    conn.close()


def get_message_count(guild_id, channel_id=None):
    conn = get_db()
    cur = conn.cursor()
    if channel_id is None:
        cur.execute(
            "SELECT COUNT(*) FROM messages WHERE guild_id = ?",
            (guild_id,)
        )
    else:
        cur.execute(
            "SELECT COUNT(*) FROM messages WHERE guild_id = ? AND channel_id = ?",
            (guild_id, channel_id)
        )
    count = cur.fetchone()[0]
    conn.close()
    return count


def get_recent_messages(guild_id, limit=35):
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        SELECT username, content
        FROM messages
        WHERE guild_id = ?
        ORDER BY id DESC
        LIMIT ?
    """, (guild_id, limit))

    rows = cur.fetchall()
    conn.close()

    rows.reverse()
    return rows


def get_summary(guild_id, channel_id):
    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        "SELECT summary FROM learned_memory WHERE guild_id = ? AND channel_id = ?",
        (guild_id, channel_id)
    )

    row = cur.fetchone()
    conn.close()

    return row[0] if row else ""


def save_summary(guild_id, channel_id, summary):
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO learned_memory (guild_id, channel_id, summary)
        VALUES (?, ?, ?)
        ON CONFLICT(guild_id, channel_id)
        DO UPDATE SET summary = excluded.summary
    """, (guild_id, channel_id, summary))

    conn.commit()
    conn.close()


# =========================================================
# LEARNED FACTS
# =========================================================

def get_facts(guild_id, channel_id, limit=100):
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        SELECT id, username, fact
        FROM learned_facts
        WHERE guild_id = ? AND channel_id = ?
        ORDER BY id DESC
        LIMIT ?
    """, (guild_id, channel_id, limit))

    rows = cur.fetchall()
    conn.close()

    rows.reverse()
    return rows


def save_fact(guild_id, channel_id, user_id, username, fact):
    fact = fact.strip()

    if len(fact) < 3:
        return

    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        SELECT id FROM learned_facts
        WHERE guild_id = ? AND channel_id = ? AND fact = ?
        LIMIT 1
    """, (guild_id, channel_id, fact))

    if cur.fetchone():
        conn.close()
        return

    cur.execute("""
        INSERT INTO learned_facts
        (guild_id, channel_id, user_id, username, fact)
        VALUES (?, ?, ?, ?, ?)
    """, (guild_id, channel_id, user_id, username, fact))

    cur.execute("""
        DELETE FROM learned_facts
        WHERE guild_id = ? AND channel_id = ?
        AND id NOT IN (
            SELECT id FROM learned_facts
            WHERE guild_id = ? AND channel_id = ?
            ORDER BY id DESC
            LIMIT ?
        )
    """, (
        guild_id, channel_id, guild_id, channel_id,
        get_tier_limits(guild_id)["max_facts"]
    ))

    conn.commit()
    conn.close()


def update_fact(guild_id, channel_id, old_fact, new_fact, user_id=None, username=None):
    old_fact = old_fact.strip()
    new_fact = new_fact.strip()
    if len(old_fact) < 3 or len(new_fact) < 3:
        return False

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT id FROM learned_facts
        WHERE guild_id = ? AND channel_id = ? AND fact = ?
        ORDER BY id DESC LIMIT 1
    """, (guild_id, channel_id, old_fact))
    row = cur.fetchone()
    if not row:
        conn.close()
        return False

    fact_id = row[0]
    cur.execute("""
        UPDATE learned_facts
        SET fact = ?, user_id = COALESCE(?, user_id), username = COALESCE(?, username), created_at = CURRENT_TIMESTAMP
        WHERE id = ? AND guild_id = ? AND channel_id = ?
    """, (new_fact, user_id, username, fact_id, guild_id, channel_id))
    conn.commit()
    conn.close()
    return True


def delete_fact(guild_id, channel_id, fact):
    fact = fact.strip()
    if not fact:
        return False

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        DELETE FROM learned_facts
        WHERE guild_id = ? AND channel_id = ? AND fact = ?
    """, (guild_id, channel_id, fact))
    deleted = cur.rowcount > 0
    conn.commit()
    conn.close()
    return deleted


# =========================================================
# GEN Z / INTERNET SLANG
# =========================================================

DEFAULT_GENZ = {
    "son": "cách gọi một người theo kiểu internet/meme; nghĩa chính xác phụ thuộc ngữ cảnh",
    "ez": "easy; dễ, thắng hoặc làm gì đó khá nhẹ nhàng",
    "know ball": "người đó thực sự hiểu biết về chủ đề đang nói, thường là lời khen",
    "knows ball": "tương tự know ball; hiểu vấn đề/thật sự có kiến thức",
    "bá khí": "ngầu, có khí chất, áp đảo hoặc tạo cảm giác rất mạnh",
    "cooked": "toang, gặp vấn đề hoặc gần như hết cứu",
    "let him cook": "cứ để người đó làm tiếp, có thể đang làm ra thứ hay",
    "w": "win; điều tốt, thắng, thành công",
    "l": "loss; thua, thất bại hoặc điều không ổn",
    "aura": "khí chất/độ ngầu được thể hiện trong tình huống",
    "locked in": "tập trung cao độ",
    "bro is him": "người đó rất đỉnh hoặc đúng là nhân vật chính trong tình huống đó",
    "clown": "chọc một người đang làm điều ngớ ngẩn/lố",
}


def seed_default_genz():
    conn = get_db()
    cur = conn.cursor()

    for term, meaning in DEFAULT_GENZ.items():
        cur.execute("""
            INSERT OR IGNORE INTO genz_terms
            (guild_id, term, meaning, example, confidence, usage_count)
            SELECT guild_id, ?, ?, '', 0.85, 1
            FROM settings
            """, (term, meaning))

    conn.commit()
    conn.close()


def get_genz_terms(guild_id, limit=150):
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        SELECT term, meaning, example, confidence, usage_count
        FROM genz_terms
        WHERE guild_id = ?
        ORDER BY usage_count DESC, confidence DESC
        LIMIT ?
    """, (guild_id, limit))

    rows = cur.fetchall()
    conn.close()

    return rows


def save_genz_term(guild_id, term, meaning, example=""):
    term = term.strip().lower()
    meaning = meaning.strip()
    example = example.strip()

    if not term or not meaning:
        return

    if len(term) > 80 or len(meaning) > 300:
        return

    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        SELECT id, confidence, usage_count
        FROM genz_terms
        WHERE guild_id = ? AND term = ?
    """, (guild_id, term))

    row = cur.fetchone()

    if row:
        term_id, confidence, usage_count = row
        new_confidence = min(1.0, confidence + 0.03)

        cur.execute("""
            UPDATE genz_terms
            SET meaning = ?,
                example = ?,
                confidence = ?,
                usage_count = ?
            WHERE id = ?
        """, (
            meaning,
            example,
            new_confidence,
            usage_count + 1,
            term_id
        ))
    else:
        cur.execute("""
            INSERT INTO genz_terms
            (guild_id, term, meaning, example, confidence, usage_count)
            VALUES (?, ?, ?, ?, 0.55, 1)
        """, (guild_id, term, meaning, example))

    cur.execute("""
        DELETE FROM genz_terms
        WHERE guild_id = ?
        AND id NOT IN (
            SELECT id FROM genz_terms
            WHERE guild_id = ?
            ORDER BY usage_count DESC, confidence DESC
            LIMIT ?
        )
    """, (guild_id, guild_id, get_tier_limits(guild_id)["max_genz"]))

    conn.commit()
    conn.close()


# =========================================================
# GIF MEMORY — HỌC GIF NGƯỜI DÙNG ĐÃ GỬI
# =========================================================

def is_server_gif_url(url: str) -> bool:
    if not url:
        return False

    u = url.lower().strip().strip("<>")

    # GIF trực tiếp
    if ".gif" in u or ".gifv" in u:
        return True

    # Discord/GIF picker thường expose các URL này.
    gif_hosts = (
        "tenor.com/",
        "media.tenor.com/",
        "c.tenor.com/",
        "giphy.com/",
        "media.giphy.com/",
        "i.giphy.com/",
    )
    return any(host in u for host in gif_hosts) and (
        "/view/" in u or "/gifs/" in u or "gif" in u
    )


def extract_server_gifs(message: discord.Message):
    urls = []

    # 1. Attachment .gif
    for attachment in message.attachments:
        url = attachment.url
        filename = (attachment.filename or "").lower()
        content_type = (attachment.content_type or "").lower()

        if (
            "image/gif" in content_type
            or filename.endswith(".gif")
            or is_server_gif_url(url)
        ):
            urls.append(url)

    # 2. Embed từ Discord GIF picker / Tenor / Giphy
    for embed in message.embeds:
        candidates = [
            getattr(embed, "url", None),
            getattr(getattr(embed, "image", None), "url", None),
            getattr(getattr(embed, "thumbnail", None), "url", None),
            getattr(getattr(embed, "video", None), "url", None),
        ]

        for url in candidates:
            if url and is_server_gif_url(url):
                urls.append(url)

    # 3. URL nằm trực tiếp trong message content
    for raw_url in re.findall(r"https?://[^\s<>]+", message.content or ""):
        url = raw_url.rstrip(".,!?)]}>")
        if is_server_gif_url(url):
            urls.append(url)

    # Khử trùng lặp
    return list(dict.fromkeys(urls))


async def remember_server_gif(message: discord.Message):
    """Tự động lưu GIF mà người thật đã gửi trong server."""
    if not message.guild or message.author.bot:
        return

    urls = extract_server_gifs(message)
    if not urls:
        return

    context = (message.content or "").strip()[:500]

    conn = get_db()
    cur = conn.cursor()

    for url in urls:
        cur.execute(
            "SELECT id FROM gif_memory WHERE guild_id = ? AND url = ?",
            (message.guild.id, url),
        )
        existing = cur.fetchone()

        if existing:
            # GIF đã biết: cập nhật context nếu lần này có context rõ hơn.
            if context:
                cur.execute(
                    """UPDATE gif_memory
                       SET context = ?, sender_id = ?, source_message_id = ?
                       WHERE id = ?""",
                    (
                        context,
                        message.author.id,
                        message.id,
                        existing[0],
                    ),
                )
        else:
            cur.execute(
                """INSERT INTO gif_memory
                   (guild_id, url, source_message_id, sender_id, context, uses, created_at)
                   VALUES (?, ?, ?, ?, ?, 0, ?)""",
                (
                    message.guild.id,
                    url,
                    message.id,
                    message.author.id,
                    context,
                    __import__("time").time(),
                ),
            )

    # Giữ tối đa 500 GIF/server.
    cur.execute(
        """DELETE FROM gif_memory
           WHERE guild_id = ?
           AND id NOT IN (
               SELECT id
               FROM gif_memory
               WHERE guild_id = ?
               ORDER BY created_at DESC
               LIMIT 500
           )""",
        (message.guild.id, message.guild.id),
    )

    conn.commit()
    conn.close()


def get_server_gif_memory(guild_id: int, limit: int = 30):
    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        """SELECT id, url, context, uses
           FROM gif_memory
           WHERE guild_id = ?
           ORDER BY uses DESC, created_at DESC
           LIMIT ?""",
        (guild_id, limit),
    )

    rows = cur.fetchall()
    conn.close()
    return rows


def choose_server_gif(guild_id: int, context: str = ""):
    """Chọn GIF đã từng được người dùng gửi; ưu tiên GIF có context gần với tin nhắn."""
    rows = get_server_gif_memory(guild_id, 80)

    if not rows:
        return None

    # Từ khóa đơn giản để ưu tiên GIF có context tương tự.
    query_words = {
        w.lower()
        for w in re.findall(r"[A-Za-zÀ-ỹ0-9_]+", context or "")
        if len(w) >= 2
    }

    scored = []
    for row in rows:
        gif_id, url, gif_context, uses = row
        context_words = {
            w.lower()
            for w in re.findall(r"[A-Za-zÀ-ỹ0-9_]+", gif_context or "")
            if len(w) >= 2
        }

        overlap = len(query_words & context_words)
        score = overlap * 10 + min(int(uses or 0), 10)

        # Có chút random để bot không spam cùng một GIF.
        score += random.random() * 3
        scored.append((score, row))

    scored.sort(key=lambda x: x[0], reverse=True)
    best = scored[:min(10, len(scored))]
    _, chosen = random.choice(best)

    gif_id, url, _, _ = chosen

    conn = get_db()
    conn.execute(
        "UPDATE gif_memory SET uses = uses + 1, last_used = ? WHERE id = ?",
        (__import__("time").time(), gif_id),
    )
    conn.commit()
    conn.close()

    return url


# Các hàm cũ giữ lại để không làm hỏng DB cũ.
def add_gif(guild_id, keyword, url):
    conn = get_db()
    conn.execute(
        "INSERT INTO gifs (guild_id, keyword, url) VALUES (?, ?, ?)",
        (guild_id, keyword.lower().strip(), url.strip()),
    )
    conn.commit()
    conn.close()


def get_gifs(guild_id, keyword):
    conn = get_db()
    rows = conn.execute(
        """SELECT url FROM gifs
           WHERE guild_id = ? AND keyword = ?
           ORDER BY RANDOM() LIMIT ?""",
        (guild_id, keyword.lower().strip(), MAX_GIF_RESULTS),
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


def get_gif_keywords(guild_id):
    conn = get_db()
    rows = conn.execute(
        "SELECT DISTINCT keyword FROM gifs WHERE guild_id = ? ORDER BY keyword",
        (guild_id,),
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


# AI BACKENDS
# =========================================================

def _gemini_key_snapshot():
    """Lấy key hiện tại một cách thread-safe."""
    if not GEMINI_API_KEYS:
        return None, None
    with GEMINI_KEY_LOCK:
        index = GEMINI_KEY_INDEX % len(GEMINI_API_KEYS)
        slot, key = GEMINI_API_KEYS[index]
        return index, (slot, key)


def _rotate_gemini_key(expected_index):
    """Chuyển sang key tiếp theo, tránh ghi đè rotation của request khác."""
    global GEMINI_KEY_INDEX
    if not GEMINI_API_KEYS:
        return
    with GEMINI_KEY_LOCK:
        if GEMINI_KEY_INDEX == expected_index:
            GEMINI_KEY_INDEX = (GEMINI_KEY_INDEX + 1) % len(GEMINI_API_KEYS)
            slot = GEMINI_API_KEYS[GEMINI_KEY_INDEX][0]
            print(f"[GEMINI] Switching to API key #{slot}")


def ask_gemini(prompt):
    """Gemini Free backend with up to 100 rotating API keys."""
    if not GEMINI_API_KEYS:
        print("[GEMINI ERROR] No Gemini API key configured.")
        return None

    # Thử tối đa một vòng qua toàn bộ key. Key hiện tại được ưu tiên;
    # chỉ chuyển key khi request thất bại do quota/rate-limit/auth.
    tried = set()
    while len(tried) < len(GEMINI_API_KEYS):
        key_index, key_info = _gemini_key_snapshot()
        if key_index is None or key_info is None:
            return None

        slot, api_key = key_info
        if key_index in tried:
            # Một request khác vừa rotation; lấy snapshot mới.
            continue
        tried.add(key_index)

        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{GEMINI_MODEL}:generateContent"
        )
        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": prompt}],
                }
            ],
            "generationConfig": {
                "temperature": 0.75,
                "topP": 0.9,
            },
        }

        try:
            response = requests.post(
                url,
                params={"key": api_key},
                json=payload,
                timeout=120,
            )

            if response.status_code == 200:
                data = response.json()
                candidates = data.get("candidates") or []
                if not candidates:
                    # Safety/content block không phải lỗi quota: không đổi key.
                    feedback = data.get("promptFeedback") or {}
                    block_reason = feedback.get("blockReason")
                    if block_reason:
                        print(f"[GEMINI ERROR] Request blocked: {block_reason}")
                        return None
                    print(f"[GEMINI ERROR] No candidates returned with key #{slot}: {data}")
                    return None

                parts = candidates[0].get("content", {}).get("parts", [])
                answer = "".join(
                    part.get("text", "")
                    for part in parts
                    if isinstance(part, dict)
                ).strip()
                if answer:
                    return answer

                print(f"[GEMINI ERROR] Empty response with key #{slot}.")
                return None

            # 429 = quota/rate limit: chuyển key ngay.
            # 401/403: key có thể hết hạn/quyền không hợp lệ: cũng thử key kế tiếp.
            if response.status_code in (401, 403, 429):
                try:
                    detail = response.json().get("error", {}).get("message", response.text)
                except Exception:
                    detail = response.text
                print(f"[GEMINI] Key #{slot} unavailable HTTP {response.status_code}: {detail}")
                _rotate_gemini_key(key_index)
                continue

            try:
                detail = response.json().get("error", {}).get("message", response.text)
            except Exception:
                detail = response.text
            print(f"[GEMINI ERROR] HTTP {response.status_code} with key #{slot}: {detail}")
            return None

        except requests.RequestException as e:
            print(f"[GEMINI] Network error with key #{slot}: {e!r}; trying next key.")
            _rotate_gemini_key(key_index)
            continue
        except Exception as e:
            print(f"[GEMINI ERROR] {e!r}")
            return None

    print("[GEMINI] All configured API keys are currently unavailable/exhausted.")
    return None


def ask_openai(prompt, model):
    if OPENAI_CLIENT is None:
        print("[OPENAI ERROR] OPENAI_API_KEY or package openai is not configured.")
        return None
    try:
        response = OPENAI_CLIENT.responses.create(
            model=model,
            input=[{"role": "user", "content": prompt}],
        )
        return (response.output_text or "").strip()
    except Exception as e:
        print("[OPENAI ERROR]", repr(e))
        return None


async def ask_ai_async(guild_id, prompt):
    plan = get_plan(guild_id)
    if plan == PLAN_FREE:
        return await asyncio.to_thread(ask_gemini, prompt)
    return await asyncio.to_thread(ask_openai, prompt, plan_model(guild_id))

# =========================================================
# MESSAGE HELPERS
# =========================================================

def clean_message_content(message):
    content = message.content

    if bot.user:
        content = content.replace(f"<@{bot.user.id}>", "")
        content = content.replace(f"<@!{bot.user.id}>", "")

    return content.strip()


async def is_reply_to_bot(message):
    if not bot.user or not message.reference:
        return False

    resolved = message.reference.resolved

    if isinstance(resolved, discord.Message):
        return resolved.author.id == bot.user.id

    if message.reference.message_id:
        try:
            replied = await message.channel.fetch_message(
                message.reference.message_id
            )
            return replied.author.id == bot.user.id
        except Exception:
            return False

    return False


def get_server_emotes(guild):
    if not guild:
        return []

    return [str(e) for e in guild.emojis]


def sanitize_ai_text(text, guild):
    """
    Chỉ cho AI dùng custom emoji thật sự tồn tại trong server.
    Nếu AI tự bịa <:name:id>, loại phần đó ra.
    """
    valid = {str(e) for e in guild.emojis}

    def repl(match):
        candidate = match.group(0)
        return candidate if candidate in valid else ""

    text = re.sub(r"<a?:[A-Za-z0-9_]+:\d+>", repl, text)
    return text.strip()


# =========================================================
# MEMORY COMPACTION
# =========================================================

async def compact_memory(guild_id, channel_id):
    count = get_message_count(guild_id, channel_id)

    if count < get_tier_limits(guild_id)["memory_trigger"]:
        return

    conn = get_db()
    cur = conn.cursor()

    remove_count = max(1, count - get_tier_limits(guild_id)["memory_keep"])

    cur.execute("""
        SELECT id, username, content
        FROM messages
        WHERE guild_id = ? AND channel_id = ?
        ORDER BY id ASC
        LIMIT ?
    """, (guild_id, channel_id, remove_count))

    rows = cur.fetchall()

    if not rows:
        conn.close()
        return

    old_text = "\n".join(
        f"{username}: {content}"
        for _, username, content in rows
    )

    old_summary = get_summary(guild_id, channel_id)

    prompt = f"""
Bạn là hệ thống memory dài hạn của Discord AI.

SUMMARY CŨ:
{old_summary}

TIN NHẮN CŨ:
{old_text}

Hãy cập nhật summary.

Giữ:
- sở thích lâu dài
- cách người dùng muốn bot nói chuyện
- thông tin ổn định về server
- dự án/chủ đề dài hạn
- kiến thức slang/meme hữu ích
- các quy tắc giao tiếp lâu dài

Không giữ:
- mật khẩu
- token
- API key
- thông tin đăng nhập
- dữ liệu tài chính
- dữ liệu nhạy cảm không cần thiết
- chuyện nhất thời

Chỉ trả về summary mới, ngắn gọn.
"""

    new_summary = await ask_ai_async(guild_id, prompt)

    if new_summary:
        save_summary(guild_id, channel_id, new_summary)

    ids = [row[0] for row in rows]
    placeholders = ",".join("?" for _ in ids)

    cur.execute(
        f"DELETE FROM messages WHERE guild_id = ? AND channel_id = ? AND id IN ({placeholders})",
        [guild_id, channel_id, *ids]
    )

    conn.commit()
    conn.close()

    print(f"[MEMORY] Compacted {len(rows)} messages in {guild_id}")


# =========================================================
# SELF LEARNING
# =========================================================

async def learn_from_message(message):
    if not message.guild:
        return

    content = clean_message_content(message)
    if not content:
        return

    guild_id = message.guild.id
    channel_id = message.channel.id
    existing = get_facts(guild_id, channel_id, 30)
    existing_text = "\n".join(
        f"[{fact_id}] {username}: {fact}" for fact_id, username, fact in existing
    ) or "(chưa có fact nào)"

    prompt = f"""
Bạn là hệ thống tự học của Discord AI.

Các fact hiện có trong channel này:
{existing_text}

Tin nhắn mới:
{content}

Hãy xác định xem tin nhắn có làm thay đổi hoặc hủy một fact đã lưu hay tạo fact mới không.

QUY TẮC FACT:
- Chỉ lưu thông tin có khả năng hữu ích lâu dài.
- Nếu thông tin mới mâu thuẫn với một fact cũ và rõ ràng là thông tin thật mới, hãy UPDATE fact cũ.
- Nếu người dùng đính chính rằng một fact cũ là sai, nói đùa, meme, roleplay, không thật, hoặc không có chuyện đó, hãy DELETE fact cũ.
- Các câu kiểu "tôi yêu X" không được xem là fact chắc chắn nếu ngữ cảnh cho thấy đó chỉ là joke.
- Không xóa fact chỉ vì nó lâu không được nhắc tới.
- Không học mật khẩu, token, API key, thông tin đăng nhập, tài chính hoặc dữ liệu nhạy cảm.

Đầu ra CHỈ được là một dòng theo một trong các dạng sau:
FACT: một fact mới
UPDATE: ID_FACT_CŨ | fact mới
DELETE: ID_FACT_CŨ
NO_LEARN

Nếu tin nhắn không đủ chắc chắn để thay đổi memory, trả về NO_LEARN.
"""

    result = await ask_ai_async(guild_id, prompt)
    if not result:
        return

    result = result.strip()

    if result.startswith("FACT:"):
        fact = result[5:].strip()
        if len(fact) <= 300:
            save_fact(
                guild_id, channel_id, message.author.id,
                message.author.display_name, fact
            )
            print("[LEARN FACT]", fact)

    elif result.startswith("UPDATE:"):
        raw = result[7:].strip()
        parts = [p.strip() for p in raw.split("|", 1)]
        if len(parts) == 2:
            try:
                fact_id = int(parts[0])
            except ValueError:
                return
            new_fact = parts[1]
            if len(new_fact) > 300:
                return

            conn = get_db()
            cur = conn.cursor()
            cur.execute("""
                UPDATE learned_facts
                SET fact = ?, user_id = ?, username = ?, created_at = CURRENT_TIMESTAMP
                WHERE id = ? AND guild_id = ? AND channel_id = ?
            """, (
                new_fact, message.author.id, message.author.display_name,
                fact_id, guild_id, channel_id
            ))
            changed = cur.rowcount > 0
            conn.commit()
            conn.close()
            if changed:
                print("[UPDATE FACT]", fact_id, "->", new_fact)

    elif result.startswith("DELETE:"):
        raw = result[7:].strip()
        try:
            fact_id = int(raw)
        except ValueError:
            return

        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM learned_facts WHERE id = ? AND guild_id = ? AND channel_id = ?",
            (fact_id, guild_id, channel_id)
        )
        deleted = cur.rowcount > 0
        conn.commit()
        conn.close()
        if deleted:
            print("[DELETE FACT]", fact_id)

    elif result.startswith("GENZ:"):
        raw = result[5:].strip()
        parts = [p.strip() for p in raw.split("|")]

        if len(parts) >= 2:
            term = parts[0]
            meaning = parts[1]
            example = parts[2] if len(parts) >= 3 else ""
            save_genz_term(guild_id, term, meaning, example)
            print("[LEARN GENZ]", term, "=", meaning)


# =========================================================
# PROMPT
# =========================================================

def build_ai_prompt(message):
    guild = message.guild
    guild_id = guild.id

    summary = get_summary(guild_id, message.channel.id)
    limits = get_tier_limits(guild_id)
    facts = get_facts(guild_id, message.channel.id, limits["facts"])
    recent = get_recent_messages(guild_id, limits["recent"])
    genz = get_genz_terms(guild_id, limits["genz"])
    emotes = get_server_emotes(guild)
    gif_memory = get_server_gif_memory(guild_id, limits["gifs"])

    current = clean_message_content(message)
    language_name = LANGUAGE_LABELS.get(get_language(message.channel.id), "English")

    prompt = f"""
Bạn là một Discord AI chatbot.

MỤC TIÊU:
- Hiểu ngữ cảnh tốt.
- Trả lời tự nhiên, ngắn gọn khi câu hỏi đơn giản.
- Khi cần thì giải thích rõ.
- Không bịa thông tin.
- Không lặp lại chính mình.
- PHẢI trả lời CHỈ bằng {language_name}.
- Không tự đổi ngôn ngữ theo ngôn ngữ người dùng.
- Không trộn ngôn ngữ trừ khi một tên riêng, thuật ngữ hoặc đoạn mã bắt buộc phải giữ nguyên.

VIBE:
- Có thể dùng internet/Gen Z slang khi đúng ngữ cảnh.
- Hiểu "son", "ez", "know ball", "bá khí", "cooked",
  "W", "L", "aura", "locked in", "let him cook", v.v.
- "Hiểu tiếng tục" không có nghĩa là phải dùng tiếng tục.
- Không chủ động dùng tục nặng như "đụ má", "đụ mẹ" hoặc biến thể.
- Các từ như "gà", "ngáo", "ảo", "clown" có thể hiểu và dùng
  khi phù hợp, nhưng không spam.
- Không xúc phạm người dùng một cách nghiêm túc.

EMOTE:
Server có các custom emote sau:
{", ".join(emotes) if emotes else "(server chưa có custom emote)"}

Chỉ dùng custom emote nếu nó nằm chính xác trong danh sách trên.
Không tự bịa ID emoji.

GIF:
Bot có bộ nhớ GIF được học trực tiếp từ GIF mà người dùng đã gửi trong server.

{chr(10).join(
    f"- GIF_ID={gid} | uses={uses} | context={ctx or '(no text)'}"
    for gid, url, ctx, uses in gif_memory
) if gif_memory else "(no GIFs learned yet)"}

Nếu thật sự phù hợp với tình huống, có thể kết thúc câu trả lời bằng:
[SERVER_GIF]

[SERVER_GIF] nghĩa là dùng lại một GIF đã được người dùng gửi trước đó.
Không tạo URL GIF giả.
Không dùng GIF trong mọi tin nhắn.

================ LONG TERM MEMORY ================
{summary if summary else "(chưa có)"}

================ LEARNED FACTS ================
"""

    if facts:
        for _, username, fact in facts:
            prompt += f"- {username}: {fact}\n"
    else:
        prompt += "(chưa có)\n"

    prompt += "\n================ GEN Z / INTERNET DICTIONARY ================\n"

    if genz:
        for term, meaning, example, confidence, usage_count in genz:
            prompt += f"- {term}: {meaning}"
            if example:
                prompt += f" | ví dụ: {example}"
            prompt += "\n"
    else:
        prompt += "(chưa có)\n"

    prompt += "\n================ RECENT CHAT ================\n"

    if recent:
        for username, content in recent:
            prompt += f"{username}: {content}\n"
    else:
        prompt += "(chưa có)\n"

    prompt += f"""
================ RESPONSE LANGUAGE ================
Current channel language: {language_name}
Reply ONLY in {language_name}.

================ CURRENT MESSAGE ================

{message.author.display_name}: {current}

Hãy trả lời tin nhắn hiện tại.
"""

    return prompt


# =========================================================
# SEND AI RESPONSE + GIF
# =========================================================

async def send_ai_response(message, answer):
    if not answer:
        return

    # AI chỉ được yêu cầu dùng GIF đã học bằng marker này.
    use_gif = bool(
        re.search(r"\[SERVER_GIF\]", answer, flags=re.IGNORECASE)
    )

    answer = re.sub(
        r"\[SERVER_GIF\]",
        "",
        answer,
        flags=re.IGNORECASE
    ).strip()

    answer = sanitize_ai_text(answer, message.guild)

    if answer:
        if len(answer) > 2000:
            answer = answer[:1990] + "..."

        await message.channel.send(answer)

    if use_gif and message.guild:
        gif_url = choose_server_gif(
            message.guild.id,
            message.content
        )

        if gif_url:
            try:
                await message.channel.send(gif_url)
            except Exception as e:
                print(f"[GIF SEND ERROR] {e}")



# =========================================================
# AUTO ROLE
# =========================================================

@bot.event
async def on_member_join(member: discord.Member):
    """Automatically grant the configured role in the configured main server."""
    if member.bot:
        return

    if not AUTO_ROLE_GUILD_ID or not AUTO_ROLE_ID:
        return

    if member.guild.id != AUTO_ROLE_GUILD_ID:
        return

    role = member.guild.get_role(AUTO_ROLE_ID)
    if role is None:
        print(f"[AUTO ROLE] Role {AUTO_ROLE_ID} was not found in guild {member.guild.id}.")
        return

    if role in member.roles:
        return

    try:
        await member.add_roles(role, reason="TOILIX Auto Role")
        print(f"[AUTO ROLE] Added {role.name} to {member} ({member.id}).")
    except discord.Forbidden:
        print(
            "[AUTO ROLE] Permission error. Make sure TOILIX has Manage Roles "
            "and its highest role is above the target role."
        )
    except Exception as e:
        print("[AUTO ROLE ERROR]", repr(e))


# =========================================================
# READY
# =========================================================

@bot.event
async def on_ready():
    init_db()
    load_settings()
    seed_default_genz()
    try:
        await restore_temporary_bans()
    except Exception as e:
        print("[TEMP BAN RESTORE ERROR]", repr(e))

    try:
        # Sync global commands. Discord may take some time to propagate global commands.
        synced = await bot.tree.sync()
        print(f"[BOT] Synced {len(synced)} global slash commands.")
    except Exception as e:
        print("[BOT] Global sync error:", e)

    print("=" * 55)
    print("DISCORD AI BOT ONLINE")
    print(f"Free AI (Gemini): {GEMINI_MODEL}")
    print(f"Standard AI: {STANDARD_MODEL}")
    print(f"Premium AI: {PREMIUM_MODEL}")
    print(f"OpenAI configured: {'YES' if openai_available() else 'NO'}")
    print("Memory: ON")
    print("Self-learning: ON")
    print("Gen Z learning: ON")
    print("Custom emote support: ON")
    print("GIF library: ON")
    print(
        f"Auto role: {'ON' if AUTO_ROLE_GUILD_ID and AUTO_ROLE_ID else 'OFF'}"
    )
    print("Direct mention: ON")
    print("Reply-to-bot: ON")
    print(f"Interval: {MIN_INTERVAL}-{MAX_INTERVAL}")
    print("=" * 55)


# =========================================================
# SLASH COMMANDS
# =========================================================

def owner_profile_link(owner):
    """Return a real Discord user mention for the bot owner."""
    return owner.mention


@bot.tree.command(name="owner", description="Show the bot owner")
@app_commands.checks.has_permissions(manage_guild=True)
async def owner_slash(interaction: discord.Interaction):
    try:
        owner = bot.get_user(BOT_OWNER_ID)
        if owner is None:
            owner = await bot.fetch_user(BOT_OWNER_ID)

        link = owner_profile_link(owner)
        await interaction.response.send_message(
            tr(
                interaction.channel.id if interaction.channel else 0,
                f"👑 **Bot Owner:** {link}",
                f"👑 **Chủ bot:** {link}",
            ),
            silent=True,
        )
    except Exception as e:
        print("[OWNER COMMAND ERROR]", repr(e))
        await interaction.response.send_message(
            tr(
                interaction.channel.id if interaction.channel else 0,
                "❌ Could not retrieve the bot owner.",
                "❌ Không thể lấy thông tin chủ bot.",
            ),
            ephemeral=True,
        )


@bot.command(name="owner")
@commands.has_guild_permissions(manage_guild=True)
async def owner_prefix(ctx):
    try:
        owner = bot.get_user(BOT_OWNER_ID)
        if owner is None:
            owner = await bot.fetch_user(BOT_OWNER_ID)

        link = owner_profile_link(owner)
        await ctx.send(
            tr(
                ctx.channel.id,
                f"👑 **Bot Owner:** {link}",
                f"👑 **Chủ bot:** {link}",
            ),
            silent=True,
        )
    except Exception as e:
        print("[OWNER COMMAND ERROR]", repr(e))
        await ctx.send(
            tr(
                ctx.channel.id,
                "❌ Could not retrieve the bot owner.",
                "❌ Không thể lấy thông tin chủ bot.",
            )
        )


@bot.tree.command(name="language", description="Choose the bot language for this channel")
@app_commands.describe(language="Choose English or Vietnamese")
@app_commands.choices(language=[
    app_commands.Choice(name="English", value="en"),
    app_commands.Choice(name="Tiếng Việt", value="vi"),
])
async def language_command(interaction: discord.Interaction, language: app_commands.Choice[str]):
    if not interaction.guild or not interaction.channel:
        await interaction.response.send_message(
            "This command can only be used in a server." if not interaction.guild
            else "Channel information is unavailable.",
            ephemeral=True,
        )
        return

    set_language(interaction.channel.id, interaction.guild.id, language.value)

    if language.value == LANG_VI:
        await interaction.response.send_message(
            "🇻🇳 Đã chuyển ngôn ngữ bot sang Tiếng Việt cho channel này."
        )
    else:
        await interaction.response.send_message(
            "🇬🇧 Bot language has been set to English for this channel."
        )


@bot.command(name="language")
async def language_prefix(ctx, language: str = ""):
    if not ctx.guild:
        await ctx.send("This command can only be used in a server.")
        return

    value = language.lower().strip()
    if value in ("vi", "vietnamese", "tiengviet", "tiếng_việt"):
        set_language(ctx.channel.id, ctx.guild.id, LANG_VI)
        await ctx.send("🇻🇳 Đã chuyển ngôn ngữ bot sang Tiếng Việt cho channel này.")
    elif value in ("en", "english"):
        set_language(ctx.channel.id, ctx.guild.id, LANG_EN)
        await ctx.send("🇬🇧 Bot language has been set to English for this channel.")
    else:
        await ctx.send(
            tr(
                ctx.channel.id,
                "Use `!language en` or `!language vi`.",
                "Dùng `!language en` hoặc `!language vi`.",
            )
        )


@bot.tree.command(name="ai_on", description="Enable the AI chatbot")
@app_commands.checks.has_permissions(manage_guild=True)
async def ai_on(interaction: discord.Interaction):
    guild_id = interaction.guild.id

    ai_enabled[guild_id] = True
    reply_intervals.setdefault(guild_id, DEFAULT_INTERVAL)
    message_counts[guild_id] = 0

    save_settings(guild_id)
    seed_default_genz()

    await interaction.response.send_message(tr(interaction.channel.id if interaction.channel else 0, "🤖 AI enabled.", "🤖 AI đã bật."))


@bot.tree.command(name="ai_off", description="Disable the AI chatbot")
@app_commands.checks.has_permissions(manage_guild=True)
async def ai_off(interaction: discord.Interaction):
    guild_id = interaction.guild.id

    ai_enabled[guild_id] = False
    save_settings(guild_id)

    await interaction.response.send_message(tr(interaction.channel.id if interaction.channel else 0, "🛑 AI disabled.", "🛑 AI đã tắt."))


@bot.tree.command(
    name="ai_channel",
    description="Enable or disable AI for a selected channel"
)
@app_commands.describe(
    channel="The channel AI should use",
    enabled="True = AI replies here, False = AI stays disabled here"
)
@app_commands.checks.has_permissions(manage_guild=True)
async def ai_channel(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    enabled: bool
):
    guild_id = interaction.guild.id

    # Configure this channel independently; never overwrite other channels.
    ai_channels[guild_id] = channel.id  # legacy/status display only
    save_ai_channel_state(guild_id, channel.id, enabled)
    if enabled:
        ai_enabled[guild_id] = True
        reply_intervals.setdefault(guild_id, DEFAULT_INTERVAL)
        message_counts[(guild_id, channel.id)] = 0

    save_settings(guild_id)

    if enabled:
        message = tr(
            interaction.channel.id if interaction.channel else 0,
            f"✅ AI enabled in {channel.mention}.",
            f"✅ AI đã bật ở {channel.mention}."
        )
    else:
        message = tr(
            interaction.channel.id if interaction.channel else 0,
            f"🛑 AI disabled in {channel.mention}. The channel is still saved and can be enabled again with `true`.",
            f"🛑 AI đã tắt ở {channel.mention}. Kênh vẫn được lưu và có thể bật lại bằng `true`."
        )

    await interaction.response.send_message(message)


@bot.tree.command(
    name="ai_interval",
    description="Set the reply interval for this server plan"
)
@app_commands.checks.has_permissions(manage_guild=True)
async def ai_interval(
    interaction: discord.Interaction,
    interval: int
):
    guild_id = interaction.guild.id
    limits = get_tier_limits(guild_id)
    if not (limits["min_interval"] <= int(interval) <= limits["max_interval"]):
        await interaction.response.send_message(
            tr(
                interaction.channel.id if interaction.channel else 0,
                f"❌ The {plan_label(get_plan(guild_id))} plan only allows interval `{limits['min_interval']}-{limits['max_interval']}`.",
                f"❌ Gói {plan_label(get_plan(guild_id))} chỉ cho interval `{limits['min_interval']}-{limits['max_interval']}`.",
            ),
            ephemeral=True,
        )
        return

    reply_intervals[guild_id] = int(interval)
    message_counts[guild_id] = 0
    save_settings(guild_id)

    await interaction.response.send_message(tr(interaction.channel.id if interaction.channel else 0, f"✅ Interval = **{interval}**\n🎲 Early reply = **10%**", f"✅ Interval = **{interval}**\n🎲 Early reply = **10%**"))


def build_status_embed(guild_id, channel_id):
    is_vi = get_language(channel_id) == LANG_VI
    enabled = ai_enabled.get(guild_id, False)
    channel_id_ai = ai_channels.get(guild_id)
    interval = reply_intervals.get(guild_id, DEFAULT_INTERVAL)
    memory = get_message_count(guild_id)
    genz_count = len(get_genz_terms(guild_id, 1000))
    gif_count = len(get_gif_keywords(guild_id))
    guild = bot.get_guild(guild_id)
    channel = guild.get_channel(channel_id_ai) if guild and channel_id_ai else None
    plan = get_plan(guild_id); lim = get_tier_limits(guild_id)
    embed = discord.Embed(
        title="🤖 TOILIX • STATUS",
        description=(f"**{guild.name if guild else 'Server'}**\nLive overview of AI, memory and bot systems."
                     if not is_vi else f"**{guild.name if guild else 'Server'}**\nTổng quan hiện tại về AI, memory và hệ thống bot."),
        color=discord.Color.from_rgb(43,45,49),
    )
    embed.add_field(name="🤖 AI", value=(
        f"**Status**  {'🟢 Enabled' if enabled else '🔴 Disabled'}\n**Channel**  {channel.mention if channel else 'Not set'}\n**Interval**  `{interval}`  •  **Early** `10%`"
        if not is_vi else
        f"**Trạng thái**  {'🟢 Bật' if enabled else '🔴 Tắt'}\n**Channel**  {channel.mention if channel else 'Chưa đặt'}\n**Interval**  `{interval}`  •  **Sớm** `10%`"), inline=False)
    embed.add_field(name="🧠 MEMORY & LEARNING" if not is_vi else "🧠 MEMORY & HỌC", value=(
        f"**Messages** `{memory}`  •  **Gen Z** `{genz_count}`  •  **GIFs** `{gif_count}`\n**Self-learning** `ON`  •  **Channel memory** `ON`"
        if not is_vi else
        f"**Tin nhắn** `{memory}`  •  **Gen Z** `{genz_count}`  •  **GIF** `{gif_count}`\n**Tự học** `BẬT`  •  **Memory theo channel** `BẬT`"), inline=False)
    embed.add_field(name="💎 PLAN", value=f"**{plan_label(plan)}**  •  AI `{plan_model(guild_id)}`\nMemory `{lim['memory_trigger']} → {lim['memory_keep']}`  •  Facts `{lim['max_facts']}`  •  Gen Z `{lim['max_genz']}`", inline=False)
    embed.add_field(name="⚡ BEHAVIOR", value=("Direct mention `ON`  •  Reply-to-bot `ON`  •  FIFO queue `PER CHANNEL`" if not is_vi else "Mention trực tiếp `BẬT`  •  Reply-to-bot `BẬT`  •  FIFO queue `THEO CHANNEL`"), inline=False)
    embed.set_footer(text="TOILIX • Use /ai_memory for this channel's memory." if not is_vi else "TOILIX • Dùng /ai_memory để xem memory của channel này.")
    return embed


def build_memory_embed(guild_id, channel_id):
    is_vi = get_language(channel_id) == LANG_VI
    summary = get_summary(guild_id, channel_id); facts = get_facts(guild_id, channel_id, 15); genz = get_genz_terms(guild_id, 15)
    enabled = ai_channel_states.get((guild_id, channel_id), ai_enabled.get(guild_id, False))
    summary_text = summary.strip() if summary else ("No summary yet." if not is_vi else "Chưa có summary.")
    if len(summary_text)>1000: summary_text=summary_text[:997]+"..."
    facts_text = "".join(f"• **{username}:** {fact}\n" for _,username,fact in facts).strip() or ("No facts yet." if not is_vi else "Chưa có facts.")
    if len(facts_text)>1000: facts_text=facts_text[:997]+"..."
    genz_text = "".join(f"• `{term}` → {meaning}\n" for term,meaning,_,_,_ in genz).strip() or ("No Gen Z terms yet." if not is_vi else "Chưa có từ Gen Z.")
    if len(genz_text)>1000: genz_text=genz_text[:997]+"..."
    embed=discord.Embed(
        title="🧠 TOILIX • MEMORY",
        description=(f"**Channel:** <#{channel_id}>  •  {'🟢 Learning' if enabled else '🔴 Paused'}\nThis panel only shows memory learned from this channel."
                     if not is_vi else f"**Channel:** <#{channel_id}>  •  {'🟢 Đang học' if enabled else '🔴 Tạm dừng'}\nBảng này chỉ hiển thị memory được học từ channel hiện tại."),
        color=discord.Color.from_rgb(43,45,49))
    embed.add_field(name="📖 SUMMARY",value=summary_text,inline=False)
    embed.add_field(name="📝 FACTS",value=facts_text,inline=False)
    embed.add_field(name="🗣️ GEN Z",value=genz_text,inline=False)
    embed.set_footer(text="Old memory is kept when AI learning is disabled." if not is_vi else "Memory cũ vẫn được giữ khi tắt AI learning.")
    return embed


@bot.tree.command(
    name="ai_memory",
    description="View AI memory"
)
async def ai_memory(interaction: discord.Interaction):
    guild_id = interaction.guild.id
    channel_id = interaction.channel.id if interaction.channel else 0
    await interaction.response.send_message(
        embed=build_memory_embed(guild_id, channel_id)
    )


@bot.tree.command(
    name="genz_add",
    description="Add slang or memes to the server dictionary"
)
@app_commands.checks.has_permissions(manage_guild=True)
async def genz_add(
    interaction: discord.Interaction,
    term: str,
    meaning: str,
    example: str = ""
):
    save_genz_term(
        interaction.guild.id,
        term,
        meaning,
        example
    )

    await interaction.response.send_message(
        tr(interaction.channel.id if interaction.channel else 0, f"🧠 Added `{term}` to the Gen Z dictionary.", f"🧠 Đã thêm `{term}` vào Gen Z dictionary.")
    )


@bot.tree.command(
    name="gif_add",
    description="Add a GIF URL to the server library"
)
@app_commands.checks.has_permissions(manage_guild=True)
async def gif_add(
    interaction: discord.Interaction,
    keyword: str,
    url: str
):
    if not (
        url.startswith("https://")
        or url.startswith("http://")
    ):
        await interaction.response.send_message(
            tr(interaction.channel.id if interaction.channel else 0, "❌ Invalid URL.", "❌ URL không hợp lệ."),
            ephemeral=True
        )
        return

    add_gif(
        interaction.guild.id,
        keyword,
        url
    )

    await interaction.response.send_message(
        tr(interaction.channel.id if interaction.channel else 0, f"🎬 Added a GIF for keyword `{keyword}`.", f"🎬 Đã thêm GIF cho keyword `{keyword}`.")
    )


@bot.tree.command(
    name="gif",
    description="Send a GIF from the server library"
)
async def gif(
    interaction: discord.Interaction,
    keyword: str
):
    urls = get_gifs(
        interaction.guild.id,
        keyword
    )

    if not urls:
        await interaction.response.send_message(
            tr(interaction.channel.id, f"❌ No GIF found for `{keyword}`.", f"❌ Chưa có GIF cho `{keyword}`.")
        )
        return

    await interaction.response.send_message(
        random.choice(urls)
    )


def build_help_embed(channel_id):
    """Build the main TOILIX help panel with grouped Owo-style sections."""
    is_vi = get_language(channel_id) == LANG_VI
    descriptions = {
        "help": ("Open this command panel.", "Mở bảng lệnh này."),
        "owner": ("Show the bot owner.", "Hiển thị chủ bot."),
        "language": ("Set the language for this channel.", "Đặt ngôn ngữ cho channel này."),
        "ai_on": ("Enable AI in the selected channel.", "Bật AI trong channel đã chọn."),
        "ai_off": ("Disable AI in the selected channel.", "Tắt AI trong channel đã chọn."),
        "ai_channel": ("Configure an AI channel and its true/false state.", "Cấu hình channel AI và trạng thái true/false."),
        "ai_interval": ("Set the AI reply interval.", "Đặt khoảng cách tin nhắn AI trả lời."),
        "status": ("View live bot, AI and learning status.", "Xem trạng thái bot, AI và hệ thống học."),
        "ai_memory": ("View memory for the current channel.", "Xem memory của channel hiện tại."),
        "genz_add": ("Add slang/memes to the dictionary.", "Thêm slang/meme vào từ điển."),
        "gif_add": ("Add a GIF URL by keyword.", "Thêm URL GIF theo từ khóa."),
        "gif": ("Send a saved GIF.", "Gửi GIF đã lưu."),
        "caption": ("Add a caption above an image.", "Thêm caption phía trên ảnh."),
        "avatar": ("Show a member avatar.", "Hiển thị avatar thành viên."),
        "support": ("Send a support request.", "Gửi yêu cầu hỗ trợ."),
        "premium": ("View the current server plan.", "Xem gói hiện tại của server."),
        "premium_add": ("Owner: grant Standard or Premium.", "Owner: cấp Standard hoặc Premium."),
        "premium_remove": ("Owner: return a server to Free.", "Owner: đưa server về Free."),
        "mute": ("Temporarily mute a member.", "Mute thành viên trong thời gian chỉ định."),
        "unmute": ("Remove a member's timeout.", "Gỡ mute/timeout của thành viên."),
        "ban": ("Temporarily ban a member.", "Ban thành viên trong thời gian chỉ định."),
        "unban": ("Unban a user by ID.", "Gỡ ban user bằng ID."),
    }
    names = {c.name for c in bot.tree.get_commands()}
    names.update(c.name for c in bot.commands)
    groups = [
        (("🤖 AI & CHAT", "🤖 AI & CHAT"), ["help", "language", "ai_on", "ai_off", "ai_channel", "ai_interval", "status", "ai_memory"]),
        (("🛡️ MODERATION", "🛡️ QUẢN TRỊ"), ["mute", "unmute", "ban", "unban"]),
        (("🎨 MEDIA", "🎨 MEDIA"), ["avatar", "caption", "gif", "gif_add"]),
        (("🧠 LEARNING", "🧠 HỌC & MEMORY"), ["genz_add"]),
        (("💎 PREMIUM & SUPPORT", "💎 PREMIUM & SUPPORT"), ["premium", "premium_add", "premium_remove", "support"]),
        (("⚙️ BOT", "⚙️ BOT"), ["owner"]),
    ]
    embed = discord.Embed(
        title="🤖 TOILIX",
        description=(
            "**Help Center**\nUse `/` or `!` • Commands are grouped by function.\n\n`/help` • `!help`  ·  Open this panel"
            if not is_vi else
            "**Trung tâm lệnh**\nDùng `/` hoặc `!` • Lệnh được chia theo từng chức năng.\n\n`/help` • `!help`  ·  Mở bảng này"
        ),
        color=discord.Color.from_rgb(43, 45, 49),
    )
    for (en_name, vi_name), command_names in groups:
        entries=[]
        for name in command_names:
            if name in names:
                en,vi=descriptions[name]
                entries.append(f"`/{name}`  `!{name}` — {vi if is_vi else en}")
        if entries: embed.add_field(name=vi_name if is_vi else en_name, value="\n".join(entries), inline=False)
    embed.add_field(
        name="⏱️ DURATION" if not is_vi else "⏱️ THỜI GIAN",
        value=("`10s` · `10m` · `10h` · `10d`\n`10d10h10m10s` or any order such as `10h2d5m`"
               if not is_vi else
               "`10s` · `10m` · `10h` · `10d`\n`10d10h10m10s` hoặc ghép theo thứ tự bất kỳ như `10h2d5m`"),
        inline=False,
    )
    embed.set_footer(text="TOILIX • AI may reply early with a 10% chance." if not is_vi else "TOILIX • AI có 10% xác suất trả lời sớm.")
    return embed


async def send_help_embed(target):
    embed = build_help_embed(target.channel.id)
    await target.send(embed=embed)


@bot.tree.command(
    name="help",
    description="View all available commands"
)
async def help_command(interaction: discord.Interaction):
    await interaction.response.send_message(
        embed=build_help_embed(interaction.channel.id if interaction.channel else 0)
    )


# =========================================================\n# PREFIX COMMANDS (!) + SUPPORT + PREMIUM
# =========================================================

async def send_long_text(ctx, text):
    for i in range(0, len(text), 1900):
        await ctx.send(text[i:i+1900])

async def send_support_request(user, guild, message_text, source, channel_id=None):
    if not SUPPORT_CHANNEL_ID:
        return False, tr(
            channel_id,
            "Support is not configured. Set SUPPORT_CHANNEL_ID in .env.",
            "Support chưa được cấu hình. Hãy đặt SUPPORT_CHANNEL_ID trong .env.",
        )

    channel = bot.get_channel(SUPPORT_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(SUPPORT_CHANNEL_ID)
        except discord.NotFound:
            return False, tr(
                channel_id,
                f"Support channel ID {SUPPORT_CHANNEL_ID} was not found.",
                f"Không tìm thấy support channel với ID {SUPPORT_CHANNEL_ID}.",
            )
        except discord.Forbidden:
            return False, tr(
                channel_id,
                "Discord denied access to the support channel. Check View Channel permission.",
                "Discord không cho bot truy cập support channel. Hãy kiểm tra quyền View Channel.",
            )
        except Exception as e:
            print("[SUPPORT FETCH ERROR]", repr(e))
            return False, tr(
                channel_id,
                "Could not access the support channel. Check the ID and bot permissions.",
                "Không thể truy cập support channel. Hãy kiểm tra ID và quyền của bot.",
            )

    if channel is None or not hasattr(channel, "send"):
        return False, tr(
            channel_id,
            f"ID {SUPPORT_CHANNEL_ID} is not a sendable channel.",
            f"ID {SUPPORT_CHANNEL_ID} không phải channel có thể gửi tin.",
        )

    is_vi = get_language(channel_id) == LANG_VI
    embed = discord.Embed(
        title="📩 Yêu cầu hỗ trợ" if is_vi else "📩 Support Request",
        description=(message_text or ("Không có nội dung." if is_vi else "No message."))[:4000],
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(
        name="Người dùng" if is_vi else "User",
        value=f"{user} (`{user.id}`)",
        inline=False,
    )
    embed.add_field(
        name="Server",
        value=f"{guild.name} (`{guild.id}`)" if guild else ("DM"),
        inline=False,
    )
    embed.add_field(name="Nguồn" if is_vi else "Source", value=source, inline=True)

    try:
        await channel.send(embed=embed)
        return True, tr(channel_id, "Sent.", "Đã gửi.")
    except discord.Forbidden:
        return False, tr(
            channel_id,
            "The bot cannot send messages to the support channel.",
            "Bot không có quyền gửi vào support channel.",
        )
    except Exception as e:
        print("[SUPPORT ERROR]", repr(e))
        return False, tr(
            channel_id,
            "Could not send the support request.",
            "Không thể gửi support request.",
        )


@bot.command(name="support")
async def support_prefix(ctx, *, message: str = "Người dùng yêu cầu hỗ trợ."):
    ok, result = await send_support_request(
        ctx.author, ctx.guild, message, "!support", ctx.channel.id
    )
    await ctx.send(tr(ctx.channel.id, "✅ Support request sent." if ok else f"❌ {result}", "✅ Đã gửi yêu cầu support." if ok else f"❌ {result}"))


@bot.tree.command(name="support", description="Send a support request to the support channel")
async def support_slash(
    interaction: discord.Interaction,
    message: str = "Người dùng yêu cầu hỗ trợ.",
):
    ok, result = await send_support_request(
        interaction.user, interaction.guild, message, "/support", interaction.channel.id if interaction.channel else 0
    )
    await interaction.response.send_message(
        tr(
            interaction.channel.id if interaction.channel else 0,
            "✅ Support request sent." if ok else f"❌ {result}",
            "✅ Đã gửi yêu cầu support." if ok else f"❌ {result}",
        ),
        ephemeral=True,
    )


def build_premium_embed(guild_id, channel_id):
    is_vi=get_language(channel_id)==LANG_VI; plan=get_plan(guild_id); info=get_plan_info(guild_id); lim=get_tier_limits(guild_id)
    labels=({PLAN_FREE:"🆓 Miễn phí",PLAN_STANDARD:"🔹 Standard",PLAN_PREMIUM:"💎 Premium"} if is_vi else {PLAN_FREE:"🆓 Free",PLAN_STANDARD:"🔹 Standard",PLAN_PREMIUM:"💎 Premium"})
    label=labels.get(plan,plan)
    embed=discord.Embed(title="💎 TOILIX • PREMIUM",description=(f"Current server plan: **{label}**\nYour plan controls AI model and feature limits." if not is_vi else f"Gói hiện tại của server: **{label}**\nGói quyết định model AI và giới hạn tính năng."),color=discord.Color.from_rgb(43,45,49))
    embed.add_field(name="🤖 AI",value=f"**Model** `{plan_model(guild_id)}`",inline=False)
    embed.add_field(name="🧠 MEMORY",value=f"Trigger `{lim['memory_trigger']}`  •  Keep `{lim['memory_keep']}`\nFacts `{lim['max_facts']}`  •  Gen Z `{lim['max_genz']}`",inline=False)
    embed.add_field(name="⚡ AI SETTINGS",value=(f"Interval `{reply_intervals.get(guild_id,DEFAULT_INTERVAL)}`  •  Early reply `10%`\n100-key Gemini fallback is available on Free." if not is_vi else f"Interval `{reply_intervals.get(guild_id,DEFAULT_INTERVAL)}`  •  Trả lời sớm `10%`\nFree có hệ thống Gemini fallback tối đa 100 key."),inline=False)
    embed.add_field(name="⏳ EXPIRES" if not is_vi else "⏳ HẾT HẠN",value=f"`{info['expires_at']}`" if info.get('expires_at') else ("`Permanent / No expiry`" if not is_vi else "`Không có thời hạn`"),inline=False)
    embed.set_footer(text="TOILIX • Owner-managed plan" if not is_vi else "TOILIX • Gói được quản lý bởi Owner")
    return embed


@bot.command(name="premium")
async def premium_prefix(ctx):
    if not ctx.guild:
        await ctx.send("This command can only be used in a server."); return
    await ctx.send(embed=build_premium_embed(ctx.guild.id,ctx.channel.id))


@bot.tree.command(name="premium", description="View the server plan and premium status")
async def premium_slash(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("This command can only be used in a server.",ephemeral=True); return
    await interaction.response.send_message(embed=build_premium_embed(interaction.guild.id,interaction.channel.id if interaction.channel else 0))


def valid_plan(plan):
    return plan.lower() in (PLAN_STANDARD, PLAN_PREMIUM)


@bot.command(name="premium_add")
@commands.is_owner()
async def premium_add_prefix(ctx, guild_id: int, plan: str):
    plan = plan.lower().strip()
    if not valid_plan(plan):
        await ctx.send(tr(ctx.channel.id, "❌ Plan must be `standard` or `premium`.", "❌ Plan phải là `standard` hoặc `premium`."))
        return

    set_plan(guild_id, plan, source="manual")
    await ctx.send(
        tr(ctx.channel.id, f"✅ Granted **{plan_label(plan)}** to server `{guild_id}`.", f"✅ Đã cấp **{plan_label(plan)}** cho server `{guild_id}`.")
    )


@bot.command(name="premium_remove")
@commands.is_owner()
async def premium_remove_prefix(ctx, guild_id: int):
    remove_plan(guild_id)
    await ctx.send(tr(ctx.channel.id, f"✅ Returned server `{guild_id}` to Free.", f"✅ Đã đưa server `{guild_id}` về Free."))


@bot.tree.command(
    name="premium_add",
    description="Owner bot cấp Standard/Premium cho server.",
)
@app_commands.describe(guild_id="ID server", plan="Gói muốn cấp")
@app_commands.choices(
    plan=[
        app_commands.Choice(name="Standard", value=PLAN_STANDARD),
        app_commands.Choice(name="Premium", value=PLAN_PREMIUM),
    ]
)
async def premium_add_slash(
    interaction: discord.Interaction,
    guild_id: str,
    plan: app_commands.Choice[str],
):
    if not await bot.is_owner(interaction.user):
        await interaction.response.send_message(
            tr(interaction.channel.id if interaction.channel else 0, "❌ Only the bot owner can use this.", "❌ Chỉ owner bot mới dùng được."), ephemeral=True
        )
        return

    try:
        gid = int(guild_id)
    except ValueError:
        await interaction.response.send_message(
            tr(interaction.channel.id if interaction.channel else 0, "❌ Guild ID must be a number.", "❌ Guild ID phải là số."), ephemeral=True
        )
        return

    set_plan(gid, plan.value, source="manual")
    await interaction.response.send_message(
        tr(interaction.channel.id if interaction.channel else 0, f"✅ Granted **{plan_label(plan.value)}** to server `{gid}`.", f"✅ Đã cấp **{plan_label(plan.value)}** cho server `{gid}`."),
        ephemeral=True,
    )


@bot.tree.command(
    name="premium_remove",
    description="Owner bot đưa server về Free.",
)
async def premium_remove_slash(
    interaction: discord.Interaction,
    guild_id: str,
):
    if not await bot.is_owner(interaction.user):
        await interaction.response.send_message(
            tr(interaction.channel.id if interaction.channel else 0, "❌ Only the bot owner can use this.", "❌ Chỉ owner bot mới dùng được."), ephemeral=True
        )
        return

    try:
        gid = int(guild_id)
    except ValueError:
        await interaction.response.send_message(
            tr(interaction.channel.id if interaction.channel else 0, "❌ Guild ID must be a number.", "❌ Guild ID phải là số."), ephemeral=True
        )
        return

    remove_plan(gid)
    await interaction.response.send_message(
        tr(interaction.channel.id if interaction.channel else 0, f"✅ Returned server `{gid}` to Free.", f"✅ Đã đưa server `{gid}` về Free."),
        ephemeral=True,
    )



# =========================================================
# MODERATION: MUTE + TEMPORARY BAN
# =========================================================

DURATION_RE = re.compile(r"(?P<value>\d+)\s*(?P<unit>[smhd])", re.IGNORECASE)
MAX_TIMEOUT_SECONDS = 28 * 24 * 60 * 60


def parse_duration(value: str):
    """Parse any combination/order of d, h, m and s."""
    if not value:
        return None
    compact = re.sub(r"\s+", "", value.strip().lower())
    matches = list(DURATION_RE.finditer(compact))
    if not matches or "".join(m.group(0) for m in matches) != compact:
        return None
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    total = sum(int(m.group("value")) * multipliers[m.group("unit").lower()] for m in matches)
    return total if total > 0 else None


def format_duration(seconds: int):
    seconds = int(seconds)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    parts = []
    if days: parts.append(f"{days}d")
    if hours: parts.append(f"{hours}h")
    if minutes: parts.append(f"{minutes}m")
    if seconds or not parts: parts.append(f"{seconds}s")
    return "".join(parts)


def save_temporary_ban(guild_id: int, user_id: int, expires_at: float, reason: str = ""):
    conn = get_db()
    conn.execute("""
        INSERT INTO temporary_bans (guild_id, user_id, expires_at, reason)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(guild_id, user_id) DO UPDATE SET
            expires_at = excluded.expires_at,
            reason = excluded.reason
    """, (guild_id, user_id, expires_at, reason))
    conn.commit()
    conn.close()


def get_temporary_ban(guild_id: int, user_id: int):
    conn = get_db()
    row = conn.execute(
        "SELECT expires_at, reason FROM temporary_bans WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id)
    ).fetchone()
    conn.close()
    return row


def remove_temporary_ban(guild_id: int, user_id: int):
    conn = get_db()
    conn.execute(
        "DELETE FROM temporary_bans WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id)
    )
    conn.commit()
    conn.close()


async def temporary_unban(guild_id: int, user_id: int, expires_at: float):
    delay = max(0, expires_at - __import__("time").time())
    await asyncio.sleep(delay)

    current = get_temporary_ban(guild_id, user_id)
    if not current:
        return

    # A newer / extended ban replaced this timer.
    if abs(float(current[0]) - float(expires_at)) > 0.5:
        return

    guild = bot.get_guild(guild_id)
    if guild is None:
        remove_temporary_ban(guild_id, user_id)
        return

    try:
        await guild.unban(discord.Object(id=user_id), reason="Temporary ban expired")
    except discord.NotFound:
        pass
    except discord.Forbidden:
        print(f"[TEMP BAN] Cannot unban {user_id} in guild {guild_id}")
        return
    except Exception as e:
        print(f"[TEMP BAN] Unban error: {e}")
        return

    remove_temporary_ban(guild_id, user_id)
    temporary_ban_tasks.pop((guild_id, user_id), None)


def schedule_temporary_unban(guild_id: int, user_id: int, expires_at: float):
    key = (guild_id, user_id)
    old_task = temporary_ban_tasks.get(key)
    if old_task and not old_task.done():
        old_task.cancel()

    task = asyncio.create_task(temporary_unban(guild_id, user_id, expires_at))
    temporary_ban_tasks[key] = task


async def restore_temporary_bans():
    conn = get_db()
    rows = conn.execute(
        "SELECT guild_id, user_id, expires_at FROM temporary_bans"
    ).fetchall()
    conn.close()

    now = __import__("time").time()
    for guild_id, user_id, expires_at in rows:
        if float(expires_at) <= now:
            guild = bot.get_guild(guild_id)
            if guild:
                try:
                    await guild.unban(
                        discord.Object(id=user_id),
                        reason="Temporary ban expired while bot was offline"
                    )
                except (discord.NotFound, discord.Forbidden):
                    pass
            remove_temporary_ban(guild_id, user_id)
        else:
            schedule_temporary_unban(guild_id, user_id, float(expires_at))


async def _mute_member(member: discord.Member, duration_text: str, reason: str, channel_id: int):
    seconds = parse_duration(duration_text)
    if seconds is None:
        return False, tr(
            channel_id,
            "❌ Invalid duration. Use `s`, `m`, `h`, `d`, for example `10d10h10m10s` or any order such as `10h2d5m`.",
            "❌ Thời gian không hợp lệ. Dùng `s`, `m`, `h`, `d`, ví dụ `10d10h10m10s` hoặc thứ tự bất kỳ như `10h2d5m`."
        )

    now = datetime.now(timezone.utc)
    existing = member.timed_out_until
    base = existing if existing and existing > now else now
    until = base + timedelta(seconds=seconds)

    # Discord timeout cannot be longer than 28 days from now.
    if until > now + timedelta(seconds=MAX_TIMEOUT_SECONDS):
        return False, tr(
            channel_id,
            "❌ The resulting timeout cannot exceed Discord's 28-day limit.",
            "❌ Thời gian mute cuối cùng không được vượt quá giới hạn 28 ngày của Discord."
        )

    try:
        await member.timeout(until, reason=reason or "Muted by moderator")
    except discord.Forbidden:
        return False, tr(
            channel_id,
            "❌ I cannot mute this member. Check my Moderate Members permission and role position.",
            "❌ Bot không thể mute thành viên này. Hãy kiểm tra quyền Moderate Members và vị trí role của bot."
        )
    except Exception as e:
        print("[MUTE ERROR]", repr(e))
        return False, tr(channel_id, "❌ Could not mute this member.", "❌ Không thể mute thành viên này.")

    return True, tr(
        channel_id,
        f"🔇 {member.mention} muted for `{format_duration(seconds)}`." + (f" Because {reason}" if reason.strip() else ""),
        f"🔇 Đã mute {member.mention} trong `{format_duration(seconds)}`." + (f" Vì {reason}" if reason.strip() else "")
    )


async def _ban_member(guild: discord.Guild, user_id: int, reason: str, channel_id: int):
    """Permanent Discord ban. The user stays banned until /unban or !unban."""
    reason = reason.strip()
    if not reason:
        return False, tr(
            channel_id,
            "❌ Please provide a reason for the ban.",
            "❌ Vui lòng nhập lý do ban."
        )

    try:
        await guild.ban(
            discord.Object(id=user_id),
            reason=reason,
            delete_message_seconds=0
        )
    except discord.Forbidden:
        return False, tr(
            channel_id,
            "❌ I cannot ban this user. Check my Ban Members permission and role position.",
            "❌ Bot không thể ban người này. Hãy kiểm tra quyền Ban Members và vị trí role của bot."
        )
    except discord.HTTPException as e:
        print("[BAN ERROR]", repr(e))
        return False, tr(
            channel_id,
            "❌ Could not ban this user.",
            "❌ Không thể ban người này."
        )

    return True, tr(
        channel_id,
        f"🔨 <@{user_id}> has been banned." + f" Because {reason}",
        f"🔨 Đã ban <@{user_id}>." + f" Vì {reason}"
    )


def parse_user_id(value: str):
    """Accept a raw Discord ID or a user mention."""
    value = value.strip()
    match = re.fullmatch(r"<?@!?(\d{15,25})>?", value)
    if match:
        return int(match.group(1))
    if value.isdigit() and 15 <= len(value) <= 25:
        return int(value)
    return None


@bot.tree.command(name="mute", description="Temporarily mute a member")
@app_commands.describe(
    member="User mention or Discord ID",
    duration="Duration: 10s, 10m, 10h, 10d, or 10d10h10m10s",
    reason="Optional reason"
)
@app_commands.checks.has_permissions(moderate_members=True)
async def mute_slash(
    interaction: discord.Interaction,
    member: str,
    duration: str,
    reason: str = ""
):
    channel_id = interaction.channel.id if interaction.channel else 0
    user_id = parse_user_id(member)
    if user_id is None:
        await interaction.response.send_message(
            tr(channel_id, "❌ Invalid user ID or mention.", "❌ ID người dùng hoặc mention không hợp lệ."),
            ephemeral=True
        )
        return

    try:
        target = interaction.guild.get_member(user_id) or await interaction.guild.fetch_member(user_id)
    except discord.NotFound:
        await interaction.response.send_message(
            tr(channel_id, "❌ That user is not a member of this server.", "❌ Người dùng đó không phải thành viên của server."),
            ephemeral=True
        )
        return
    except discord.HTTPException:
        await interaction.response.send_message(
            tr(channel_id, "❌ Could not find that member.", "❌ Không thể tìm thấy thành viên đó."),
            ephemeral=True
        )
        return

    ok, message = await _mute_member(target, duration, reason, channel_id)
    await interaction.response.send_message(message, ephemeral=not ok)


@bot.tree.command(name="ban", description="Permanently ban a user")
@app_commands.describe(
    user="User mention or Discord ID",
    reason="Reason for the ban"
)
@app_commands.checks.has_permissions(ban_members=True)
async def ban_slash(
    interaction: discord.Interaction,
    user: str,
    reason: str
):
    user_id = parse_user_id(user)
    channel_id = interaction.channel.id if interaction.channel else 0
    if user_id is None:
        await interaction.response.send_message(
            tr(channel_id, "❌ Invalid user ID or mention.", "❌ ID người dùng hoặc mention không hợp lệ."),
            ephemeral=True
        )
        return

    ok, message = await _ban_member(interaction.guild, user_id, reason, channel_id)
    await interaction.response.send_message(message, ephemeral=not ok)


@bot.command(name="mute")
@commands.has_guild_permissions(moderate_members=True)
async def mute_prefix(ctx, member: str, duration: str, *, reason: str = ""):
    user_id = parse_user_id(member)
    if user_id is None:
        await ctx.send(tr(ctx.channel.id, "❌ Invalid user ID or mention.", "❌ ID người dùng hoặc mention không hợp lệ."))
        return

    target = ctx.guild.get_member(user_id)
    if target is None:
        try:
            target = await ctx.guild.fetch_member(user_id)
        except discord.NotFound:
            await ctx.send(tr(ctx.channel.id, "❌ That user is not a member of this server.", "❌ Người dùng đó không phải thành viên của server."))
            return
        except discord.HTTPException:
            await ctx.send(tr(ctx.channel.id, "❌ Could not find that member.", "❌ Không thể tìm thấy thành viên đó."))
            return

    ok, message = await _mute_member(target, duration, reason, ctx.channel.id)
    await ctx.send(message)


@bot.command(name="ban")
@commands.has_guild_permissions(ban_members=True)
async def ban_prefix(ctx, user: str, *, reason: str = ""):
    user_id = parse_user_id(user)
    if user_id is None:
        await ctx.send(tr(ctx.channel.id, "❌ Invalid user ID or mention.", "❌ ID người dùng hoặc mention không hợp lệ."))
        return

    ok, message = await _ban_member(ctx.guild, user_id, reason, ctx.channel.id)
    await ctx.send(message)


async def _unmute_member(member: discord.Member, channel_id: int):
    try:
        await member.timeout(None, reason="Unmuted by moderator")
        return True, tr(channel_id, f"🔊 {member.mention} is no longer muted.", f"🔊 Đã gỡ mute cho {member.mention}.")
    except discord.Forbidden:
        return False, tr(channel_id, "❌ I cannot unmute this member. Check my Moderate Members permission and role position.", "❌ Bot không thể gỡ mute. Hãy kiểm tra quyền Moderate Members và vị trí role của bot.")
    except Exception as e:
        print("[UNMUTE ERROR]",repr(e)); return False, tr(channel_id,"❌ Could not unmute this member.","❌ Không thể gỡ mute thành viên này.")


async def _unban_member(guild: discord.Guild, user_id: int, channel_id: int):
    try:
        await guild.unban(discord.Object(id=user_id), reason="Unbanned by moderator")
    except discord.NotFound:
        remove_temporary_ban(guild.id,user_id); task=temporary_ban_tasks.pop((guild.id,user_id),None)
        if task and not task.done(): task.cancel()
        return False,tr(channel_id,"❌ This user is not banned.","❌ User này hiện không bị ban.")
    except discord.Forbidden:
        return False,tr(channel_id,"❌ I cannot unban this user. Check my Ban Members permission.","❌ Bot không thể unban user này. Hãy kiểm tra quyền Ban Members.")
    except Exception as e:
        print("[UNBAN ERROR]",repr(e)); return False,tr(channel_id,"❌ Could not unban this user.","❌ Không thể unban user này.")
    remove_temporary_ban(guild.id,user_id); task=temporary_ban_tasks.pop((guild.id,user_id),None)
    if task and not task.done(): task.cancel()
    return True,tr(channel_id,f"🔓 <@{user_id}> has been unbanned.",f"🔓 Đã unban <@{user_id}>.")


@bot.tree.command(name="unmute",description="Remove a member's timeout")
@app_commands.describe(member="Member to unmute")
@app_commands.checks.has_permissions(moderate_members=True)
async def unmute_slash(interaction: discord.Interaction, member: discord.Member):
    ok,message=await _unmute_member(member,interaction.channel.id if interaction.channel else 0)
    await interaction.response.send_message(message,ephemeral=not ok)


@bot.tree.command(name="unban",description="Unban a user by ID")
@app_commands.describe(user_id="The Discord user ID to unban")
@app_commands.checks.has_permissions(ban_members=True)
async def unban_slash(interaction: discord.Interaction,user_id:str):
    try: uid=int(user_id.strip())
    except (ValueError,AttributeError):
        await interaction.response.send_message(tr(interaction.channel.id if interaction.channel else 0,"❌ User ID must be a number.","❌ User ID phải là số."),ephemeral=True); return
    ok,message=await _unban_member(interaction.guild,uid,interaction.channel.id if interaction.channel else 0)
    await interaction.response.send_message(message,ephemeral=not ok)


@bot.command(name="unmute")
@commands.has_guild_permissions(moderate_members=True)
async def unmute_prefix(ctx,member:discord.Member):
    ok,message=await _unmute_member(member,ctx.channel.id); await ctx.send(message)


@bot.command(name="unban")
@commands.has_guild_permissions(ban_members=True)
async def unban_prefix(ctx,user_id:str):
    try: uid=int(user_id.strip())
    except (ValueError,AttributeError):
        await ctx.send(tr(ctx.channel.id,"❌ User ID must be a number.","❌ User ID phải là số.")); return
    ok,message=await _unban_member(ctx.guild,uid,ctx.channel.id); await ctx.send(message)


# =========================================================
# IMAGE COMMANDS: AVATAR + CAPTION
# =========================================================

def _find_font(size: int):
    candidates = [
        # Windows
        r"C:\Windows\Fonts\arial.ttf",
        r"C:\Windows\Fonts\segoeui.ttf",
        r"C:\Windows\Fonts\tahoma.ttf",
        # Linux / common Python images
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
        "/usr/share/fonts/opentype/noto/NotoSans-Regular.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size=size)
            except Exception:
                pass
    return ImageFont.load_default()


async def _get_image_bytes_from_message(message: discord.Message):
    """Lấy ảnh từ attachment của message hoặc message được reply."""
    attachments = list(message.attachments)

    if not attachments and message.reference and message.reference.message_id:
        try:
            replied = message.reference.resolved
            if not isinstance(replied, discord.Message):
                replied = await message.channel.fetch_message(message.reference.message_id)
            attachments = list(replied.attachments)
        except Exception:
            attachments = []

    for attachment in attachments:
        content_type = (attachment.content_type or "").lower()
        filename = attachment.filename.lower()
        if content_type.startswith("image/") or filename.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
            try:
                return await attachment.read(), attachment.filename
            except Exception:
                return None, None

    return None, None


async def _make_caption_image(image_bytes: bytes, text: str):
    """Create an image with a white caption area above the original image."""
    def build():
        with Image.open(BytesIO(image_bytes)) as original:
            # GIF -> first frame for stable PNG output.
            if getattr(original, "is_animated", False):
                original.seek(0)

            img = original.convert("RGB")
            width, height = img.size
            clean_text = " ".join(str(text).split())

            # Pick a font size that can wrap the caption without horizontal overflow.
            font_size = max(18, min(64, int(max(90, height * 0.18) * 0.48)))
            max_text_width = max(80, width - 40)

            while True:
                font = _find_font(font_size)
                words = clean_text.split()
                lines = []
                current = ""

                for word in words:
                    candidate = word if not current else f"{current} {word}"
                    bbox = ImageDraw.Draw(Image.new("RGB", (1, 1))).textbbox(
                        (0, 0), candidate, font=font
                    )
                    if bbox[2] - bbox[0] <= max_text_width:
                        current = candidate
                    else:
                        if current:
                            lines.append(current)
                        # Handle a single word longer than the available width.
                        if ImageDraw.Draw(Image.new("RGB", (1, 1))).textbbox(
                            (0, 0), word, font=font
                        )[2] - ImageDraw.Draw(Image.new("RGB", (1, 1))).textbbox(
                            (0, 0), word, font=font
                        )[0] > max_text_width:
                            piece = ""
                            for char in word:
                                candidate_piece = piece + char
                                bbox_piece = ImageDraw.Draw(Image.new("RGB", (1, 1))).textbbox(
                                    (0, 0), candidate_piece, font=font
                                )
                                if bbox_piece[2] - bbox_piece[0] <= max_text_width:
                                    piece = candidate_piece
                                else:
                                    if piece:
                                        lines.append(piece)
                                    piece = char
                            current = piece
                        else:
                            current = word

                if current:
                    lines.append(current)

                # At smaller font sizes, more lines are acceptable.
                line_bbox = font.getbbox("Ag")
                line_h = max(1, line_bbox[3] - line_bbox[1])
                required_h = len(lines) * (line_h + 8) + 32

                if font_size <= 18 or required_h <= 400 or len(lines) <= 8:
                    break
                font_size -= 2

            band_h = max(90, min(400, required_h))
            canvas = Image.new("RGB", (width, height + band_h), "white")
            canvas.paste(img, (0, band_h))

            draw = ImageDraw.Draw(canvas)
            total_text_h = len(lines) * (line_h + 8) - 8
            y = max(12, (band_h - total_text_h) // 2)

            for line in lines:
                bbox = draw.textbbox((0, 0), line, font=font)
                text_w = bbox[2] - bbox[0]
                x = max(20, (width - text_w) // 2)
                draw.text((x, y - bbox[1]), line, fill="black", font=font)
                y += line_h + 8

            out = BytesIO()
            canvas.save(out, format="PNG", optimize=True)
            out.seek(0)
            return out.read()

    return await asyncio.to_thread(build)


async def _caption_from_prefix(ctx, text: str):
    if not text.strip():
        await ctx.send(tr(ctx.channel.id, "❌ Use `!caption <text>` with an image or reply to an image.", "❌ Dùng `!caption <chữ>` rồi đính kèm ảnh hoặc reply vào ảnh."))
        return

    image_bytes, filename = await _get_image_bytes_from_message(ctx.message)
    if not image_bytes:
        await ctx.send(tr(ctx.channel.id, "❌ Attach an image to the command or reply to a message with an image.", "❌ Hãy đính kèm ảnh với lệnh hoặc reply vào một tin nhắn có ảnh."))
        return

    try:
        result = await _make_caption_image(image_bytes, text)
        await ctx.send(file=discord.File(BytesIO(result), filename="caption.png"))
    except Exception as e:
        print("[CAPTION ERROR]", repr(e))
        await ctx.send(tr(ctx.channel.id, "❌ Could not create the caption image.", "❌ Không thể tạo ảnh caption."))


@bot.command(name="caption")
async def caption_prefix(ctx, *, text: str = ""):
    await _caption_from_prefix(ctx, text)


@bot.tree.command(name="caption", description="Add a white caption area above an image")
@app_commands.describe(text="Dòng chữ cần ghi", image="Ảnh cần thêm caption")
async def caption_slash(
    interaction: discord.Interaction,
    text: str,
    image: discord.Attachment | None = None,
):
    if not text.strip():
        await interaction.response.send_message(tr(interaction.channel.id if interaction.channel else 0, "❌ Caption cannot be empty.", "❌ Caption không được để trống."), ephemeral=True)
        return

    image_bytes = None
    if image is not None:
        try:
            image_bytes = await image.read()
        except Exception:
            image_bytes = None

    if image_bytes is None:
        await interaction.response.send_message(
            tr(
                interaction.channel.id if interaction.channel else 0,
                "❌ Select an image in the `image` field. With prefix, you can reply to an image.",
                "❌ Hãy chọn ảnh ở ô `image`. Với prefix, có thể reply vào ảnh.",
            ),
            ephemeral=True,
        )
        return

    await interaction.response.defer()
    try:
        result = await _make_caption_image(image_bytes, text)
        await interaction.followup.send(file=discord.File(BytesIO(result), filename="caption.png"))
    except Exception as e:
        print("[CAPTION ERROR]", repr(e))
        await interaction.followup.send(tr(interaction.channel.id if interaction.channel else 0, "❌ Could not create the caption image.", "❌ Không thể tạo ảnh caption."))


async def _send_avatar(ctx, target=None):
    """Hiển thị avatar của người dùng lệnh; nếu có @mention thì hiển thị avatar người được mention."""
    user = target or ctx.author
    try:
        avatar_url = user.display_avatar.url
        embed = discord.Embed(
            title=tr(ctx.channel.id, f"🖼️ Avatar of {user.display_name}", f"🖼️ Avatar của {user.display_name}"),
            color=discord.Color.blurple(),
        )
        embed.set_image(url=avatar_url)
        embed.set_footer(text=f"@{user.name}")
        await ctx.send(embed=embed)
    except Exception as e:
        print("[AVATAR ERROR]", repr(e))
        await ctx.send(tr(ctx.channel.id, "❌ Could not get this user's avatar.", "❌ Không thể lấy avatar của người dùng này."))


@bot.command(name="avatar")
async def avatar_prefix(ctx, target: discord.User = None):
    await _send_avatar(ctx, target)


@bot.tree.command(name="avatar", description="Show your avatar or a selected user avatar")
@app_commands.describe(user="Người có avatar muốn xem (bỏ trống để xem avatar của bạn)")
async def avatar_slash(interaction: discord.Interaction, user: discord.User | None = None):
    target = user or interaction.user
    try:
        avatar_url = target.display_avatar.url
        embed = discord.Embed(
            title=tr(interaction.channel.id if interaction.channel else 0, f"🖼️ Avatar of {target.display_name}", f"🖼️ Avatar của {target.display_name}"),
            color=discord.Color.blurple(),
        )
        embed.set_image(url=avatar_url)
        embed.set_footer(text=f"@{target.name}")
        await interaction.response.send_message(embed=embed)
    except Exception as e:
        print("[AVATAR ERROR]", repr(e))
        await interaction.response.send_message(tr(interaction.channel.id if interaction.channel else 0, "❌ Could not get this user's avatar.", "❌ Không thể lấy avatar của người dùng này."), ephemeral=True)


# =========================================================
# PREFIX (!) MIRRORS FOR ALL OLD SLASH COMMANDS
# =========================================================

@bot.command(name="ai_on")
@commands.has_guild_permissions(manage_guild=True)
async def ai_on_prefix(ctx):
    guild_id = ctx.guild.id
    ai_enabled[guild_id] = True
    reply_intervals.setdefault(guild_id, DEFAULT_INTERVAL)
    message_counts[guild_id] = 0
    save_settings(guild_id)
    seed_default_genz()
    await ctx.send(tr(ctx.channel.id, "🤖 AI enabled.", "🤖 AI đã bật."))


@bot.command(name="ai_off")
@commands.has_guild_permissions(manage_guild=True)
async def ai_off_prefix(ctx):
    guild_id = ctx.guild.id
    ai_enabled[guild_id] = False
    save_settings(guild_id)
    await ctx.send(tr(ctx.channel.id, "🛑 AI disabled.", "🛑 AI đã tắt."))


@bot.command(name="ai_channel")
@commands.has_guild_permissions(manage_guild=True)
async def ai_channel_prefix(ctx, channel: discord.TextChannel, enabled: bool):
    guild_id = ctx.guild.id

    ai_channels[guild_id] = channel.id  # legacy/status display only
    save_ai_channel_state(guild_id, channel.id, enabled)
    if enabled:
        ai_enabled[guild_id] = True
        reply_intervals.setdefault(guild_id, DEFAULT_INTERVAL)
        message_counts[(guild_id, channel.id)] = 0

    save_settings(guild_id)

    if enabled:
        await ctx.send(tr(
            ctx.channel.id,
            f"✅ AI enabled in {channel.mention}.",
            f"✅ AI đã bật ở {channel.mention}."
        ))
    else:
        await ctx.send(tr(
            ctx.channel.id,
            f"🛑 AI disabled in {channel.mention}. The channel is still saved; use `!ai_channel {channel.mention} true` to enable it again.",
            f"🛑 AI đã tắt ở {channel.mention}. Kênh vẫn được lưu; dùng `!ai_channel {channel.mention} true` để bật lại."
        ))


@bot.command(name="ai_interval")
@commands.has_guild_permissions(manage_guild=True)
async def ai_interval_prefix(ctx, interval: int):
    guild_id = ctx.guild.id
    limits = get_tier_limits(guild_id)
    if not (limits["min_interval"] <= interval <= limits["max_interval"]):
        await ctx.send(
            tr(
                ctx.channel.id,
                f"❌ The {plan_label(get_plan(guild_id))} plan only allows interval `{limits['min_interval']}-{limits['max_interval']}`.",
                f"❌ Gói {plan_label(get_plan(guild_id))} chỉ cho interval `{limits['min_interval']}-{limits['max_interval']}`.",
            )
        )
        return
    reply_intervals[guild_id] = interval
    message_counts[guild_id] = 0
    save_settings(guild_id)
    await ctx.send(tr(ctx.channel.id, f"✅ Interval = **{interval}**\n🎲 Early reply = **10%**", f"✅ Interval = **{interval}**\n🎲 Early reply = **10%**"))


@bot.command(name="status")
async def status_prefix(ctx):
    await ctx.send(embed=build_status_embed(ctx.guild.id, ctx.channel.id))


@bot.command(name="ai_memory")
async def ai_memory_prefix(ctx):
    await ctx.send(embed=build_memory_embed(ctx.guild.id, ctx.channel.id))


@bot.command(name="genz_add")
@commands.has_guild_permissions(manage_guild=True)
async def genz_add_prefix(ctx, term: str, meaning: str, *, example: str = ""):
    save_genz_term(ctx.guild.id, term, meaning, example)
    await ctx.send(tr(ctx.channel.id, f"🧠 Added `{term}` to the Gen Z dictionary.", f"🧠 Đã thêm `{term}` vào Gen Z dictionary."))


@bot.command(name="gif_add")
@commands.has_guild_permissions(manage_guild=True)
async def gif_add_prefix(ctx, keyword: str, url: str):
    if not (url.startswith("https://") or url.startswith("http://")):
        await ctx.send(tr(ctx.channel.id, "❌ Invalid URL.", "❌ URL không hợp lệ."))
        return
    add_gif(ctx.guild.id, keyword, url)
    await ctx.send(tr(ctx.channel.id, f"🎬 Added a GIF for keyword `{keyword}`.", f"🎬 Đã thêm GIF cho keyword `{keyword}`."))


@bot.command(name="gif")
async def gif_prefix(ctx, *, keyword: str = ""):
    urls = get_gifs(ctx.guild.id, keyword)
    if not urls:
        await ctx.send(tr(ctx.channel.id, "❌ No GIF found for this keyword.", "❌ Không tìm thấy GIF cho keyword này."))
        return
    await ctx.send(random.choice(urls))


@bot.command(name="help")
async def help_prefix(ctx):
    await ctx.send(embed=build_help_embed(ctx.channel.id))


@bot.event
async def on_command_error(ctx, error):
    # Ignore command-not-found so normal chat is unaffected.
    if isinstance(error, commands.CommandNotFound):
        return

    if isinstance(error, commands.MissingPermissions):
        await ctx.send(
            tr(
                ctx.channel.id,
                "❌ You need the Manage Server permission.",
                "❌ Bạn cần quyền Quản lý Server.",
            )
        )
        return

    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(
            tr(
                ctx.channel.id,
                f"❌ Missing argument: `{error.param.name}`.",
                f"❌ Thiếu tham số: `{error.param.name}`.",
            )
        )
        return

    if isinstance(error, commands.BadArgument):
        await ctx.send(
            tr(
                ctx.channel.id,
                "❌ One or more arguments are invalid.",
                "❌ Một hoặc nhiều tham số không hợp lệ.",
            )
        )
        return

    print("[PREFIX COMMAND ERROR]", repr(error))
    await ctx.send(
        tr(
            ctx.channel.id,
            "❌ The command could not be completed.",
            "❌ Không thể thực hiện lệnh.",
        )
    )


# =========================================================
# MESSAGE EVENT
# =========================================================

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # Học GIF từ những gì người thật gửi trong server.
    try:
        await remember_server_gif(message)
    except Exception as e:
        print(f"[GIF MEMORY] {e}")

    if message.guild is None:
        await bot.process_commands(message)
        return

    if message.content.startswith("!"):
        await bot.process_commands(message)
        return

    guild_id = message.guild.id
    channel_id = message.channel.id

    if not ai_enabled.get(guild_id, False):
        await bot.process_commands(message)
        return

    channel_state = ai_channel_states.get((guild_id, channel_id))
    if channel_state is None:
        # Backward compatibility for any legacy channel not yet migrated.
        channel_state = ai_channels.get(guild_id) == channel_id
    if not channel_state:
        await bot.process_commands(message)
        return

    save_message(message)

    if get_message_count(guild_id, channel_id) >= MEMORY_TRIGGER:
        try:
            await compact_memory(guild_id, channel_id)
        except Exception as e:
            print("[MEMORY ERROR]", e)

    mentioned = bool(
        bot.user and bot.user in message.mentions
    )

    replied_to_bot = await is_reply_to_bot(message)

    direct = mentioned or replied_to_bot

    counter_key = (guild_id, channel_id)
    if direct:
        message_counts[counter_key] = 0
        should_reply = True
    else:
        count = message_counts.get(counter_key, 0) + 1
        message_counts[counter_key] = count

        interval = reply_intervals.get(
            guild_id,
            DEFAULT_INTERVAL
        )

        should_reply = (
            count >= interval
            or random.random() < EARLY_REPLY_CHANCE
        )

        if should_reply:
            message_counts[counter_key] = 0

    if not should_reply:
        await bot.process_commands(message)
        return

    try:
        prompt = build_ai_prompt(message)

        async with message.channel.typing():
            answer = await ask_ai_async(guild_id, prompt)

        if not answer:
            if get_plan(guild_id) == PLAN_FREE:
                error_en = "⚠️ Gemini did not return a response. Check GEMINI_API_KEY, the Gemini model, or the API quota."
                error_vi = "⚠️ Gemini không trả về phản hồi. Hãy kiểm tra GEMINI_API_KEY, model Gemini hoặc giới hạn API."
            else:
                error_en = "⚠️ Premium AI did not return a response. Check OPENAI_API_KEY, the OpenAI model, or the API quota."
                error_vi = "⚠️ Premium AI không trả về phản hồi. Hãy kiểm tra OPENAI_API_KEY, model OpenAI hoặc giới hạn API."

            await message.channel.send(
                tr(message.channel.id, error_en, error_vi)
            )
        else:
            await send_ai_response(
                message,
                answer
            )

            task = asyncio.create_task(
                learn_from_message(message)
            )

            learning_tasks.setdefault(
                guild_id,
                set()
            )

            learning_tasks[guild_id].add(task)

            task.add_done_callback(
                lambda t: learning_tasks[guild_id].discard(t)
            )

    except Exception as e:
        print("[AI ERROR]", e)

        try:
            await message.channel.send(
                tr(
                    message.channel.id,
                    "⚠️ An error occurred while processing AI.",
                    "⚠️ Có lỗi khi xử lý AI.",
                )
            )
        except Exception:
            pass

    await bot.process_commands(message)


# =========================================================
# COMMAND ERROR HANDLERS
# =========================================================

async def permission_error(interaction, error):
    if isinstance(error, app_commands.errors.MissingPermissions):
        message = tr(
            interaction.channel.id if interaction.channel else 0,
            "❌ You need the Manage Server permission.",
            "❌ Bạn cần quyền Quản lý Server.",
        )
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


async def generic_app_command_error(interaction, error):
    print("[COMMAND ERROR]", repr(error))
    message = tr(
        interaction.channel.id if interaction.channel else 0,
        "❌ The command could not be completed. Check the arguments and bot permissions.",
        "❌ Không thể thực hiện lệnh. Hãy kiểm tra tham số và quyền của bot.",
    )
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


for _command in (
    status,
    ai_memory,
    language_command,
    gif,
    help_command,
    support_slash,
    premium_slash,
    premium_add_slash,
    premium_remove_slash,
    caption_slash,
    avatar_slash,
    mute_slash,
    ban_slash,
    unmute_slash,
    unban_slash,
):
    _command.error(generic_app_command_error)


owner_slash.error(permission_error)
ai_on.error(permission_error)
ai_off.error(permission_error)
ai_channel.error(permission_error)
ai_interval.error(permission_error)
genz_add.error(permission_error)
gif_add.error(permission_error)
mute_slash.error(permission_error)
ban_slash.error(permission_error)


# =========================================================
# START
# =========================================================

if not TOKEN:
    print("❌ DISCORD_TOKEN is missing from .env")
else:
    init_db()
    bot.run(TOKEN)
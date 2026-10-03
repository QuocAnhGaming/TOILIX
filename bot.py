import os
import random
import sqlite3
import asyncio
import re
import requests
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

# FREE: giữ AI cũ của bot (Ollama).
MODEL = os.getenv("OLLAMA_MODEL", "llama3.2:3b")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434/api/generate")

# STANDARD/PREMIUM: AI mới qua OpenAI. Đổi model bằng .env.
STANDARD_MODEL = os.getenv("STANDARD_MODEL", "gpt-6-sol")
PREMIUM_MODEL = os.getenv("PREMIUM_MODEL", "gpt-6-astra")

MIN_INTERVAL = 5
MAX_INTERVAL = 15
DEFAULT_INTERVAL = 6
EARLY_REPLY_CHANCE = 0.25

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
ai_channels = {}
reply_intervals = {}
message_counts = {}
learning_tasks = {}


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
            guild_id INTEGER PRIMARY KEY,
            summary TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS learned_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER,
            user_id INTEGER,
            username TEXT,
            fact TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

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

    cur.execute("""
        CREATE TABLE IF NOT EXISTS language_settings (
            guild_id INTEGER PRIMARY KEY,
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
    return MODEL

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

def get_language(guild_id):
    if not guild_id:
        return LANG_EN
    conn = get_db()
    row = conn.execute("SELECT language FROM language_settings WHERE guild_id = ?", (guild_id,)).fetchone()
    conn.close()
    return row[0] if row and row[0] in (LANG_EN, LANG_VI) else LANG_EN

def set_language(guild_id, language):
    language = language if language in (LANG_EN, LANG_VI) else LANG_EN
    conn = get_db()
    conn.execute("""
        INSERT INTO language_settings (guild_id, language) VALUES (?, ?)
        ON CONFLICT(guild_id) DO UPDATE SET language = excluded.language
    """, (guild_id, language))
    conn.commit()
    conn.close()

def tr(guild_id, english, vietnamese):
    return vietnamese if get_language(guild_id) == LANG_VI else english


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


def get_message_count(guild_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM messages WHERE guild_id = ?",
        (guild_id,)
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


def get_summary(guild_id):
    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        "SELECT summary FROM learned_memory WHERE guild_id = ?",
        (guild_id,)
    )

    row = cur.fetchone()
    conn.close()

    return row[0] if row else ""


def save_summary(guild_id, summary):
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO learned_memory (guild_id, summary)
        VALUES (?, ?)
        ON CONFLICT(guild_id)
        DO UPDATE SET summary = excluded.summary
    """, (guild_id, summary))

    conn.commit()
    conn.close()


# =========================================================
# LEARNED FACTS
# =========================================================

def get_facts(guild_id, limit=100):
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        SELECT username, fact
        FROM learned_facts
        WHERE guild_id = ?
        ORDER BY id DESC
        LIMIT ?
    """, (guild_id, limit))

    rows = cur.fetchall()
    conn.close()

    rows.reverse()
    return rows


def save_fact(guild_id, user_id, username, fact):
    fact = fact.strip()

    if len(fact) < 3:
        return

    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        SELECT id FROM learned_facts
        WHERE guild_id = ? AND fact = ?
        LIMIT 1
    """, (guild_id, fact))

    if cur.fetchone():
        conn.close()
        return

    cur.execute("""
        INSERT INTO learned_facts
        (guild_id, user_id, username, fact)
        VALUES (?, ?, ?, ?)
    """, (guild_id, user_id, username, fact))

    cur.execute("""
        DELETE FROM learned_facts
        WHERE guild_id = ?
        AND id NOT IN (
            SELECT id FROM learned_facts
            WHERE guild_id = ?
            ORDER BY id DESC
            LIMIT ?
        )
    """, (guild_id, guild_id, get_tier_limits(guild_id)["max_facts"]))

    conn.commit()
    conn.close()


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
def ask_ollama(prompt):
    try:
        response = requests.post(
            OLLAMA_URL,
            json={"model": MODEL, "prompt": prompt, "stream": False, "options": {"temperature": 0.75, "top_p": 0.9}},
            timeout=180,
        )
        response.raise_for_status()
        return response.json().get("response", "").strip()
    except Exception as e:
        print("[OLLAMA ERROR]", e)
        return None

def ask_openai(prompt, model):
    if OPENAI_CLIENT is None:
        print("[OPENAI ERROR] OPENAI_API_KEY hoặc package openai chưa được cấu hình.")
        return None
    try:
        response = OPENAI_CLIENT.responses.create(model=model, input=[{"role": "user", "content": prompt}])
        return (response.output_text or "").strip()
    except Exception as e:
        print("[OPENAI ERROR]", repr(e))
        return None

async def ask_ai_async(guild_id, prompt):
    plan = get_plan(guild_id)
    if plan == PLAN_FREE:
        return await asyncio.to_thread(ask_ollama, prompt)
    return await asyncio.to_thread(ask_openai, prompt, plan_model(guild_id))

async def ask_ollama_async(prompt):
    return await asyncio.to_thread(ask_ollama, prompt)

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

async def compact_memory(guild_id):
    count = get_message_count(guild_id)

    if count < get_tier_limits(guild_id)["memory_trigger"]:
        return

    conn = get_db()
    cur = conn.cursor()

    remove_count = max(1, count - get_tier_limits(guild_id)["memory_keep"])

    cur.execute("""
        SELECT id, username, content
        FROM messages
        WHERE guild_id = ?
        ORDER BY id ASC
        LIMIT ?
    """, (guild_id, remove_count))

    rows = cur.fetchall()

    if not rows:
        conn.close()
        return

    old_text = "\n".join(
        f"{username}: {content}"
        for _, username, content in rows
    )

    old_summary = get_summary(guild_id)

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
        save_summary(guild_id, new_summary)

    ids = [row[0] for row in rows]
    placeholders = ",".join("?" for _ in ids)

    cur.execute(
        f"DELETE FROM messages WHERE id IN ({placeholders})",
        ids
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

    prompt = f"""
Bạn là hệ thống tự học của Discord AI.

Tin nhắn:
{content}

Hãy kiểm tra xem có điều gì đáng nhớ lâu dài không.

CÓ THỂ HỌC:
- sở thích
- cách người dùng muốn bot nói chuyện
- thông tin ổn định về server
- dự án dài hạn
- slang/meme phrase và nghĩa theo ngữ cảnh

KHÔNG HỌC:
- mật khẩu
- token
- API key
- thông tin đăng nhập
- tài chính
- dữ liệu nhạy cảm
- câu chửi tục như một phong cách mặc định
- câu nói nhất thời

Nếu là slang/meme mới:
GENZ: term | meaning | example

Nếu là fact:
FACT: một câu ngắn

Nếu không có gì:
NO_LEARN

Chỉ trả về đúng một dòng.
"""

    result = await ask_ai_async(message.guild.id, prompt)

    if not result:
        return

    result = result.strip()

    if result.startswith("FACT:"):
        fact = result[5:].strip()

        if len(fact) <= 300:
            save_fact(
                message.guild.id,
                message.author.id,
                message.author.display_name,
                fact
            )

            print("[LEARN FACT]", fact)

    elif result.startswith("GENZ:"):
        raw = result[5:].strip()
        parts = [p.strip() for p in raw.split("|")]

        if len(parts) >= 2:
            term = parts[0]
            meaning = parts[1]
            example = parts[2] if len(parts) >= 3 else ""

            save_genz_term(
                message.guild.id,
                term,
                meaning,
                example
            )

            print("[LEARN GENZ]", term, "=", meaning)


# =========================================================
# PROMPT
# =========================================================

def build_ai_prompt(message):
    guild = message.guild
    guild_id = guild.id

    summary = get_summary(guild_id)
    limits = get_tier_limits(guild_id)
    facts = get_facts(guild_id, limits["facts"])
    recent = get_recent_messages(guild_id, limits["recent"])
    genz = get_genz_terms(guild_id, limits["genz"])
    emotes = get_server_emotes(guild)
    gif_memory = get_server_gif_memory(guild_id, limits["gifs"])

    current = clean_message_content(message)
    language_name = LANGUAGE_LABELS.get(get_language(guild_id), "English")

    prompt = f"""
Bạn là một Discord AI chatbot.

MỤC TIÊU:
- Hiểu ngữ cảnh tốt.
- Trả lời tự nhiên, ngắn gọn khi câu hỏi đơn giản.
- Khi cần thì giải thích rõ.
- Không bịa thông tin.
- Không lặp lại chính mình.
- Có thể nói tiếng Việt, tiếng Anh hoặc trộn hai ngôn ngữ
  nếu phù hợp với cách nói của người dùng.

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
    f"- GIF_ID={gid} | uses={uses} | context={ctx or "(không có text)"}"
    for gid, url, ctx, uses in gif_memory
) if gif_memory else "(chưa học được GIF nào)"}

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
        for username, fact in facts:
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
# READY
# =========================================================

@bot.event
async def on_ready():
    init_db()
    load_settings()
    seed_default_genz()

    try:
        # Sync global commands. Discord may take some time to propagate global commands.
        synced = await bot.tree.sync()
        print(f"[BOT] Synced {len(synced)} global slash commands.")
    except Exception as e:
        print("[BOT] Global sync error:", e)

    print("=" * 55)
    print("DISCORD AI BOT ONLINE")
    print(f"Free AI (Ollama): {MODEL}")
    print(f"Standard AI: {STANDARD_MODEL}")
    print(f"Premium AI: {PREMIUM_MODEL}")
    print(f"OpenAI configured: {"YES" if openai_available() else "NO"}")
    print("Memory: ON")
    print("Self-learning: ON")
    print("Gen Z learning: ON")
    print("Custom emote support: ON")
    print("GIF library: ON")
    print("Direct mention: ON")
    print("Reply-to-bot: ON")
    print(f"Interval: {MIN_INTERVAL}-{MAX_INTERVAL}")
    print("=" * 55)


# =========================================================
# SLASH COMMANDS
# =========================================================

@bot.tree.command(name="language", description="Choose the bot language for this server")
@app_commands.describe(language="Choose English or Vietnamese")
@app_commands.choices(language=[
    app_commands.Choice(name="English", value="en"),
    app_commands.Choice(name="Tiếng Việt", value="vi"),
])
async def language_command(interaction: discord.Interaction, language: app_commands.Choice[str]):
    if not interaction.guild:
        await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        return
    set_language(interaction.guild.id, language.value)
    if language.value == LANG_VI:
        await interaction.response.send_message("🇻🇳 Đã chuyển ngôn ngữ bot sang Tiếng Việt.")
    else:
        await interaction.response.send_message("🇬🇧 Bot language has been set to English.")

@bot.command(name="language")
async def language_prefix(ctx, language: str = ""):
    if not ctx.guild:
        await ctx.send("This command can only be used in a server.")
        return
    value = language.lower().strip()
    if value in ("vi", "vietnamese", "tiengviet", "tiếng_việt"):
        set_language(ctx.guild.id, LANG_VI)
        await ctx.send("🇻🇳 Đã chuyển ngôn ngữ bot sang Tiếng Việt.")
    elif value in ("en", "english"):
        set_language(ctx.guild.id, LANG_EN)
        await ctx.send("🇬🇧 Bot language has been set to English.")
    else:
        await ctx.send("Use `!language en` or `!language vi`. You can also use `/language`.")


@bot.tree.command(name="ai_on", description="Enable the AI chatbot")
@app_commands.checks.has_permissions(manage_guild=True)
async def ai_on(interaction: discord.Interaction):
    guild_id = interaction.guild.id

    ai_enabled[guild_id] = True
    reply_intervals.setdefault(guild_id, DEFAULT_INTERVAL)
    message_counts[guild_id] = 0

    save_settings(guild_id)
    seed_default_genz()

    await interaction.response.send_message(tr(guild_id, "🤖 AI enabled.", "🤖 AI đã bật."))


@bot.tree.command(name="ai_off", description="Disable the AI chatbot")
@app_commands.checks.has_permissions(manage_guild=True)
async def ai_off(interaction: discord.Interaction):
    guild_id = interaction.guild.id

    ai_enabled[guild_id] = False
    save_settings(guild_id)

    await interaction.response.send_message(tr(guild_id, "🛑 AI disabled.", "🛑 AI đã tắt."))


@bot.tree.command(
    name="ai_channel",
    description="Choose the channel where AI can reply"
)
@app_commands.checks.has_permissions(manage_guild=True)
async def ai_channel(
    interaction: discord.Interaction,
    channel: discord.TextChannel
):
    guild_id = interaction.guild.id

    ai_channels[guild_id] = channel.id
    save_settings(guild_id)

    await interaction.response.send_message(tr(guild_id, f"✅ AI is active in {channel.mention}", f"✅ AI hoạt động ở {channel.mention}"))


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
            f"❌ Gói {plan_label(get_plan(guild_id))} chỉ cho interval `{limits['min_interval']}-{limits['max_interval']}`.",
            ephemeral=True,
        )
        return

    reply_intervals[guild_id] = int(interval)
    message_counts[guild_id] = 0
    save_settings(guild_id)

    await interaction.response.send_message(tr(guild_id, f"✅ Interval = **{interval}**\n🎲 Early reply = **25%**", f"✅ Interval = **{interval}**\n🎲 Early reply = **25%**"))


@bot.tree.command(name="status", description="View AI status")
async def status(interaction: discord.Interaction):
    guild_id = interaction.guild.id

    enabled = ai_enabled.get(guild_id, False)
    channel_id = ai_channels.get(guild_id)
    interval = reply_intervals.get(
        guild_id,
        DEFAULT_INTERVAL
    )

    memory = get_message_count(guild_id)
    genz_count = len(get_genz_terms(guild_id, 1000))
    gif_count = len(get_gif_keywords(guild_id))

    channel = (
        interaction.guild.get_channel(channel_id)
        if channel_id else None
    )

    await interaction.response.send_message(
        f"""
**AI Status**

Trạng thái: {"🟢 Bật" if enabled else "🔴 Tắt"}
Channel: {channel.mention if channel else "Chưa đặt"}
Interval: {interval}
Memory: {memory}
Gen Z dictionary: {genz_count}
GIF keywords: {gif_count}
Early reply: 25%
Direct mention: ON
Reply-to-bot: ON
Self-learning: ON
"""
    )


@bot.tree.command(
    name="ai_memory",
    description="View AI memory"
)
async def ai_memory(interaction: discord.Interaction):
    guild_id = interaction.guild.id

    summary = get_summary(guild_id)
    facts = get_facts(guild_id, 15)
    genz = get_genz_terms(guild_id, 15)

    text = "🧠 **AI MEMORY**\n\n"

    text += "**Summary:**\n"
    text += (summary[:1200] if summary else "Chưa có.")

    text += "\n\n**Facts:**\n"
    if facts:
        for username, fact in facts:
            text += f"• {username}: {fact}\n"
    else:
        text += "Chưa có."

    text += "\n**Gen Z:**\n"
    if genz:
        for term, meaning, _, _, _ in genz:
            text += f"• `{term}` = {meaning}\n"
    else:
        text += "Chưa có."

    await interaction.response.send_message(text[:1900])


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
        tr(interaction.guild.id, f"🧠 Added `{term}` to the Gen Z dictionary.", f"🧠 Đã thêm `{term}` vào Gen Z dictionary.")
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
            tr(interaction.guild.id, "❌ Invalid URL.", "❌ URL không hợp lệ."),
            ephemeral=True
        )
        return

    add_gif(
        interaction.guild.id,
        keyword,
        url
    )

    await interaction.response.send_message(
        tr(interaction.guild.id, f"🎬 Added a GIF for keyword `{keyword}`.", f"🎬 Đã thêm GIF cho keyword `{keyword}`.")
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
            ftr(interaction.guild.id, f"❌ No GIF found for `{keyword}`.", f"❌ Chưa có GIF cho `{keyword}`.")
        )
        return

    await interaction.response.send_message(
        random.choice(urls)
    )


@bot.tree.command(
    name="help",
    description="View help and commands"
)
async def help_command(interaction: discord.Interaction):
    await interaction.response.send_message(
        """
🤖 **Discord AI**

`/ai_on` → bật AI
`/ai_off` → tắt AI
`/ai_channel` → chọn channel
`/ai_interval` → 5-15 tin
`/status` → trạng thái
`/ai_memory` → memory

**Gen Z**
`/genz_add` → thêm slang thủ công

**GIF**
`/gif_add` → thêm GIF URL
`/gif` → gửi GIF

AI tự học:
• facts
• Gen Z / internet slang
• ngữ cảnh hội thoại
• custom emote

AI trả lời ngay khi:
• @mention bot
• Reply tin nhắn bot

Chat bình thường:
• interval
• 25% early reply
"""
    )


# =========================================================\n# PREFIX COMMANDS (!) + SUPPORT + PREMIUM
# =========================================================

async def send_long_text(ctx, text):
    for i in range(0, len(text), 1900):
        await ctx.send(text[i:i+1900])

async def send_support_request(user, guild, message_text, source):
    if not SUPPORT_CHANNEL_ID:
        return False, "Chưa cấu hình SUPPORT_CHANNEL_ID trong .env."

    channel = bot.get_channel(SUPPORT_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(SUPPORT_CHANNEL_ID)
        except discord.NotFound:
            return False, f"Không tìm thấy channel với ID {SUPPORT_CHANNEL_ID}. Hãy kiểm tra lại ID."
        except discord.Forbidden:
            return False, "Discord không cho bot truy cập channel này. Kiểm tra quyền View Channel."
        except Exception as e:
            print("[SUPPORT FETCH ERROR]", repr(e))
            return False, "Không thể truy cập support channel. Kiểm tra ID và quyền của bot."

    # Hỗ trợ TextChannel, Thread và các channel/messageable khác có thể gửi tin.
    if channel is None or not hasattr(channel, "send"):
        return False, f"ID {SUPPORT_CHANNEL_ID} không phải channel có thể gửi tin."

    embed = discord.Embed(
        title="📩 Support Request",
        description=(message_text or "Không có nội dung.")[:4000],
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(name="User", value=f"{user} (`{user.id}`)", inline=False)
    embed.add_field(
        name="Server",
        value=f"{guild.name} (`{guild.id}`)" if guild else "DM",
        inline=False,
    )
    embed.add_field(name="Source", value=source, inline=True)

    try:
        await channel.send(embed=embed)
        return True, "Đã gửi."
    except discord.Forbidden:
        return False, "Bot không có quyền gửi vào support channel."
    except Exception as e:
        print("[SUPPORT ERROR]", repr(e))
        return False, "Không thể gửi support request."


@bot.command(name="support")
async def support_prefix(ctx, *, message: str = "Người dùng yêu cầu hỗ trợ."):
    ok, result = await send_support_request(
        ctx.author, ctx.guild, message, "!support"
    )
    await ctx.send("✅ Đã gửi yêu cầu support." if ok else f"❌ {result}")


@bot.tree.command(name="support", description="Send a support request to the support channel")
async def support_slash(
    interaction: discord.Interaction,
    message: str = "Người dùng yêu cầu hỗ trợ.",
):
    ok, result = await send_support_request(
        interaction.user, interaction.guild, message, "/support"
    )
    await interaction.response.send_message(
        "✅ Đã gửi yêu cầu support." if ok else f"❌ {result}",
        ephemeral=True,
    )


@bot.command(name="premium")
async def premium_prefix(ctx):
    if not ctx.guild:
        await ctx.send("❌ Lệnh này chỉ dùng trong server.")
        return

    plan = get_plan(ctx.guild.id)
    info = get_plan_info(ctx.guild.id)
    lim = get_tier_limits(ctx.guild.id)

    text = (
        f"**{plan_label(plan)}**\n"
        f"AI: `{plan_model(ctx.guild.id)}`\n"
        f"Memory: `{lim['memory_trigger']} → {lim['memory_keep']}`\n"
        f"Facts: `{lim['max_facts']}`\n"
        f"Gen Z: `{lim['max_genz']}`"
    )
    if info.get("expires_at"):
        text += f"\nExpires: `{info['expires_at']}`"

    await ctx.send(text)


@bot.tree.command(name="premium", description="View the server plan")
async def premium_slash(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message(
            "❌ Lệnh này chỉ dùng trong server.", ephemeral=True
        )
        return

    plan = get_plan(interaction.guild.id)
    info = get_plan_info(interaction.guild.id)
    lim = get_tier_limits(interaction.guild.id)

    embed = discord.Embed(title="💎 Premium Status")
    embed.add_field(name="Plan", value=plan_label(plan), inline=True)
    embed.add_field(name="AI", value=f"`{plan_model(interaction.guild.id)}`", inline=True)
    embed.add_field(
        name="Memory",
        value=f"`{lim['memory_trigger']} → {lim['memory_keep']}`",
        inline=False,
    )
    embed.add_field(name="Facts", value=str(lim["max_facts"]), inline=True)
    embed.add_field(name="Gen Z", value=str(lim["max_genz"]), inline=True)
    if info.get("expires_at"):
        embed.add_field(name="Expires", value=f"`{info['expires_at']}`", inline=False)

    await interaction.response.send_message(embed=embed)


def valid_plan(plan):
    return plan.lower() in (PLAN_STANDARD, PLAN_PREMIUM)


@bot.command(name="premium_add")
@commands.is_owner()
async def premium_add_prefix(ctx, guild_id: int, plan: str):
    plan = plan.lower().strip()
    if not valid_plan(plan):
        await ctx.send("❌ Plan phải là `standard` hoặc `premium`.")
        return

    set_plan(guild_id, plan, source="manual")
    await ctx.send(
        f"✅ Đã cấp **{plan_label(plan)}** cho server `{guild_id}`."
    )


@bot.command(name="premium_remove")
@commands.is_owner()
async def premium_remove_prefix(ctx, guild_id: int):
    remove_plan(guild_id)
    await ctx.send(f"✅ Đã đưa server `{guild_id}` về Free.")


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
            "❌ Chỉ owner bot mới dùng được.", ephemeral=True
        )
        return

    try:
        gid = int(guild_id)
    except ValueError:
        await interaction.response.send_message(
            "❌ Guild ID phải là số.", ephemeral=True
        )
        return

    set_plan(gid, plan.value, source="manual")
    await interaction.response.send_message(
        f"✅ Đã cấp **{plan_label(plan.value)}** cho server `{gid}`.",
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
            "❌ Chỉ owner bot mới dùng được.", ephemeral=True
        )
        return

    try:
        gid = int(guild_id)
    except ValueError:
        await interaction.response.send_message(
            "❌ Guild ID phải là số.", ephemeral=True
        )
        return

    remove_plan(gid)
    await interaction.response.send_message(
        f"✅ Đã đưa server `{gid}` về Free.",
        ephemeral=True,
    )


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
    """Tạo ảnh với một dải nền trắng ở phía trên và chữ giữ nguyên nội dung."""
    def build():
        with Image.open(BytesIO(image_bytes)) as original:
            # GIF -> lấy frame đầu để output ổn định.
            if getattr(original, "is_animated", False):
                original.seek(0)
            img = original.convert("RGB")
            width, height = img.size

            # Tối đa 160px, tối thiểu 90px; tùy chiều cao ảnh.
            band_h = max(90, min(160, int(height * 0.18)))
            canvas = Image.new("RGB", (width, height + band_h), "white")
            canvas.paste(img, (0, band_h))

            draw = ImageDraw.Draw(canvas)
            clean_text = str(text)
            font_size = max(18, min(64, int(band_h * 0.48)))

            # Thu nhỏ font để cố gắng giữ caption trên một dòng.
            while font_size > 16:
                font = _find_font(font_size)
                bbox = draw.textbbox((0, 0), clean_text, font=font)
                if bbox[2] - bbox[0] <= width - 40:
                    break
                font_size -= 2

            font = _find_font(font_size)
            bbox = draw.textbbox((0, 0), clean_text, font=font)
            text_w = bbox[2] - bbox[0]
            text_h = bbox[3] - bbox[1]
            x = max(20, (width - text_w) // 2)
            y = max(0, (band_h - text_h) // 2 - bbox[1])
            draw.text((x, y), clean_text, fill="black", font=font)

            out = BytesIO()
            canvas.save(out, format="PNG", optimize=True)
            out.seek(0)
            return out.read()

    return await asyncio.to_thread(build)


async def _caption_from_prefix(ctx, text: str):
    if not text.strip():
        await ctx.send("❌ Dùng: `!caption <chữ>` rồi đính kèm ảnh hoặc reply vào ảnh.")
        return

    image_bytes, filename = await _get_image_bytes_from_message(ctx.message)
    if not image_bytes:
        await ctx.send("❌ Hãy đính kèm ảnh với lệnh hoặc reply vào một tin nhắn có ảnh.")
        return

    try:
        result = await _make_caption_image(image_bytes, text)
        await ctx.send(file=discord.File(BytesIO(result), filename="caption.png"))
    except Exception as e:
        print("[CAPTION ERROR]", repr(e))
        await ctx.send("❌ Không thể tạo ảnh caption.")


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
        await interaction.response.send_message("❌ Caption không được để trống.", ephemeral=True)
        return

    image_bytes = None
    if image is not None:
        try:
            image_bytes = await image.read()
        except Exception:
            image_bytes = None

    if image_bytes is None:
        await interaction.response.send_message(
            "❌ Hãy chọn ảnh ở ô `image`. Với prefix, có thể reply vào ảnh.",
            ephemeral=True,
        )
        return

    await interaction.response.defer()
    try:
        result = await _make_caption_image(image_bytes, text)
        await interaction.followup.send(file=discord.File(BytesIO(result), filename="caption.png"))
    except Exception as e:
        print("[CAPTION ERROR]", repr(e))
        await interaction.followup.send("❌ Không thể tạo ảnh caption.")


async def _send_avatar(ctx, target=None):
    """Hiển thị avatar của người dùng lệnh; nếu có @mention thì hiển thị avatar người được mention."""
    user = target or ctx.author
    try:
        avatar_url = user.display_avatar.url
        embed = discord.Embed(
            title=f"🖼️ Avatar của {user.display_name}",
            color=discord.Color.blurple(),
        )
        embed.set_image(url=avatar_url)
        embed.set_footer(text=f"@{user.name}")
        await ctx.send(embed=embed)
    except Exception as e:
        print("[AVATAR ERROR]", repr(e))
        await ctx.send("❌ Không thể lấy avatar của người dùng này.")


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
            title=f"🖼️ Avatar của {target.display_name}",
            color=discord.Color.blurple(),
        )
        embed.set_image(url=avatar_url)
        embed.set_footer(text=f"@{target.name}")
        await interaction.response.send_message(embed=embed)
    except Exception as e:
        print("[AVATAR ERROR]", repr(e))
        await interaction.response.send_message("❌ Không thể lấy avatar của người dùng này.", ephemeral=True)


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
    await ctx.send("🤖 AI đã bật.")


@bot.command(name="ai_off")
@commands.has_guild_permissions(manage_guild=True)
async def ai_off_prefix(ctx):
    guild_id = ctx.guild.id
    ai_enabled[guild_id] = False
    save_settings(guild_id)
    await ctx.send("🛑 AI đã tắt.")


@bot.command(name="ai_channel")
@commands.has_guild_permissions(manage_guild=True)
async def ai_channel_prefix(ctx, channel: discord.TextChannel):
    guild_id = ctx.guild.id
    ai_channels[guild_id] = channel.id
    save_settings(guild_id)
    await ctx.send(f"✅ AI hoạt động ở {channel.mention}")


@bot.command(name="ai_interval")
@commands.has_guild_permissions(manage_guild=True)
async def ai_interval_prefix(ctx, interval: int):
    guild_id = ctx.guild.id
    limits = get_tier_limits(guild_id)
    if not (limits["min_interval"] <= interval <= limits["max_interval"]):
        await ctx.send(
            f"❌ Gói {plan_label(get_plan(guild_id))} chỉ cho interval `"
            f"{limits['min_interval']}-{limits['max_interval']}`."
        )
        return
    reply_intervals[guild_id] = interval
    message_counts[guild_id] = 0
    save_settings(guild_id)
    await ctx.send(f"✅ Interval = **{interval}**\n🎲 Early reply = **25%**")


@bot.command(name="status")
async def status_prefix(ctx):
    guild_id = ctx.guild.id
    enabled = ai_enabled.get(guild_id, False)
    channel_id = ai_channels.get(guild_id)
    interval = reply_intervals.get(guild_id, DEFAULT_INTERVAL)
    memory = get_message_count(guild_id)
    genz_count = len(get_genz_terms(guild_id, 1000))
    gif_count = len(get_gif_keywords(guild_id))
    channel = ctx.guild.get_channel(channel_id) if channel_id else None
    await ctx.send(
        f"**AI Status**\n\n"
        f"Trạng thái: {'🟢 Bật' if enabled else '🔴 Tắt'}\n"
        f"Channel: {channel.mention if channel else 'Chưa đặt'}\n"
        f"Interval: {interval}\nMemory: {memory}\n"
        f"Gen Z dictionary: {genz_count}\nGIF keywords: {gif_count}\n"
        f"Early reply: 25%\nDirect mention: ON\nReply-to-bot: ON\nSelf-learning: ON"
    )


@bot.command(name="ai_memory")
async def ai_memory_prefix(ctx):
    guild_id = ctx.guild.id
    summary = get_summary(guild_id)
    facts = get_facts(guild_id, 15)
    genz = get_genz_terms(guild_id, 15)
    text = "🧠 **AI MEMORY**\n\n**Summary:**\n"
    text += summary[:1200] if summary else "Chưa có."
    text += "\n\n**Facts:**\n"
    text += "".join(f"• {username}: {fact}\n" for username, fact in facts) if facts else "Chưa có.\n"
    text += "\n**Gen Z:**\n"
    text += "".join(f"• `{term}` = {meaning}\n" for term, meaning, _, _, _ in genz) if genz else "Chưa có."
    await ctx.send(text[:1900])


@bot.command(name="genz_add")
@commands.has_guild_permissions(manage_guild=True)
async def genz_add_prefix(ctx, term: str, meaning: str, *, example: str = ""):
    save_genz_term(ctx.guild.id, term, meaning, example)
    await ctx.send(tr(interaction.guild.id, f"🧠 Added `{term}` to the Gen Z dictionary.", f"🧠 Đã thêm `{term}` vào Gen Z dictionary."))


@bot.command(name="gif_add")
@commands.has_guild_permissions(manage_guild=True)
async def gif_add_prefix(ctx, keyword: str, url: str):
    if not (url.startswith("https://") or url.startswith("http://")):
        await ctx.send(tr(interaction.guild.id, "❌ Invalid URL.", "❌ URL không hợp lệ."))
        return
    add_gif(ctx.guild.id, keyword, url)
    await ctx.send(tr(interaction.guild.id, f"🎬 Added a GIF for keyword `{keyword}`.", f"🎬 Đã thêm GIF cho keyword `{keyword}`."))


@bot.command(name="gif")
async def gif_prefix(ctx, *, keyword: str = ""):
    urls = get_gifs(ctx.guild.id, keyword)
    if not urls:
        await ctx.send("❌ Không tìm thấy GIF cho keyword này.")
        return
    await ctx.send(random.choice(urls))


@bot.command(name="help")
async def help_prefix(ctx):
    await ctx.send(
        "**📖 BOT COMMANDS**\n\n"
        "`!ai_on` / `!ai_off` → bật/tắt AI\n"
        "`!ai_channel #channel` → chọn channel AI\n"
        "`!ai_interval <số>` → đặt interval\n"
        "`!status` → bot status\n"
        "`!ai_memory` → xem memory\n"
        "`!genz_add <term> <meaning> [example]` → thêm Gen Z\n"
        "`!gif_add <keyword> <url>` → thêm GIF\n"
        "`!gif <keyword>` → gửi GIF\n"
        "`!caption <chữ>` + ảnh → thêm nền trắng + chữ phía trên\n"
        "`!avatar` → xem avatar của bạn\n"        "`!avatar @user` → xem avatar của người được mention\n"
        "`!support <message>` → send support\n"
        "`!premium` → xem gói server\n"
        "`!premium_add <server_id> <standard|premium>` → cấp gói (owner)\n"
        "`!premium_remove <server_id>` → về Free (owner)"
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

    if ai_channels.get(guild_id) != channel_id:
        await bot.process_commands(message)
        return

    save_message(message)

    if get_message_count(guild_id) >= MEMORY_TRIGGER:
        try:
            await compact_memory(guild_id)
        except Exception as e:
            print("[MEMORY ERROR]", e)

    mentioned = bool(
        bot.user and bot.user in message.mentions
    )

    replied_to_bot = await is_reply_to_bot(message)

    direct = mentioned or replied_to_bot

    if direct:
        message_counts[guild_id] = 0
        should_reply = True
    else:
        count = message_counts.get(guild_id, 0) + 1
        message_counts[guild_id] = count

        interval = reply_intervals.get(
            guild_id,
            DEFAULT_INTERVAL
        )

        should_reply = (
            count >= interval
            or random.random() < EARLY_REPLY_CHANCE
        )

        if should_reply:
            message_counts[guild_id] = 0

    if not should_reply:
        await bot.process_commands(message)
        return

    try:
        prompt = build_ai_prompt(message)

        async with message.channel.typing():
            answer = await ask_ai_async(guild_id, prompt)

        if not answer:
            await message.channel.send(
                "⚠️ AI không trả về phản hồi. Hãy kiểm tra Ollama/OpenAI và .env."
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
                "⚠️ Có lỗi khi xử lý AI."
            )
        except Exception:
            pass

    await bot.process_commands(message)


# =========================================================
# COMMAND ERROR HANDLERS
# =========================================================

async def permission_error(interaction, error):
    if isinstance(
        error,
        app_commands.errors.MissingPermissions
    ):
        if interaction.response.is_done():
            await interaction.followup.send(
                "❌ Bạn cần quyền Manage Server.",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "❌ Bạn cần quyền Manage Server.",
                ephemeral=True
            )


ai_on.error(permission_error)
ai_off.error(permission_error)
ai_channel.error(permission_error)
ai_interval.error(permission_error)
genz_add.error(permission_error)
gif_add.error(permission_error)


# =========================================================
# START
# =========================================================

if not TOKEN:
    print("❌ Không tìm thấy DISCORD_TOKEN trong .env")
else:
    init_db()
    bot.run(TOKEN)
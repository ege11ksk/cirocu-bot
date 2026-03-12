import os
import re
import json
import logging
import threading
import datetime
import requests
import telebot
import pytz
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from flask import Flask
from apscheduler.schedulers.background import BackgroundScheduler

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN environment variable is not set.")

bot = telebot.TeleBot(TOKEN)
bot_me = None  # initialized in main()

DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "database.json")
data_lock = threading.Lock()
TZ = pytz.timezone("Europe/Istanbul")

# ── Persistence ───────────────────────────────────────────────────────────────

DEFAULT_DB: dict = {
    "users": {},
    "active_users_today": [],
    "last_reset_date": datetime.date.today().isoformat(),
}

DEFAULT_USER: dict = {
    "total_turnover": 0.0,
    "daily_income": 0.0,
    "daily_expense": 0.0,
}


def load_db() -> dict:
    try:
        with open(DB_FILE, "r") as f:
            data = json.load(f)
        # Migration: if old flat format, discard and start fresh per-user
        if "users" not in data:
            logger.info("Migrating old DB to per-user format.")
            data = DEFAULT_DB.copy()
            data["users"] = {}
        if "active_users_today" not in data:
            data["active_users_today"] = []
        if "last_reset_date" not in data:
            data["last_reset_date"] = datetime.date.today().isoformat()
        return data
    except (FileNotFoundError, json.JSONDecodeError):
        return {
            "users": {},
            "active_users_today": [],
            "last_reset_date": datetime.date.today().isoformat(),
        }


def save_db(data: dict) -> None:
    db_dir = os.path.dirname(DB_FILE)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    with open(DB_FILE, "w") as f:
        json.dump(data, f, indent=2)


def get_user_data(user_id: int) -> dict:
    """Return (and init if needed) per-user stats dict."""
    uid = str(user_id)
    if uid not in db["users"]:
        db["users"][uid] = DEFAULT_USER.copy()
    u = db["users"][uid]
    for k, v in DEFAULT_USER.items():
        if k not in u:
            u[k] = v
    return u


db: dict = load_db()

# Maps bot message_id → TRY-equivalent amount for that transaction
pending_transactions: dict[int, float] = {}

# ── Exchange rates (CoinGecko) ────────────────────────────────────────────────

_rates_cache: dict = {}
_rates_lock = threading.Lock()


def get_rates() -> dict | None:
    """Fetch live USDT/TRX rates in TRY and USD from CoinGecko."""
    try:
        resp = requests.get(
            "https://api.coingecko.com/api/v3/simple/price"
            "?ids=tether,tron&vs_currencies=try,usd",
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        with _rates_lock:
            _rates_cache.update(data)
        return data
    except Exception as exc:
        logger.error("CoinGecko fetch failed: %s", exc)
        with _rates_lock:
            return _rates_cache.copy() if _rates_cache else None


def convert_amount(amount: float, from_c: str, to_c: str, rates: dict) -> float | None:
    """Convert between USDT / TRY / TRX using CoinGecko rates."""
    from_c, to_c = from_c.lower(), to_c.lower()
    if from_c == to_c:
        return amount

    def to_usd(c: str, v: float) -> float | None:
        if c == "usdt":
            return v * rates["tether"]["usd"]
        if c == "trx":
            return v * rates["tron"]["usd"]
        if c == "try":
            return v / rates["tether"]["try"] * rates["tether"]["usd"]
        return None

    def to_try(c: str, v: float) -> float | None:
        if c == "usdt":
            return v * rates["tether"]["try"]
        if c == "trx":
            return v * rates["tron"]["try"]
        if c == "try":
            return v
        return None

    def from_usd(usd: float, c: str) -> float | None:
        if c == "usdt":
            return usd / rates["tether"]["usd"]
        if c == "trx":
            return usd / rates["tron"]["usd"]
        if c == "try":
            return usd / rates["tether"]["usd"] * rates["tether"]["try"]
        return None

    if to_c == "try":
        return to_try(from_c, amount)
    if from_c == "try":
        usd = to_usd("try", amount)
        return from_usd(usd, to_c) if usd is not None else None
    usd = to_usd(from_c, amount)
    return from_usd(usd, to_c) if usd is not None else None


def to_try_equivalent(amount: float, currency: str, rates: dict) -> float:
    """Always return a TRY equivalent for turnover tracking."""
    result = convert_amount(amount, currency, "try", rates)
    return result if result is not None else amount


# ── Number formatting ─────────────────────────────────────────────────────────

def fmt_try(n: float) -> str:
    """Turkish style: 1.234,56"""
    s = f"{n:,.2f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def fmt_amount(n: float, currency: str) -> str:
    c = currency.upper()
    if c == "TRY":
        return f"{fmt_try(n)} TRY"
    s = f"{n:.6f}".rstrip("0").rstrip(".")
    return f"{s} {c}"


# ── Trigger regex ─────────────────────────────────────────────────────────────

TRIGGER = re.compile(
    r"(\d+(?:[+\-]\d+)*)"
    r"\s+(usdt|try|trx)"
    r"(?:\s+%(\d+(?:\.\d+)?))?"
    r"(?:\s+to\s+(usdt|try|trx))?",
    re.IGNORECASE,
)


# ── Math evaluator ────────────────────────────────────────────────────────────

def safe_eval(expr: str) -> float | None:
    clean = expr.strip()
    if not re.fullmatch(r"\d+([+\-]\d+)*", clean):
        return None
    try:
        return float(eval(clean))  # noqa: S307
    except Exception:
        return None


# ── Inline keyboard ───────────────────────────────────────────────────────────

def make_keyboard(msg_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup()
    kb.add(
        InlineKeyboardButton("+ Kâr İşle",  callback_data=f"profit_{msg_id}"),
        InlineKeyboardButton("- Zarar İşle", callback_data=f"loss_{msg_id}"),
    )
    return kb


# ── Reports ───────────────────────────────────────────────────────────────────

def send_instant_report(user_id: int) -> None:
    """DM the user their own current total turnover."""
    with data_lock:
        u = get_user_data(user_id)
        turnover = u["total_turnover"]
    msg = (
        "📊 Güncel Durum Özeti\n\n"
        f"💰 Toplam Kârın: <b>{fmt_try(turnover)} TL</b>\n\n"
        "güzel satış kanka devam et 🤓"
    )
    bot.send_message(user_id, msg, parse_mode="HTML")


def send_daily_report() -> None:
    """Send personal end-of-day report to each active user at 23:59."""
    with data_lock:
        users = list(db["active_users_today"])

    now = datetime.datetime.now(TZ)
    date_str = now.strftime("%d.%m.%Y")

    for uid in users:
        with data_lock:
            u = get_user_data(int(uid))
            daily_income  = u["daily_income"]
            daily_expense = u["daily_expense"]

        net = daily_income - daily_expense
        net_line = ""
        if net > 0:
            net_line = f"\n\n🟢 Net Kâr: ₺{fmt_try(net)}"
        elif net < 0:
            net_line = f"\n\n🔴 Net Zarar: ₺{fmt_try(abs(net))}"

        report = (
            f"📊 Gün Sonu Kar Raporu Taslağı\n\n"
            f"📅 Tarih: {date_str}\n\n"
            f"💰 Toplam Gelirin: ₺{fmt_try(daily_income)}\n\n"
            f"💸 Toplam Giderin: ₺{fmt_try(daily_expense)}\n\n"
            f"⚖️ Net Sonuç:{net_line}"
        )

        try:
            bot.send_message(uid, report)
        except Exception as exc:
            logger.warning("Could not send daily report to %s: %s", uid, exc)

    logger.info("Daily report sent to %d users.", len(users))


def reset_daily_stats() -> None:
    """Reset each user's daily stats at 00:00 (1 min after report)."""
    with data_lock:
        now = datetime.datetime.now(TZ)
        for uid in db["users"]:
            db["users"][uid]["daily_income"] = 0.0
            db["users"][uid]["daily_expense"] = 0.0
        db["active_users_today"] = []
        db["last_reset_date"] = now.date().isoformat()
        save_db(db)
    logger.info("Daily stats reset at 00:00 for all users.")


# ── Transaction processor ─────────────────────────────────────────────────────

def _maybe_reset_daily() -> None:
    """Reset daily stats if the date has changed (call while holding data_lock)."""
    today = datetime.datetime.now(TZ).date().isoformat()
    if db.get("last_reset_date") != today:
        for uid in db["users"]:
            db["users"][uid]["daily_income"] = 0.0
            db["users"][uid]["daily_expense"] = 0.0
        db["active_users_today"] = []
        db["last_reset_date"] = today


def process_transaction(
    chat_id: int,
    user_id: int,
    try_amount: float,
    add: bool = True,
    specific_minus: float | None = None,
) -> None:
    with data_lock:
        _maybe_reset_daily()
        u = get_user_data(user_id)

        if specific_minus is not None:
            actual = try_amount - specific_minus
            u["total_turnover"] += actual
            if actual >= 0:
                u["daily_income"] += actual
            else:
                u["daily_expense"] += abs(actual)
        elif add:
            u["total_turnover"] += try_amount
            u["daily_income"]   += try_amount
        else:
            u["total_turnover"] -= try_amount
            u["daily_expense"]  += try_amount

        if user_id not in db["active_users_today"]:
            db["active_users_today"].append(user_id)

        save_db(db)

    bot.send_message(
        chat_id,
        "✅ İşlem başarılı. Güncel rapor özel mesaj (DM) olarak gönderildi.",
    )

    try:
        send_instant_report(user_id)
    except Exception:
        bot.send_message(
            chat_id,
            "⚠️ Lütfen raporu alabilmek için botun içine girip Başlat (Start) butonuna basın!",
        )


# ── /start handler ────────────────────────────────────────────────────────────

@bot.message_handler(commands=["start"])
def handle_start(message: telebot.types.Message) -> None:
    user = message.from_user
    raw_name = f"@{user.username}" if user.username else user.first_name
    isim = telebot.util.escape(raw_name)
    bot.reply_to(
        message,
        f"<b>Selam {isim} 👋 CİROCU bota hoş geldin!\n"
        " Cirocu Bot üzerinden kripto para birimleri ile hem oransal hem de birimsel hesaplamalar yapabilir, cironu 7/24 otomatik yönetebilirsin. Nasıl mı? Öyleyse başlayalım!</b>\n"
        "\n"
        " 🧮 1. HESAPLAMA KOMUTLARI\n"
        "\n"
        " * 100 usdt to try\n"
        "\n"
        " * 5000+1000-500 try\n"
        "\n"
        " * 150 usdt %30\n"
        "\n"
        " * try to usdt / trx to try / usdt to trx\n"
        "\n"
        " * 🟢 [+ Kâr İşle] butonu\n"
        " \n"
        " * 🔴 [- Zarar İşle] butonu\n"
        " \n"
        " <b>🕵️‍♂️ 2. GİZLİ VE OTOMATİK RAPORLAMA</b>\n"
        " \n"
        " <b>📩 Anlık Rapor:</b> Grupta ciro gizli kalır, işlemi yapana güncel kasa anında özelden (DM) gelir!\n"
        "\n"
        " <b>🌙 Gece Yarısı Fişi:</b> Her gece tam 23:59'da o günkü net kâr/zarar raporun otomatik olarak DM kutuna düşer.",
        parse_mode="HTML",
    )


# ── Main message handler ──────────────────────────────────────────────────────

@bot.message_handler(func=lambda m: True, content_types=["text"])
def handle_message(message: telebot.types.Message) -> None:
    text = message.text or ""

    # ── Reply shortcuts to a previous bot calculation ─────────────────────────
    if (
        message.reply_to_message
        and message.reply_to_message.from_user
        and bot_me
        and message.reply_to_message.from_user.id == bot_me.id
    ):
        orig_id = message.reply_to_message.message_id
        if orig_id in pending_transactions:
            try_amount = pending_transactions[orig_id]
            reply = text.strip()

            if reply == "+":
                process_transaction(message.chat.id, message.from_user.id, try_amount, add=True)
                return
            if reply == "-":
                process_transaction(message.chat.id, message.from_user.id, try_amount, add=False)
                return
            if re.fullmatch(r"-\d+(\.\d+)?", reply):
                minus_val = abs(float(reply))
                process_transaction(
                    message.chat.id, message.from_user.id, try_amount, specific_minus=minus_val
                )
                return

    # ── Calculation trigger ───────────────────────────────────────────────────
    match = TRIGGER.search(text)
    if not match:
        return

    expr      = match.group(1)
    from_curr = match.group(2).lower()
    pct_str   = match.group(3)
    to_curr   = (match.group(4) or "").lower()

    math_result = safe_eval(expr)
    if math_result is None:
        return

    if pct_str is not None:
        math_result = math_result * float(pct_str) / 100.0

    if from_curr == "try" and not to_curr:
        try_amount = math_result
        reply_text = fmt_amount(try_amount, "try")

    elif to_curr and from_curr != to_curr:
        rates = get_rates()
        if rates is None:
            bot.reply_to(message, "⚠️ Döviz kuru alınamadı. Lütfen tekrar deneyin.")
            return

        converted = convert_amount(math_result, from_curr, to_curr, rates)
        if converted is None:
            return

        reply_text = f"{fmt_amount(math_result, from_curr)} = {fmt_amount(converted, to_curr)}"

        if to_curr == "try":
            try_amount = converted
        elif from_curr == "try":
            try_amount = math_result
        else:
            try_amount = to_try_equivalent(math_result, from_curr, rates)

    elif pct_str is not None and not to_curr:
        rates = get_rates()
        try_amount = to_try_equivalent(math_result, from_curr, rates) if rates else math_result
        reply_text = fmt_amount(math_result, from_curr)

    else:
        return

    sent = bot.reply_to(message, reply_text, reply_markup=make_keyboard(0))
    pending_transactions[sent.message_id] = try_amount
    bot.edit_message_reply_markup(
        chat_id=sent.chat.id,
        message_id=sent.message_id,
        reply_markup=make_keyboard(sent.message_id),
    )


# ── Callback buttons ──────────────────────────────────────────────────────────

@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call: telebot.types.CallbackQuery) -> None:
    data = call.data or ""

    if data.startswith("profit_"):
        msg_id = int(data.split("_", 1)[1])
        if msg_id in pending_transactions:
            process_transaction(
                call.message.chat.id, call.from_user.id,
                pending_transactions[msg_id], add=True,
            )
        bot.answer_callback_query(call.id, "Kâr işlendi! ✅")

    elif data.startswith("loss_"):
        msg_id = int(data.split("_", 1)[1])
        if msg_id in pending_transactions:
            process_transaction(
                call.message.chat.id, call.from_user.id,
                pending_transactions[msg_id], add=False,
            )
        bot.answer_callback_query(call.id, "Zarar işlendi! ✅")


# ── Flask keep-alive ──────────────────────────────────────────────────────────

flask_app = Flask(__name__)


@flask_app.route("/")
def index():
    return "Bot is alive!", 200


def run_flask() -> None:
    flask_app.run(host="0.0.0.0", port=9000, use_reloader=False)


# ── Scheduler ─────────────────────────────────────────────────────────────────

def start_scheduler() -> None:
    scheduler = BackgroundScheduler(timezone=TZ)
    scheduler.add_job(send_daily_report, "cron", hour=23, minute=59)
    scheduler.add_job(reset_daily_stats, "cron", hour=0, minute=0)
    scheduler.start()
    logger.info("Scheduler started — report at 23:59, reset at 00:00 Istanbul time.")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    bot_me = bot.get_me()
    logger.info(f"Logged in as @{bot_me.username}")
    threading.Thread(target=run_flask, daemon=True).start()
    start_scheduler()
    logger.info("Bot is starting with polling…")
    bot.infinity_polling(allowed_updates=["message", "callback_query"])

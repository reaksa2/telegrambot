"""
Telegram Daily Payment Bot
--------------------------
Two ways to add a payment:

  1) Buttons:  tap 🍳 Breakfast / 🍜 Lunch / 🍲 Dinner / 📝 Other,
               then type the amount (e.g. 20000 or $2.5).
               For Other you can add a word: "15000 taxi".

  2) Typing:   pay 20000៛ breakfast   |  payment 15000 dinner  |  pay $2.5 coffee

On the last day of every month (23:59 local time) the bot sends a report to
every chat: totals per person and per comment.

Commands (also on the buttons):
  /start /help      - show buttons + how to use
  /report           - this month's report so far
  /report 2026-09   - report for a specific month
  /mine             - your own payments this month
  /undo             - delete your last payment

Groups:
  Every Telegram group is its own separate book. Family A's group only sees
  Family A's payments; Family B's group only sees Family B's. A private chat
  with the bot is a personal book. The same person can be in several groups;
  their payments are kept apart per group.

Running:
  - On your computer: no WEBHOOK_URL  -> the bot asks Telegram for messages (polling)
  - Online (Render):  RENDER_EXTERNAL_URL / WEBHOOK_URL set -> Telegram sends
    messages to https://<your-app>/telegram (webhook). cron-job.org calls
    https://<your-app>/cron/month-end?key=CRON_SECRET every night at 23:55 so
    the month-end report is sent even if the free host was asleep.

Storage:
  - DATABASE_URL set  -> PostgreSQL (e.g. free Neon / Supabase database)
  - otherwise         -> local SQLite file (DB_PATH, default payments.db)
"""

import asyncio
import hashlib
import html
import logging
import os
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from telegram import ForceReply, ReplyKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------- Settings (from environment variables) ----------------
TZ = ZoneInfo(os.getenv("BOT_TZ", "Asia/Phnom_Penh"))
DEFAULT_CURRENCY = os.getenv("DEFAULT_CURRENCY", "KHR").upper()  # KHR or USD
DB_PATH = os.getenv("DB_PATH", "payments.db")
DATABASE_URL = os.getenv("DATABASE_URL")  # postgres://... -> use PostgreSQL
NO_COMMENT = "other"
PENDING_MINUTES = 10  # how long the bot waits for an amount after a button tap

# Online hosting (webhook mode). Render sets RENDER_EXTERNAL_URL automatically.
WEBHOOK_URL = (os.getenv("WEBHOOK_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")
PORT = int(os.getenv("PORT", "10000"))
CRON_SECRET = os.getenv("CRON_SECRET", "")  # protects the /cron/month-end address

# ---------------- Buttons ----------------
CATEGORY_BUTTONS = {
    "🍳 Breakfast": "breakfast",
    "🍜 Lunch": "lunch",
    "🍲 Dinner": "dinner",
    "📝 Other": "other",
}
BTN_REPORT = "📊 Report"
BTN_MINE = "🧾 My payments"
BTN_UNDO = "↩️ Undo"

KEYBOARD = ReplyKeyboardMarkup(
    [
        ["🍳 Breakfast", "🍜 Lunch"],
        ["🍲 Dinner", "📝 Other"],
        [BTN_REPORT, BTN_MINE, BTN_UNDO],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

# ---------------- Parsing ----------------
KEYWORD_RE = re.compile(r"^\s*(?:payment|pay)\b\s*(.*)$", re.IGNORECASE | re.DOTALL)

# An amount with an optional currency mark before or after it.
# Matches: 20000  20,000  20000៛  ៛20000  20000r  20000riel  $2.5  2.5$  2.5usd
AMOUNT_RE = re.compile(
    r"^(?P<pre>\$|៛)?(?P<num>[0-9][0-9,]*(?:\.[0-9]{1,2})?)"
    r"(?P<post>\$|៛|r|riel|khr|usd)?$",
    re.IGNORECASE,
)
CURRENCY_WORDS = {"$": "USD", "usd": "USD", "៛": "KHR", "r": "KHR", "riel": "KHR", "khr": "KHR"}

# Khmer digits -> normal digits, so "២០០០០៛" works too
KHMER_DIGITS = str.maketrans("០១២៣៤៥៦៧៨៩", "0123456789")

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("payment-bot")


def parse_amount_and_comment(text):
    """'20000៛ breakfast' -> (20000.0, 'KHR', 'breakfast'); no amount -> (None, None, None)."""
    tokens = text.translate(KHMER_DIGITS).split()
    amount, currency, used = None, None, set()
    for i, tok in enumerate(tokens):
        a = AMOUNT_RE.match(tok)
        if not a:
            continue
        amount = float(a.group("num").replace(",", ""))
        mark = (a.group("pre") or a.group("post") or "").lower()
        used.add(i)
        # currency written as a separate word: "20000 ៛" / "5 usd" / "$ 5"
        if not mark and i + 1 < len(tokens) and tokens[i + 1].lower() in CURRENCY_WORDS:
            mark = tokens[i + 1].lower()
            used.add(i + 1)
        if not mark and i > 0 and tokens[i - 1] in ("$", "៛"):
            mark = tokens[i - 1]
            used.add(i - 1)
        currency = CURRENCY_WORDS.get(mark, DEFAULT_CURRENCY)
        break

    if amount is None:
        return (None, None, None)
    comment = " ".join(t for j, t in enumerate(tokens) if j not in used).strip().lower()
    return amount, currency, comment or NO_COMMENT


def parse_payment(text):
    """'pay 20000៛ breakfast' -> (amount, currency, comment).
    None if the text doesn't start with pay/payment."""
    m = KEYWORD_RE.match(text)
    if not m:
        return None
    return parse_amount_and_comment(m.group(1))


# ---------------- Database (SQLite or PostgreSQL) ----------------
if DATABASE_URL:
    import psycopg
    from psycopg.rows import dict_row


def run(sql, args=(), fetch=None):
    """Run one SQL statement. Write SQL with '?' placeholders; fetch = None | 'one' | 'all'."""
    if DATABASE_URL:
        with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
            cur = conn.execute(sql.replace("?", "%s"), args)
            if fetch == "one":
                return cur.fetchone()
            if fetch == "all":
                return cur.fetchall()
            return None
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            cur = conn.execute(sql, args)
            if fetch == "one":
                return cur.fetchone()
            if fetch == "all":
                return cur.fetchall()
            return None
    finally:
        conn.close()


def init_db():
    if DATABASE_URL:
        run(
            """
            CREATE TABLE IF NOT EXISTS payments (
                id         BIGSERIAL PRIMARY KEY,
                chat_id    BIGINT  NOT NULL,
                user_id    BIGINT  NOT NULL,
                user_name  TEXT    NOT NULL,
                amount     DOUBLE PRECISION NOT NULL,
                currency   TEXT    NOT NULL DEFAULT 'KHR',
                note       TEXT,
                month      TEXT    NOT NULL,
                created_at TEXT    NOT NULL
            )
            """
        )
        log.info("Storage: PostgreSQL")
    else:
        run(
            """
            CREATE TABLE IF NOT EXISTS payments (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id    INTEGER NOT NULL,
                user_id    INTEGER NOT NULL,
                user_name  TEXT    NOT NULL,
                amount     REAL    NOT NULL,
                currency   TEXT    NOT NULL DEFAULT 'KHR',
                note       TEXT,
                month      TEXT    NOT NULL,
                created_at TEXT    NOT NULL
            )
            """
        )
        # upgrade databases made by the first version (no currency column)
        cols = [r["name"] for r in run("PRAGMA table_info(payments)", fetch="all")]
        if "currency" not in cols:
            run(f"ALTER TABLE payments ADD COLUMN currency TEXT NOT NULL DEFAULT '{DEFAULT_CURRENCY}'")
        log.info("Storage: SQLite file %s", DB_PATH)
    run("CREATE INDEX IF NOT EXISTS idx_chat_month ON payments(chat_id, month)")
    # remembers which month-end reports were already sent, so none is sent twice
    run(
        "CREATE TABLE IF NOT EXISTS report_log ("
        " chat_id BIGINT NOT NULL, month TEXT NOT NULL, sent_at TEXT NOT NULL,"
        " PRIMARY KEY (chat_id, month))"
    )


def add_payment(chat_id, user_id, user_name, amount, currency, note):
    now = datetime.now(TZ)
    run(
        "INSERT INTO payments (chat_id, user_id, user_name, amount, currency, note, month, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (chat_id, user_id, user_name, amount, currency, note,
         now.strftime("%Y-%m"), now.isoformat(timespec="seconds")),
    )


def month_rows(chat_id, month, user_id=None):
    sql = "SELECT * FROM payments WHERE chat_id=? AND month=?"
    args = [chat_id, month]
    if user_id is not None:
        sql += " AND user_id=?"
        args.append(user_id)
    return run(sql + " ORDER BY id", args, fetch="all")


def delete_last(chat_id, user_id):
    return run(
        "DELETE FROM payments WHERE id = ("
        "  SELECT id FROM payments WHERE chat_id=? AND user_id=? ORDER BY id DESC LIMIT 1"
        ") RETURNING amount, currency, note",
        (chat_id, user_id),
        fetch="one",
    )


def migrate_chat(old_chat_id, new_chat_id):
    """A normal group that becomes a 'supergroup' gets a NEW chat id from Telegram.
    Move its history to the new id so the family keeps one continuous record."""
    run("UPDATE payments SET chat_id=? WHERE chat_id=?", (new_chat_id, old_chat_id))
    run("UPDATE report_log SET chat_id=? WHERE chat_id=?", (new_chat_id, old_chat_id))


def report_already_sent(chat_id, month):
    return run("SELECT 1 AS x FROM report_log WHERE chat_id=? AND month=?",
               (chat_id, month), fetch="one") is not None


def mark_report_sent(chat_id, month):
    run("INSERT INTO report_log (chat_id, month, sent_at) VALUES (?, ?, ?)",
        (chat_id, month, datetime.now(TZ).isoformat(timespec="seconds")))


def chats_with_payments(month):
    rows = run("SELECT DISTINCT chat_id FROM payments WHERE month=?", (month,), fetch="all")
    return [r["chat_id"] for r in rows]


# ---------------- Formatting ----------------
def money(amount, currency):
    if currency == "USD":
        return f"${amount:,.2f}"
    return f"{amount:,.0f}៛"


def totals_text(totals):
    """{'KHR': 45000, 'USD': 2.5} -> '45,000៛ + $2.50'"""
    parts = [money(totals[c], c) for c in ("KHR", "USD") if totals.get(c)]
    return " + ".join(parts) if parts else money(0, DEFAULT_CURRENCY)


def month_label(month):
    return datetime.strptime(month, "%Y-%m").strftime("%B %Y")


def summarize(rows, key):
    """Group rows by key -> sorted [(group, {'n': count, 'totals': {cur: sum}})]."""
    groups = defaultdict(lambda: {"n": 0, "totals": defaultdict(float)})
    for r in rows:
        g = groups[key(r)]
        g["n"] += 1
        g["totals"][r["currency"]] += r["amount"]
    # biggest first (rough riel-equivalent: KHR + USD*4000)
    return sorted(
        groups.items(),
        key=lambda kv: -(kv[1]["totals"].get("KHR", 0) + kv[1]["totals"].get("USD", 0) * 4000),
    )


def build_report(chat_id, month, chat_title=None):
    rows = month_rows(chat_id, month)
    title = f"📊 <b>Payment report – {month_label(month)}</b>"
    if chat_title:
        title += f"\n👨‍👩‍👧 {html.escape(chat_title)}"
    if not rows:
        return f"{title}\n\nNo payments recorded."

    names = {r["user_id"]: r["user_name"] for r in rows}  # latest name wins
    lines = [title, "", "👤 <b>By person</b>"]
    for i, (uid, g) in enumerate(summarize(rows, lambda r: r["user_id"]), 1):
        lines.append(
            f"{i}. {html.escape(names[uid])}: <b>{totals_text(g['totals'])}</b> ({g['n']})"
        )

    lines += ["", "🏷 <b>By comment</b>"]
    for note, g in summarize(rows, lambda r: r["note"] or NO_COMMENT):
        lines.append(f"• {html.escape(note)}: {totals_text(g['totals'])} ({g['n']})")

    grand = defaultdict(float)
    for r in rows:
        grand[r["currency"]] += r["amount"]
    lines += ["", f"💰 <b>Total: {totals_text(grand)}</b> ({len(rows)} payments)"]
    return "\n".join(lines)


def build_user_detail(chat_id, user_id, month):
    rows = month_rows(chat_id, month, user_id)
    if not rows:
        return f"You have no payments in {month_label(month)}."
    lines = [f"🧾 <b>Your payments – {month_label(month)}</b>", ""]
    total = defaultdict(float)
    for r in rows:
        day = datetime.fromisoformat(r["created_at"]).strftime("%d %b %H:%M")
        lines.append(f"{day}: {money(r['amount'], r['currency'])} – {html.escape(r['note'] or NO_COMMENT)}")
        total[r["currency"]] += r["amount"]

    lines += ["", "🏷 <b>By comment</b>"]
    for note, g in summarize(rows, lambda r: r["note"] or NO_COMMENT):
        lines.append(f"• {html.escape(note)}: {totals_text(g['totals'])} ({g['n']})")
    lines += ["", f"<b>Total: {totals_text(total)}</b>"]
    return "\n".join(lines)


def display_name(user):
    if user.username:
        return f"@{user.username}"
    return user.full_name or str(user.id)


# ---------------- Handlers ----------------
HELP_TEXT = (
    "👋 Hi! I record daily payments.\n\n"
    "👇 Tap a button (Breakfast, Lunch, Dinner, Other), then type the amount.\n\n"
    "Or type it in one line:\n"
    "  pay 20000៛ breakfast\n"
    "  pay $2.5 coffee\n\n"
    "A full report is sent automatically on the last day of each month."
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, reply_markup=KEYBOARD)


async def save_and_confirm(update, amount, currency, note):
    msg = update.message
    if amount <= 0:
        await msg.reply_text("Amount must be more than 0.", reply_markup=KEYBOARD)
        return
    user = msg.from_user
    add_payment(msg.chat_id, user.id, display_name(user), amount, currency, note)

    month = datetime.now(TZ).strftime("%Y-%m")
    total = defaultdict(float)
    for r in month_rows(msg.chat_id, month, user.id):
        total[r["currency"]] += r["amount"]

    await msg.reply_text(
        f"✅ Saved {money(amount, currency)} – {note}\n"
        f"Your total this month: {totals_text(total)}",
        reply_markup=KEYBOARD,
    )


def pending_key(chat_id):
    return f"pending:{chat_id}"


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text:
        return
    text = msg.text.strip()
    key = pending_key(msg.chat_id)

    # --- button taps ---
    if text in CATEGORY_BUTTONS:
        category = CATEGORY_BUTTONS[text]
        context.user_data[key] = (category, datetime.now(TZ))
        example = "15000 taxi" if category == "other" else "20000"
        await msg.reply_text(
            f"{text} – how much? Type the amount, e.g. {example}\n(add $ for dollars: $2.5)",
            reply_markup=ForceReply(selective=True, input_field_placeholder=example),
        )
        return
    if text == BTN_REPORT:
        context.user_data.pop(key, None)
        return await report_cmd(update, context)
    if text == BTN_MINE:
        context.user_data.pop(key, None)
        return await mine_cmd(update, context)
    if text == BTN_UNDO:
        context.user_data.pop(key, None)
        return await undo_cmd(update, context)

    # --- one-line typing: "pay 20000៛ breakfast" ---
    parsed = parse_payment(text)
    if parsed is not None:
        context.user_data.pop(key, None)
        amount, currency, note = parsed
        if amount is None:
            await msg.reply_text(
                "❓ I couldn't find the amount. Example: pay 20000៛ breakfast",
                reply_markup=KEYBOARD,
            )
            return
        return await save_and_confirm(update, amount, currency, note)

    # --- amount typed after tapping a button ---
    pending = context.user_data.get(key)
    if pending:
        category, since = pending
        if datetime.now(TZ) - since > timedelta(minutes=PENDING_MINUTES):
            context.user_data.pop(key, None)
            return  # too old, treat as normal chat
        amount, currency, comment = parse_amount_and_comment(text)
        if amount is None:
            await msg.reply_text(
                "Please type a number, e.g. 20000 (or tap another button).",
                reply_markup=ForceReply(selective=True, input_field_placeholder="20000"),
            )
            return
        context.user_data.pop(key, None)
        # for Other, use the word they typed ("15000 taxi" -> taxi)
        note = comment if category == "other" else category
        return await save_and_confirm(update, amount, currency, note)

    # anything else = normal chat, ignore


async def report_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    month = datetime.now(TZ).strftime("%Y-%m")
    if context.args:
        try:
            month = datetime.strptime(context.args[0], "%Y-%m").strftime("%Y-%m")
        except ValueError:
            await update.message.reply_text("Use the format: /report 2026-09")
            return
    chat = update.effective_chat
    await update.message.reply_text(
        build_report(chat.id, month, chat.title),
        parse_mode=ParseMode.HTML,
        reply_markup=KEYBOARD,
    )


async def mine_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    month = datetime.now(TZ).strftime("%Y-%m")
    await update.message.reply_text(
        build_user_detail(update.effective_chat.id, update.effective_user.id, month),
        parse_mode=ParseMode.HTML,
        reply_markup=KEYBOARD,
    )


async def undo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    row = delete_last(update.effective_chat.id, update.effective_user.id)
    if row:
        text = f"↩️ Deleted {money(row['amount'], row['currency'])} – {row['note'] or NO_COMMENT}"
    else:
        text = "You have no payments to delete."
    await update.message.reply_text(text, reply_markup=KEYBOARD)


async def on_migrate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if msg and msg.migrate_to_chat_id:
        migrate_chat(msg.chat_id, msg.migrate_to_chat_id)
        log.info("Group %s upgraded to %s: history moved", msg.chat_id, msg.migrate_to_chat_id)


# ---------------- Month-end report ----------------
def month_to_report(now):
    """Which month's report is due right now?
    - last day of the month, 23:00 or later -> this month
    - 1st of the month, before 12:00        -> last month (catch-up if 23:59 was missed)
    - any other time                        -> None"""
    if (now + timedelta(days=1)).day == 1 and now.hour >= 23:
        return now.strftime("%Y-%m")
    if now.day == 1 and now.hour < 12:
        return (now - timedelta(days=1)).strftime("%Y-%m")
    return None


async def send_month_end_reports(bot, now=None):
    """Send the due month-end report to every group once. Safe to call many times."""
    now = now or datetime.now(TZ)
    month = month_to_report(now)
    if month is None:
        return f"Nothing due now ({now:%Y-%m-%d %H:%M}). Reports go out on the last day from 23:00."
    sent = skipped = failed = 0
    for chat_id in chats_with_payments(month):
        if report_already_sent(chat_id, month):
            skipped += 1
            continue
        try:
            title = None
            try:
                title = (await bot.get_chat(chat_id)).title
            except Exception:
                pass
            await bot.send_message(chat_id, build_report(chat_id, month, title), parse_mode=ParseMode.HTML)
            mark_report_sent(chat_id, month)
            sent += 1
        except Exception as e:  # bot removed from group, etc.
            failed += 1
            log.warning("Could not send report to %s: %s", chat_id, e)
    return f"{month}: sent {sent}, already sent {skipped}, failed {failed}"


async def month_end_job(context: ContextTypes.DEFAULT_TYPE):
    log.info(await send_month_end_reports(context.bot))


# ---------------- Building the bot ----------------
def build_application(token, request=None):
    builder = Application.builder().token(token)
    if request is not None:  # used by tests
        builder = builder.request(request).get_updates_request(request)
    else:
        builder = (
            builder
            # slow / unstable internet: wait longer before giving up
            .connect_timeout(30).read_timeout(30).write_timeout(30).pool_timeout(30)
            .get_updates_connect_timeout(30).get_updates_read_timeout(60)
        )
        proxy = os.getenv("PROXY")  # e.g. http://127.0.0.1:7890 or socks5://127.0.0.1:1080
        if proxy:
            builder = builder.proxy(proxy).get_updates_proxy(proxy)
            log.info("Using proxy %s", proxy)
    app = builder.build()

    # Only react to NEW messages. Edited messages are ignored, otherwise
    # editing an old "/report" would crash and editing "pay ..." would double-save.
    new_msg = filters.UpdateType.MESSAGE
    app.add_handler(MessageHandler(filters.StatusUpdate.MIGRATE, on_migrate))
    app.add_handler(CommandHandler(["start", "help"], start, filters=new_msg))
    app.add_handler(CommandHandler("report", report_cmd, filters=new_msg))
    app.add_handler(CommandHandler("mine", mine_cmd, filters=new_msg))
    app.add_handler(CommandHandler("undo", undo_cmd, filters=new_msg))
    app.add_handler(MessageHandler(new_msg & filters.TEXT & ~filters.COMMAND, on_message))

    # Daily check at 23:59 while the bot is awake. Online, cron-job.org also
    # calls /cron/month-end, so the report goes out even if the host was asleep.
    app.job_queue.run_daily(month_end_job, time=time(23, 59, tzinfo=TZ))
    return app


def webhook_secret(token):
    """Secret Telegram sends with every update, so strangers can't post fake ones."""
    return hashlib.sha256(("webhook:" + token).encode()).hexdigest()[:48]


def build_web(app, token):
    """Small web server: Telegram posts messages to /telegram; cron-job.org calls the others."""
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse, Response
    from starlette.routing import Route

    secret = webhook_secret(token)

    async def telegram(request):
        if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != secret:
            return Response(status_code=403)
        await app.update_queue.put(Update.de_json(await request.json(), app.bot))
        return Response()

    async def health(request):
        return PlainTextResponse("Payment bot is running")

    async def cron(request):
        if not CRON_SECRET or request.query_params.get("key") != CRON_SECRET:
            return PlainTextResponse("Forbidden", status_code=403)
        return PlainTextResponse(await send_month_end_reports(app.bot))

    return Starlette(routes=[
        Route("/", health, methods=["GET", "HEAD"]),
        Route("/telegram", telegram, methods=["POST"]),
        Route("/cron/month-end", cron, methods=["GET", "POST"]),
    ])


async def run_webhook(app, token, base_url, port):
    import uvicorn
    server = uvicorn.Server(uvicorn.Config(build_web(app, token), host="0.0.0.0", port=port, log_level="warning"))
    async with app:  # initialize / shutdown
        await app.bot.set_webhook(
            url=f"{base_url}/telegram",
            allowed_updates=Update.ALL_TYPES,
            secret_token=webhook_secret(token),
        )
        await app.start()
        log.info("Bot started (online mode): %s/telegram", base_url)
        await server.serve()  # runs until the host stops the app
        await app.stop()


# ---------------- Main ----------------
def main():
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("Please set the BOT_TOKEN environment variable.")
    init_db()
    app = build_application(token)

    if WEBHOOK_URL:
        if not CRON_SECRET:
            log.warning("CRON_SECRET is not set: the /cron/month-end address is switched off.")
        asyncio.run(run_webhook(app, token, WEBHOOK_URL, PORT))
    else:
        log.info("Bot started (computer mode).")
        # bootstrap_retries=-1: keep retrying at startup instead of crashing
        app.run_polling(allowed_updates=Update.ALL_TYPES, bootstrap_retries=-1, drop_pending_updates=False)


if __name__ == "__main__":
    main()

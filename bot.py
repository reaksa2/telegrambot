"""
Telegram Daily Payment Bot
--------------------------
Two ways to add a payment:

  1) Buttons:  tap 🍳 Breakfast / 🍜 Lunch / 🍲 Dinner / 📝 Other (or a
               category the group added), then type the amount (e.g. 20000 or $2.5).
               For Other you can add a word: "15000 taxi".

  2) Typing:   pay 20000៛ breakfast   |  payment 15000 dinner  |  pay $2.5 coffee

  Forgot one? Add a date: "pay 20000 lunch yesterday", "pay 15000 taxi 25/10"
  (also works after a button tap: "20000 yesterday").

On the last day of every month (23:59 local time) the bot sends a report to
every chat: totals per person and per comment.

Commands (most are also on the buttons):
  /start /help      - show buttons + how to use
  /today            - today's payments
  /week             - this week's payments (Monday to today)
  /report           - this month's report so far
  /report 2026-09   - report for a specific month
  /mine             - your own payments this month
  /edit             - change or delete one of your recent payments
  /undo             - delete your last payment
  /budget 1500000   - monthly budget for this chat (/budget off, /budget = status)
  /remind 21:00     - evening reminder if nothing was recorded today (/remind off)
  /addcat ☕ Coffee  - add your own category button
  /delcat Coffee    - remove it

Groups:
  Every Telegram group is its own separate book. Family A's group only sees
  Family A's payments; Family B's group only sees Family B's. A private chat
  with the bot is a personal book. The same person can be in several groups;
  their payments are kept apart per group. Budget, reminder and custom
  categories are also per group.

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
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from telegram import (
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------- Settings (from environment variables) ----------------
TZ = ZoneInfo(os.getenv("BOT_TZ", "Asia/Phnom_Penh"))
DEFAULT_CURRENCY = os.getenv("DEFAULT_CURRENCY", "KHR").upper()  # KHR or USD
KHR_PER_USD = float(os.getenv("KHR_PER_USD", "4000"))  # used for budgets and sorting
DB_PATH = os.getenv("DB_PATH", "payments.db")
DATABASE_URL = os.getenv("DATABASE_URL")  # postgres://... -> use PostgreSQL
NO_COMMENT = "other"
PENDING_MINUTES = 10  # how long the bot waits for an amount after a button tap
DEFAULT_REMIND_AT = "21:00"
EDIT_LIST_SIZE = 10  # how many recent payments /edit shows
MAX_CATEGORIES = 12  # custom category buttons per chat

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
BTN_TODAY = "📅 Today"
BTN_WEEK = "📆 Week"
BTN_REPORT = "📊 Report"
BTN_MINE = "🧾 My payments"
BTN_EDIT = "✏️ Edit"
BTN_UNDO = "↩️ Undo"
MENU_BUTTONS = {BTN_TODAY, BTN_WEEK, BTN_REPORT, BTN_MINE, BTN_EDIT, BTN_UNDO}


def keyboard(chat_id):
    """Reply keyboard for one chat: built-in categories, the chat's own ones, then the menu."""
    rows = [["🍳 Breakfast", "🍜 Lunch"], ["🍲 Dinner", "📝 Other"]]
    custom = [c["label"] for c in chat_categories(chat_id)]
    rows += [custom[i:i + 3] for i in range(0, len(custom), 3)]
    rows += [[BTN_TODAY, BTN_WEEK, BTN_REPORT], [BTN_MINE, BTN_EDIT, BTN_UNDO]]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True)


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

# Dates for payments added later: "yesterday", "25/10", "25/10/2026", "2026-10-25".
# ("." is not allowed as a separator, otherwise "2.5" would be a date.)
DATE_WORDS = {"today": 0, "ថ្ងៃនេះ": 0, "yesterday": 1, "ម្សិលមិញ": 1, "ម្សិលម៉ិញ": 1}
DMY_RE = re.compile(r"^(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2}|\d{4}))?$")
ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")

# Khmer digits -> normal digits, so "២០០០០៛" works too
KHMER_DIGITS = str.maketrans("០១២៣៤៥៦៧៨៩", "0123456789")

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("payment-bot")


def today():
    return datetime.now(TZ).date()


def parse_date(tok, ref):
    """'yesterday' / '25/10' / '2026-10-25' -> date, otherwise None. ref = today's date."""
    if tok.lower() in DATE_WORDS:
        return ref - timedelta(days=DATE_WORDS[tok.lower()])
    try:
        m = ISO_DATE_RE.match(tok)
        if m:
            return date(int(m[1]), int(m[2]), int(m[3]))
        m = DMY_RE.match(tok)
        if m:
            if m[3]:
                year = int(m[3]) + (2000 if len(m[3]) == 2 else 0)
                return date(year, int(m[2]), int(m[1]))
            d = date(ref.year, int(m[2]), int(m[1]))
            # "30/12" typed on 2 January means last December
            return d if d <= ref else date(ref.year - 1, d.month, d.day)
    except ValueError:  # 31/02 and the like
        return None
    return None


def parse_amount_and_comment(text):
    """'20000៛ breakfast yesterday' -> (20000.0, 'KHR', 'breakfast', date(...)).
    comment is '' when none was typed; day is None when no date was typed.
    No amount -> (None, None, None, None)."""
    tokens = text.translate(KHMER_DIGITS).split()
    used = set()

    day = None
    for i, tok in enumerate(tokens):
        d = parse_date(tok, today())
        if d is not None:
            day = d
            used.add(i)
            break

    amount, currency = None, None
    for i, tok in enumerate(tokens):
        if i in used:
            continue
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
        return (None, None, None, None)
    comment = " ".join(t for j, t in enumerate(tokens) if j not in used).strip().lower()
    return amount, currency, comment, day


def parse_payment(text):
    """'pay 20000៛ breakfast' -> (amount, currency, comment, day).
    None if the text doesn't start with pay/payment."""
    m = KEYWORD_RE.match(text)
    if not m:
        return None
    amount, currency, comment, day = parse_amount_and_comment(m.group(1))
    return amount, currency, comment or NO_COMMENT, day


def parse_hhmm(text):
    """'21:00' / '9:30' / '21' -> '21:00', or None."""
    m = re.match(r"^(\d{1,2})(?::(\d{2}))?$", text.translate(KHMER_DIGITS))
    if not m or int(m[1]) > 23 or int(m[2] or 0) > 59:
        return None
    return f"{int(m[1]):02d}:{int(m[2] or 0):02d}"


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
    run("CREATE INDEX IF NOT EXISTS idx_chat_created ON payments(chat_id, created_at)")
    # remembers which month-end reports were already sent, so none is sent twice
    run(
        "CREATE TABLE IF NOT EXISTS report_log ("
        " chat_id BIGINT NOT NULL, month TEXT NOT NULL, sent_at TEXT NOT NULL,"
        " PRIMARY KEY (chat_id, month))"
    )
    # one row per chat: monthly budget and evening reminder
    run(
        "CREATE TABLE IF NOT EXISTS chat_settings ("
        " chat_id BIGINT PRIMARY KEY, budget DOUBLE PRECISION, budget_currency TEXT,"
        " remind_at TEXT, last_reminded TEXT)"
    )
    # extra category buttons a chat added with /addcat
    run(
        "CREATE TABLE IF NOT EXISTS categories ("
        " chat_id BIGINT NOT NULL, label TEXT NOT NULL, note TEXT NOT NULL,"
        " created_at TEXT NOT NULL, PRIMARY KEY (chat_id, label))"
    )


def payment_time(day=None):
    """When a payment happened: now, or the same clock time on an earlier day."""
    now = datetime.now(TZ).replace(microsecond=0)
    if day is None or day == now.date():
        return now
    return datetime.combine(day, now.time(), tzinfo=TZ)


def add_payment(chat_id, user_id, user_name, amount, currency, note, when):
    run(
        "INSERT INTO payments (chat_id, user_id, user_name, amount, currency, note, month, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (chat_id, user_id, user_name, amount, currency, note,
         when.strftime("%Y-%m"), when.isoformat(timespec="seconds")),
    )


def month_rows(chat_id, month, user_id=None):
    sql = "SELECT * FROM payments WHERE chat_id=? AND month=?"
    args = [chat_id, month]
    if user_id is not None:
        sql += " AND user_id=?"
        args.append(user_id)
    return run(sql + " ORDER BY created_at, id", args, fetch="all")


def rows_between(chat_id, start, end):
    """Payments from day `start` up to (not including) day `end`."""
    return run(
        "SELECT * FROM payments WHERE chat_id=? AND created_at>=? AND created_at<? "
        "ORDER BY created_at, id",
        (chat_id, start.isoformat(), end.isoformat()),
        fetch="all",
    )


def recent_payments(chat_id, user_id, limit=EDIT_LIST_SIZE):
    return run(
        "SELECT * FROM payments WHERE chat_id=? AND user_id=? ORDER BY id DESC LIMIT ?",
        (chat_id, user_id, limit),
        fetch="all",
    )


def own_payment(payment_id, chat_id, user_id):
    """The payment, but only if it belongs to this person in this chat."""
    return run(
        "SELECT * FROM payments WHERE id=? AND chat_id=? AND user_id=?",
        (payment_id, chat_id, user_id),
        fetch="one",
    )


def update_payment(payment_id, amount, currency, note, when):
    run(
        "UPDATE payments SET amount=?, currency=?, note=?, month=?, created_at=? WHERE id=?",
        (amount, currency, note, when.strftime("%Y-%m"), when.isoformat(timespec="seconds"), payment_id),
    )


def delete_payment(payment_id, chat_id, user_id):
    return run(
        "DELETE FROM payments WHERE id=? AND chat_id=? AND user_id=? RETURNING amount, currency, note",
        (payment_id, chat_id, user_id),
        fetch="one",
    )


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
    for table in ("payments", "report_log", "chat_settings", "categories"):
        run(f"UPDATE {table} SET chat_id=? WHERE chat_id=?", (new_chat_id, old_chat_id))


def report_already_sent(chat_id, month):
    return run("SELECT 1 AS x FROM report_log WHERE chat_id=? AND month=?",
               (chat_id, month), fetch="one") is not None


def mark_report_sent(chat_id, month):
    run("INSERT INTO report_log (chat_id, month, sent_at) VALUES (?, ?, ?)",
        (chat_id, month, datetime.now(TZ).isoformat(timespec="seconds")))


def chats_with_payments(month):
    rows = run("SELECT DISTINCT chat_id FROM payments WHERE month=?", (month,), fetch="all")
    return [r["chat_id"] for r in rows]


SETTING_FIELDS = {"budget", "budget_currency", "remind_at", "last_reminded"}


def get_settings(chat_id):
    return run("SELECT * FROM chat_settings WHERE chat_id=?", (chat_id,), fetch="one")


def save_settings(chat_id, **fields):
    cols = list(fields)
    assert set(cols) <= SETTING_FIELDS
    run(
        f"INSERT INTO chat_settings (chat_id, {', '.join(cols)}) VALUES (?{', ?' * len(cols)}) "
        f"ON CONFLICT (chat_id) DO UPDATE SET " + ", ".join(f"{c}=excluded.{c}" for c in cols),
        (chat_id, *fields.values()),
    )


def chats_to_remind(day):
    return run(
        "SELECT chat_id, remind_at FROM chat_settings WHERE remind_at IS NOT NULL "
        "AND (last_reminded IS NULL OR last_reminded<>?)",
        (day.isoformat(),),
        fetch="all",
    )


def chat_categories(chat_id):
    return run("SELECT label, note FROM categories WHERE chat_id=? ORDER BY created_at",
               (chat_id,), fetch="all")


def add_category(chat_id, label, note):
    run("INSERT INTO categories (chat_id, label, note, created_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT (chat_id, label) DO NOTHING",
        (chat_id, label, note, datetime.now(TZ).isoformat(timespec="seconds")))


def delete_category(chat_id, name):
    return run("DELETE FROM categories WHERE chat_id=? AND (label=? OR note=?) RETURNING label",
               (chat_id, name, name.lower()), fetch="one")


# ---------------- Formatting ----------------
def money(amount, currency):
    if currency == "USD":
        return f"${amount:,.2f}"
    return f"{amount:,.0f}៛"


def totals_text(totals):
    """{'KHR': 45000, 'USD': 2.5} -> '45,000៛ + $2.50'"""
    parts = [money(totals[c], c) for c in ("KHR", "USD") if totals.get(c)]
    return " + ".join(parts) if parts else money(0, DEFAULT_CURRENCY)


def convert(totals, currency):
    """{'KHR': 40000, 'USD': 5} -> one number in `currency`, using KHR_PER_USD."""
    khr = totals.get("KHR", 0) + totals.get("USD", 0) * KHR_PER_USD
    return khr if currency == "KHR" else khr / KHR_PER_USD


def month_label(month):
    return datetime.strptime(month, "%Y-%m").strftime("%B %Y")


def short_day(created_at):
    return datetime.fromisoformat(created_at).strftime("%d %b")


def summarize(rows, key):
    """Group rows by key -> sorted [(group, {'n': count, 'totals': {cur: sum}})]."""
    groups = defaultdict(lambda: {"n": 0, "totals": defaultdict(float)})
    for r in rows:
        g = groups[key(r)]
        g["n"] += 1
        g["totals"][r["currency"]] += r["amount"]
    return sorted(groups.items(), key=lambda kv: -convert(kv[1]["totals"], "KHR"))  # biggest first


def summary_lines(rows):
    """'By person', 'By comment' and the grand total for a list of payments."""
    names = {r["user_id"]: r["user_name"] for r in rows}  # latest name wins
    lines = ["👤 <b>By person</b>"]
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
    return lines


def chat_heading(title, chat_title):
    if chat_title:
        title += f"\n👨‍👩‍👧 {html.escape(chat_title)}"
    return title


def build_report(chat_id, month, chat_title=None):
    rows = month_rows(chat_id, month)
    title = chat_heading(f"📊 <b>Payment report – {month_label(month)}</b>", chat_title)
    if not rows:
        return f"{title}\n\nNo payments recorded."
    lines = [title, ""] + summary_lines(rows)
    if month == datetime.now(TZ).strftime("%Y-%m"):
        budget = budget_text(chat_id)
        if budget:
            lines += ["", budget]
    return "\n".join(lines)


def build_today_report(chat_id, chat_title=None):
    day = today()
    rows = rows_between(chat_id, day, day + timedelta(days=1))
    title = chat_heading(f"📅 <b>Today – {day:%a %d %b %Y}</b>", chat_title)
    if not rows:
        return f"{title}\n\nNo payments recorded today."
    lines = [title, "", "🧾 <b>Payments</b>"]
    for r in rows:
        lines.append(
            f"{datetime.fromisoformat(r['created_at']):%H:%M} {html.escape(r['user_name'])}: "
            f"{money(r['amount'], r['currency'])} – {html.escape(r['note'] or NO_COMMENT)}"
        )
    return "\n".join(lines + [""] + summary_lines(rows))


def build_week_report(chat_id, chat_title=None):
    end = today()
    start = end - timedelta(days=end.weekday())  # Monday
    rows = rows_between(chat_id, start, end + timedelta(days=1))
    title = chat_heading(f"📆 <b>This week – {start:%d %b} to {end:%d %b}</b>", chat_title)
    if not rows:
        return f"{title}\n\nNo payments recorded this week."
    per_day = defaultdict(lambda: defaultdict(float))
    for r in rows:
        per_day[r["created_at"][:10]][r["currency"]] += r["amount"]
    lines = [title, "", "📅 <b>By day</b>"]
    d = start
    while d <= end:
        totals = per_day.get(d.isoformat())
        lines.append(f"{d:%a %d %b}: {totals_text(totals) if totals else '–'}")
        d += timedelta(days=1)
    return "\n".join(lines + [""] + summary_lines(rows))


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


def mention(user):
    """Clickable name; also makes a ForceReply reach this person in a group."""
    return f'<a href="tg://user?id={user.id}">{html.escape(user.first_name or display_name(user))}</a>'


# ---------------- Budget ----------------
def budget_status(chat_id, month=None):
    """(spent, budget, currency) for the month, or None when no budget is set."""
    s = get_settings(chat_id)
    if not s or not s["budget"]:
        return None
    month = month or datetime.now(TZ).strftime("%Y-%m")
    totals = defaultdict(float)
    for r in month_rows(chat_id, month):
        totals[r["currency"]] += r["amount"]
    return convert(totals, s["budget_currency"]), s["budget"], s["budget_currency"]


def budget_text(chat_id):
    status = budget_status(chat_id)
    if status is None:
        return None
    spent, budget, cur = status
    pct = spent / budget * 100
    filled = min(10, round(pct / 10))
    bar = "▓" * filled + "░" * (10 - filled)
    rest = f"{money(budget - spent, cur)} left" if spent <= budget else f"over by {money(spent - budget, cur)}"
    return f"🎯 <b>Budget:</b> {money(spent, cur)} / {money(budget, cur)} ({pct:.0f}%)\n{bar} {rest}"


def budget_alert(chat_id, amount, currency, month):
    """Warning text when this payment pushed the month past 80% or 100% of the budget."""
    if month != datetime.now(TZ).strftime("%Y-%m"):
        return None
    status = budget_status(chat_id, month)
    if status is None:
        return None
    after, budget, cur = status
    before = after - convert({currency: amount}, cur)
    for share, text in ((1.0, "🚨 <b>Over budget!</b>"), (0.8, "⚠️ <b>80% of this month's budget is used.</b>")):
        if before < share * budget <= after:
            return f"{text}\n{budget_text(chat_id)}"
    return None


# ---------------- Handlers ----------------
HELP_TEXT = (
    "👋 Hi! I record daily payments.\n\n"
    "👇 Tap a button (Breakfast, Lunch, Dinner, Other), then type the amount.\n\n"
    "Or type it in one line:\n"
    "  pay 20000៛ breakfast\n"
    "  pay $2.5 coffee\n\n"
    "Forgot one? Add the date:\n"
    "  pay 20000 lunch yesterday\n"
    "  pay 15000 taxi 25/10\n\n"
    "More:\n"
    "  /today, /week, /report – totals\n"
    "  /edit – change or delete a payment\n"
    "  /budget 1500000 – monthly budget\n"
    "  /remind 21:00 – evening reminder\n"
    "  /addcat ☕ Coffee – your own button\n\n"
    "A full report is sent automatically on the last day of each month."
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, reply_markup=keyboard(update.effective_chat.id))


async def save_and_confirm(update, amount, currency, note, day=None):
    msg = update.message
    kb = keyboard(msg.chat_id)
    if amount <= 0:
        await msg.reply_text("Amount must be more than 0.", reply_markup=kb)
        return
    if day is not None and day > today():
        await msg.reply_text("The date can't be in the future.", reply_markup=kb)
        return
    user = msg.from_user
    when = payment_time(day)
    add_payment(msg.chat_id, user.id, display_name(user), amount, currency, note, when)

    month = when.strftime("%Y-%m")
    total = defaultdict(float)
    for r in month_rows(msg.chat_id, month, user.id):
        total[r["currency"]] += r["amount"]

    dated = "" if when.date() == today() else f" ({when:%d %b})"
    period = "this month" if month == datetime.now(TZ).strftime("%Y-%m") else f"in {month_label(month)}"
    text = (
        f"✅ Saved {money(amount, currency)} – {html.escape(note)}{dated}\n"
        f"Your total {period}: {totals_text(total)}"
    )
    alert = budget_alert(msg.chat_id, amount, currency, month)
    if alert:
        text += f"\n\n{alert}"
    await msg.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


def pending_key(chat_id):
    return f"pending:{chat_id}"


def edit_key(chat_id):
    return f"edit:{chat_id}"


def clear_pending(context, chat_id):
    context.user_data.pop(pending_key(chat_id), None)
    context.user_data.pop(edit_key(chat_id), None)


def custom_category(chat_id, text):
    for c in chat_categories(chat_id):
        if c["label"] == text:
            return c["note"]
    return None


def is_fresh(since):
    return datetime.now(TZ) - since <= timedelta(minutes=PENDING_MINUTES)


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text:
        return
    text = msg.text.strip()
    chat_id = msg.chat_id

    # --- menu buttons ---
    menu = {
        BTN_TODAY: today_cmd, BTN_WEEK: week_cmd, BTN_REPORT: report_cmd,
        BTN_MINE: mine_cmd, BTN_EDIT: edit_cmd, BTN_UNDO: undo_cmd,
    }
    if text in menu:
        clear_pending(context, chat_id)
        return await menu[text](update, context)

    # --- category buttons (built-in or added with /addcat) ---
    category = CATEGORY_BUTTONS.get(text) or custom_category(chat_id, text)
    if category:
        clear_pending(context, chat_id)
        context.user_data[pending_key(chat_id)] = (category, datetime.now(TZ))
        example = "15000 taxi" if category == "other" else "20000"
        await msg.reply_text(
            f"{text} – how much? Type the amount, e.g. {example}\n"
            "(add $ for dollars: $2.5; add yesterday or 25/10 for an earlier day)",
            reply_markup=ForceReply(selective=True, input_field_placeholder=example),
        )
        return

    # --- one-line typing: "pay 20000៛ breakfast" ---
    parsed = parse_payment(text)
    if parsed is not None:
        clear_pending(context, chat_id)
        amount, currency, note, day = parsed
        if amount is None:
            await msg.reply_text(
                "❓ I couldn't find the amount. Example: pay 20000៛ breakfast",
                reply_markup=keyboard(chat_id),
            )
            return
        return await save_and_confirm(update, amount, currency, note, day)

    # --- new amount typed after tapping ✏️ in /edit ---
    editing = context.user_data.get(edit_key(chat_id))
    if editing:
        payment_id, since = editing
        if not is_fresh(since):
            context.user_data.pop(edit_key(chat_id), None)
            return  # too old, treat as normal chat
        amount, currency, comment, day = parse_amount_and_comment(text)
        if amount is None:
            await msg.reply_text(
                "Please type the new amount, e.g. 25000 (or 25000 lunch yesterday).",
                reply_markup=ForceReply(selective=True, input_field_placeholder="25000"),
            )
            return
        context.user_data.pop(edit_key(chat_id), None)
        return await apply_edit(update, payment_id, amount, currency, comment, day)

    # --- amount typed after tapping a category button ---
    pending = context.user_data.get(pending_key(chat_id))
    if pending:
        category, since = pending
        if not is_fresh(since):
            context.user_data.pop(pending_key(chat_id), None)
            return  # too old, treat as normal chat
        amount, currency, comment, day = parse_amount_and_comment(text)
        if amount is None:
            await msg.reply_text(
                "Please type a number, e.g. 20000 (or tap another button).",
                reply_markup=ForceReply(selective=True, input_field_placeholder="20000"),
            )
            return
        context.user_data.pop(pending_key(chat_id), None)
        # for Other, use the word they typed ("15000 taxi" -> taxi)
        note = (comment or NO_COMMENT) if category == "other" else category
        return await save_and_confirm(update, amount, currency, note, day)

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
        reply_markup=keyboard(chat.id),
    )


async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    await update.message.reply_text(
        build_today_report(chat.id, chat.title), parse_mode=ParseMode.HTML, reply_markup=keyboard(chat.id)
    )


async def week_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    await update.message.reply_text(
        build_week_report(chat.id, chat.title), parse_mode=ParseMode.HTML, reply_markup=keyboard(chat.id)
    )


async def mine_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    month = datetime.now(TZ).strftime("%Y-%m")
    chat_id = update.effective_chat.id
    await update.message.reply_text(
        build_user_detail(chat_id, update.effective_user.id, month),
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard(chat_id),
    )


async def undo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    row = delete_last(chat_id, update.effective_user.id)
    if row:
        text = f"↩️ Deleted {money(row['amount'], row['currency'])} – {row['note'] or NO_COMMENT}"
    else:
        text = "You have no payments to delete."
    await update.message.reply_text(text, reply_markup=keyboard(chat_id))


# ---------------- Edit / delete one payment ----------------
def edit_list(chat_id, user):
    """Text + inline buttons: one row per recent payment, ✏️ to change, 🗑 to delete."""
    rows = recent_payments(chat_id, user.id)
    if not rows:
        return f"{mention(user)}, you have no payments to edit.", None
    buttons = []
    for r in rows:
        label = f"✏️ {short_day(r['created_at'])} · {money(r['amount'], r['currency'])} · {r['note'] or NO_COMMENT}"
        buttons.append([
            InlineKeyboardButton(label[:60], callback_data=f"e:{r['id']}"),
            InlineKeyboardButton("🗑", callback_data=f"d:{r['id']}"),
        ])
    text = f"{mention(user)}, your last {len(rows)} payments.\nTap ✏️ to change one or 🗑 to delete it."
    return text, InlineKeyboardMarkup(buttons)


async def edit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text, markup = edit_list(update.effective_chat.id, update.effective_user)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


async def on_edit_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    action, _, pid = query.data.partition(":")
    chat_id, user = query.message.chat.id, query.from_user
    row = own_payment(int(pid), chat_id, user.id)
    if row is None:
        # someone else's list in a group, or the payment is already gone
        await query.answer("You can only change your own payments.", show_alert=True)
        return
    what = f"{money(row['amount'], row['currency'])} – {html.escape(row['note'] or NO_COMMENT)} ({short_day(row['created_at'])})"

    if action == "e":  # change: ask for the new amount
        await query.answer()
        clear_pending(context, chat_id)
        context.user_data[edit_key(chat_id)] = (row["id"], datetime.now(TZ))
        await query.message.reply_text(
            f"✏️ {mention(user)}, type the new amount for {what}.\n"
            "You can also change the comment or the day: 25000 lunch yesterday",
            parse_mode=ParseMode.HTML,
            reply_markup=ForceReply(selective=True, input_field_placeholder="25000"),
        )
    elif action == "d":  # delete: ask to confirm first
        await query.answer()
        await query.edit_message_text(
            f"🗑 Delete {what}?",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Delete", callback_data=f"x:{row['id']}"),
                InlineKeyboardButton("✖️ Cancel", callback_data=f"c:{row['id']}"),
            ]]),
        )
    elif action == "x":  # delete confirmed
        delete_payment(row["id"], chat_id, user.id)
        await query.answer("Deleted")
        text, markup = edit_list(chat_id, user)
        await query.edit_message_text(f"🗑 Deleted {what}\n\n{text}", parse_mode=ParseMode.HTML, reply_markup=markup)
    elif action == "c":  # cancel: show the list again
        await query.answer()
        text, markup = edit_list(chat_id, user)
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


async def apply_edit(update, payment_id, amount, currency, comment, day):
    msg = update.message
    kb = keyboard(msg.chat_id)
    row = own_payment(payment_id, msg.chat_id, msg.from_user.id)
    if row is None:
        await msg.reply_text("That payment was already deleted.", reply_markup=kb)
        return
    if amount <= 0:
        await msg.reply_text("Amount must be more than 0.", reply_markup=kb)
        return
    if day is not None and day > today():
        await msg.reply_text("The date can't be in the future.", reply_markup=kb)
        return
    old_when = datetime.fromisoformat(row["created_at"])
    when = old_when if day is None else datetime.combine(day, old_when.timetz())
    note = comment or row["note"] or NO_COMMENT  # no new comment typed -> keep the old one
    update_payment(payment_id, amount, currency, note, when)
    await msg.reply_text(
        f"✏️ Changed {money(row['amount'], row['currency'])} – {html.escape(row['note'] or NO_COMMENT)}"
        f" ({old_when:%d %b})\n"
        f"to {money(amount, currency)} – {html.escape(note)} ({when:%d %b})",
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )


# ---------------- Settings commands ----------------
async def budget_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    kb = keyboard(chat_id)
    arg = " ".join(context.args).strip()
    if arg.lower() in ("off", "0", "none", "stop"):
        save_settings(chat_id, budget=None, budget_currency=None)
        await update.message.reply_text("🎯 Budget removed.", reply_markup=kb)
        return
    if arg:
        amount, currency, _, _ = parse_amount_and_comment(arg)
        if not amount or amount <= 0:
            await update.message.reply_text("Use: /budget 1500000  or  /budget $400  (/budget off to remove)")
            return
        save_settings(chat_id, budget=amount, budget_currency=currency)
    text = budget_text(chat_id)
    if text is None:
        text = (
            "🎯 No monthly budget yet.\n"
            "Set one with /budget 1500000 or /budget $400.\n"
            "I'll warn the chat at 80% and when it's over."
        )
    else:
        text += f"\n<i>Riel and dollars are added up at {KHR_PER_USD:,.0f}៛ = $1.</i>"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def remind_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    kb = keyboard(chat_id)
    arg = " ".join(context.args).strip().lower()
    if arg in ("off", "stop", "no"):
        save_settings(chat_id, remind_at=None)
        await update.message.reply_text("🔕 Reminder turned off.", reply_markup=kb)
        return
    if not arg:
        s = get_settings(chat_id)
        if s and s["remind_at"]:
            text = f"🔔 Reminder is on at {s['remind_at']}. /remind off to stop."
        else:
            text = (f"🔕 Reminder is off.\n/remind {DEFAULT_REMIND_AT} – I'll remind this chat at that time "
                    "if nothing was recorded that day.")
        await update.message.reply_text(text, reply_markup=kb)
        return
    at = DEFAULT_REMIND_AT if arg == "on" else parse_hhmm(arg)
    if at is None:
        await update.message.reply_text("Use: /remind 21:00  (or /remind off)")
        return
    now = datetime.now(TZ)
    # time already passed today -> start tomorrow, don't remind right away
    last = now.date().isoformat() if now.strftime("%H:%M") >= at else None
    save_settings(chat_id, remind_at=at, last_reminded=last)
    await update.message.reply_text(
        f"🔔 OK! Every day at {at} I'll remind this chat if nothing was recorded that day.",
        reply_markup=kb,
    )


async def addcat_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    label = " ".join(context.args).strip()
    if not label:
        await update.message.reply_text("Use: /addcat ☕ Coffee")
        return
    reserved = set(CATEGORY_BUTTONS) | MENU_BUTTONS
    if (len(label) > 30 or label.startswith("/") or label in reserved
            or AMOUNT_RE.match(label.translate(KHMER_DIGITS)) or parse_payment(label) is not None):
        await update.message.reply_text("Please choose another name (short, not a number). Example: /addcat ☕ Coffee")
        return
    existing = chat_categories(chat_id)
    if any(c["label"] == label for c in existing):
        await update.message.reply_text(f"{label} is already a button.", reply_markup=keyboard(chat_id))
        return
    if len(existing) >= MAX_CATEGORIES:
        await update.message.reply_text(f"Up to {MAX_CATEGORIES} extra buttons. Remove one first with /delcat.")
        return
    add_category(chat_id, label, category_note(label))
    await update.message.reply_text(f"✅ Added the button {label}.", reply_markup=keyboard(chat_id))


async def delcat_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    name = " ".join(context.args).strip()
    if not name:
        labels = [c["label"] for c in chat_categories(chat_id)]
        text = ("Your buttons: " + ", ".join(labels) + "\nRemove one with /delcat Coffee") if labels \
            else "No extra buttons yet. Add one with /addcat ☕ Coffee"
        await update.message.reply_text(text)
        return
    row = delete_category(chat_id, name)
    text = f"🗑 Removed the button {row['label']}." if row else f"There's no button called {name}."
    await update.message.reply_text(text, reply_markup=keyboard(chat_id))


def category_note(label):
    """'☕ Coffee' -> 'coffee' (the emoji is only for the button)."""
    words = label.split()
    while len(words) > 1 and not any(ch.isalnum() for ch in words[0]):
        words.pop(0)
    return " ".join(words).lower()


async def on_migrate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if msg and msg.migrate_to_chat_id:
        migrate_chat(msg.chat_id, msg.migrate_to_chat_id)
        log.info("Group %s upgraded to %s: history moved", msg.chat_id, msg.migrate_to_chat_id)


# ---------------- Evening reminder ----------------
async def send_reminders(bot, now=None):
    """Remind every chat whose reminder time has passed today and that recorded nothing today."""
    now = now or datetime.now(TZ)
    day = now.date()
    sent = 0
    for r in chats_to_remind(day):
        if now.strftime("%H:%M") < r["remind_at"]:
            continue
        chat_id = r["chat_id"]
        if rows_between(chat_id, day, day + timedelta(days=1)):
            save_settings(chat_id, last_reminded=day.isoformat())
            continue  # already recorded something today
        try:
            await bot.send_message(
                chat_id,
                "🔔 Nothing recorded today yet. Did you pay for anything?\n"
                "Tap a button below, or type: pay 20000 lunch",
                reply_markup=keyboard(chat_id),
            )
            sent += 1
        except Exception as e:  # bot removed from group, etc.
            log.warning("Could not send reminder to %s: %s", chat_id, e)
            continue
        save_settings(chat_id, last_reminded=day.isoformat())
    return sent


async def reminder_job(context: ContextTypes.DEFAULT_TYPE):
    sent = await send_reminders(context.bot)
    if sent:
        log.info("Sent %d reminder(s)", sent)


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
    app.add_handler(CommandHandler("today", today_cmd, filters=new_msg))
    app.add_handler(CommandHandler("week", week_cmd, filters=new_msg))
    app.add_handler(CommandHandler("mine", mine_cmd, filters=new_msg))
    app.add_handler(CommandHandler("edit", edit_cmd, filters=new_msg))
    app.add_handler(CommandHandler("undo", undo_cmd, filters=new_msg))
    app.add_handler(CommandHandler("budget", budget_cmd, filters=new_msg))
    app.add_handler(CommandHandler("remind", remind_cmd, filters=new_msg))
    app.add_handler(CommandHandler("addcat", addcat_cmd, filters=new_msg))
    app.add_handler(CommandHandler("delcat", delcat_cmd, filters=new_msg))
    app.add_handler(CallbackQueryHandler(on_edit_button, pattern=r"^[edxc]:\d+$"))
    app.add_handler(MessageHandler(new_msg & filters.TEXT & ~filters.COMMAND, on_message))

    # Daily check at 23:59 while the bot is awake. Online, cron-job.org also
    # calls /cron/month-end, so the report goes out even if the host was asleep.
    app.job_queue.run_daily(month_end_job, time=time(23, 59, tzinfo=TZ))
    # Evening reminders: look every 5 minutes for chats whose time has come.
    app.job_queue.run_repeating(reminder_job, interval=300, first=30)
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

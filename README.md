# Daily Payment Telegram Bot

Record daily payments with buttons or by typing. The bot sends a monthly report on the last day of each month at 23:59.

## 1. Create the bot
1. In Telegram, open **@BotFather** → `/newbot` → copy the **token**.
2. **For groups:** in BotFather send `/setprivacy` → choose your bot → **Disable**, then remove and re-add the bot to the group. (Without this the bot can't see button taps or `pay ...` messages in groups.)

## 2. How to use
Send `/start` and the buttons appear:

```
[ 🍳 Breakfast ] [ 🍜 Lunch ]
[ 🍲 Dinner ]    [ 📝 Other ]
[ ☕ Coffee ] ...              <- buttons your group added with /addcat
[ 📅 Today ] [ 📆 Week ] [ 📊 Report ]
[ 🧾 My payments ] [ ✏️ Edit ] [ ↩️ Undo ]
```

- Tap **Breakfast / Lunch / Dinner**, then type the amount: `20000` (riel) or `$2.5` (dollars).
- Tap **Other**, then type amount + what it was: `15000 taxi`.
- Or type in one line: `pay 20000៛ breakfast`, `pay $2.5 coffee`.
- Khmer digits work too: `២០០០០៛`.
- **Forgot to record one?** Add the day: `pay 20000 lunch yesterday`, `pay 15000 taxi 25/10`,
  `pay 5000 2026-10-01`. Also works after a button: `20000 ម្សិលមិញ`.

The report shows totals **by person** and **by comment**. Riel and dollars are added up separately.

### More commands
| Command | What it does |
|---|---|
| `/today` | Everyone's payments today |
| `/week` | This week (Monday to today), with a total for each day |
| `/report`, `/report 2026-09` | Month report |
| `/mine` | Your own payments this month |
| `/edit` | Your last 10 payments with ✏️ (change) and 🗑 (delete) buttons. After ✏️, type e.g. `25000`, `25000 latte` or `25000 yesterday` |
| `/undo` | Delete your last payment |
| `/budget 1500000`, `/budget $400` | Monthly budget for this group. The bot warns at **80%** and when it's **over**. `/budget` shows progress, `/budget off` removes it |
| `/remind 21:00` | Every evening at 21:00, if nobody recorded anything that day, the bot sends a reminder. `/remind` shows it, `/remind off` stops it |
| `/addcat ☕ Coffee` | Add your own button (up to 12 per group) |
| `/delcat Coffee` | Remove it (`/delcat` lists them) |

Budget, reminder and extra buttons are separate for each group. In a group, only the person who
added a payment can change or delete it.

**Tip:** in BotFather send `/setcommands`, choose your bot, and paste this so the commands show in the menu:
```
today - Today's payments
week - This week's payments
report - This month's report
mine - My payments this month
edit - Change or delete a payment
undo - Delete my last payment
budget - Monthly budget
remind - Evening reminder
addcat - Add a category button
delcat - Remove a category button
help - How to use
```

### Groups (families)
Each Telegram group keeps its own separate records:
- **Family A's group** sees only Family A's payments, reports and month-end summary.
- **Family B's group** sees only Family B's.
- A person in both groups has separate totals in each one. `/undo` only removes their last payment in the group where they typed it.
- A private chat with the bot is a personal record for just that person.
- If Telegram upgrades a group to a "supergroup", its history is moved over automatically.

To start a new family: create a Telegram group, add the bot, then send `/start`.

## 3. Run on your computer
```bash
pip install -r requirements.txt
# Windows PowerShell
$env:BOT_TOKEN="123456:ABC..."
python bot.py
```
Without `DATABASE_URL`, data is saved in the file `payments.db` next to `bot.py`.

## 4. Put it online for free (Render + Neon + cron-job.org)
Three free services:
- **Neon** stores the data (free PostgreSQL that doesn't expire).
- **Render** runs the bot (free web service).
- **cron-job.org** keeps the bot awake and makes sure the month-end report goes out.

### Step 1 – Database (Neon)
1. Sign up at https://neon.com and create a project. Pick the **Singapore** region (closest to Cambodia).
2. Copy the **connection string**. It looks like `postgresql://user:pass@ep-xxx.ap-southeast-1.aws.neon.tech/neondb?sslmode=require`

### Step 2 – Put the code on GitHub
1. Sign up at https://github.com and click **New repository**. Name it `payment-bot`, choose **Private**, and click Create.
2. Click **uploading an existing file** and drag in: `bot.py`, `requirements.txt`, `README.md`, `.gitignore`, `.python-version`.
   (On Windows, files starting with a dot may be hidden. In File Explorer, turn on View → Show → Hidden items.)
3. Click **Commit changes**.
   ⚠️ Never put your bot token in these files. It goes in Render's settings (step 3).

### Step 3 – Run it (Render)
1. Sign up at https://render.com using your GitHub account.
2. **New → Web Service**, then pick your `payment-bot` repository.
3. Fill in:
   - Region: **Singapore**
   - Runtime: **Python 3**
   - Build command: `pip install -r requirements.txt`
   - Start command: `python bot.py`
   - Instance type: **Free**
4. Under **Environment Variables**, add:
   | Key | Value |
   |---|---|
   | `BOT_TOKEN` | your token from BotFather |
   | `DATABASE_URL` | the Neon connection string |
   | `CRON_SECRET` | any long password you invent, e.g. `my-family-2026-xk93` |
5. Click **Deploy**. When the log shows `Bot started (online mode)`, it's working.
   Your address looks like `https://payment-bot-xxxx.onrender.com`. Open it and you should see "Payment bot is running".
6. **Stop the copy running on your PC.** Only one copy of the bot can run at a time.

### Step 4 – Keep it awake + month-end report (cron-job.org)
Render's free plan puts the app to sleep after 15 minutes without visits. Sign up at https://cron-job.org and create **two** jobs:

| Job | URL | Schedule |
|---|---|---|
| Keep awake | `https://payment-bot-xxxx.onrender.com/` | every 10 minutes |
| Month-end report | `https://payment-bot-xxxx.onrender.com/cron/month-end?key=YOUR_CRON_SECRET` | every day at 23:55, time zone **Asia/Phnom_Penh** |

The month-end job runs every day, but the bot only sends reports on the last day of the month. If it's ever missed, the bot catches up on the morning of the 1st. Each family gets its report only once, even if the job runs several times.

To test it: open the month-end URL in your browser. On a normal day it says "Nothing due now". With a wrong key it says "Forbidden".

### Updating the bot later
Upload the new `bot.py` to GitHub and Render redeploys automatically. Your data is safe in Neon.

### Free-plan limits to know
- Render gives 750 free hours a month, and one always-awake app uses about 744. Run only **one** free service on that Render account.
- If the bot was asleep, the first message can take about a minute to get an answer. The keep-awake job prevents this.
- Render says the free plan isn't meant for business-critical apps. That's fine for a family bot.

## Settings
| Variable           | Default           | Meaning                                       |
|--------------------|-------------------|-----------------------------------------------|
| `BOT_TOKEN`        | (required)        | Token from BotFather                          |
| `DATABASE_URL`     | (empty = SQLite)  | PostgreSQL connection string                  |
| `DB_PATH`          | `payments.db`     | SQLite file (only when no DATABASE_URL)       |
| `BOT_TZ`           | `Asia/Phnom_Penh` | Time zone for days / month end                |
| `DEFAULT_CURRENCY` | `KHR`             | Currency when no symbol typed (`KHR`/`USD`)   |
| `KHR_PER_USD`      | `4000`            | Exchange rate used to add riel + dollars for the budget |
| `CRON_SECRET`      | (empty = off)     | Password for the /cron/month-end address      |
| `WEBHOOK_URL`      | (auto on Render)  | Public address; set only on hosts other than Render |
| `PROXY`            | (empty)           | e.g. `socks5://127.0.0.1:1080` if Telegram is blocked |

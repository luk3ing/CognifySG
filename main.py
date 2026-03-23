"""
CognifySG — Professional Tuition Agency Bot v4
Multi-admin | Production-grade | python-telegram-bot==21.6
"""

import os
import sqlite3
import random
import re
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)

# ── CONFIG ─────────────────────────────────────────────────────────────────────
TOKEN            = os.environ.get("TOKEN")
SUPER_ADMIN_ID   = int(os.environ.get("ADMIN_CHAT_ID", "0"))  # only you can add/remove admins
MAX_CAPTCHA      = 3

# ── KEEPALIVE ──────────────────────────────────────────────────────────────────
class KeepAlive(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers()
        self.wfile.write(b"CognifySG is running!")
    def log_message(self, *a): pass

threading.Thread(target=lambda: HTTPServer(("0.0.0.0", 8080), KeepAlive).serve_forever(), daemon=True).start()

# ── DATABASE ───────────────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect("cognify.db")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS admins (
            user_id    INTEGER PRIMARY KEY,
            username   TEXT,
            name       TEXT,
            added_by   INTEGER,
            added_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS blocked (
            user_id    INTEGER PRIMARY KEY,
            blocked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS captcha_attempts (
            user_id  INTEGER PRIMARY KEY,
            attempts INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS tutors (
            user_id    INTEGER PRIMARY KEY,
            username   TEXT,
            name       TEXT,
            phone      TEXT,
            subjects   TEXT,
            levels     TEXT,
            areas      TEXT,
            rate       INTEGER,
            available  INTEGER DEFAULT 1,
            approved   INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS requests (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            parent_id  INTEGER,
            username   TEXT,
            name       TEXT,
            phone      TEXT,
            subject    TEXT,
            level      TEXT,
            areas      TEXT,
            budget     INTEGER,
            status     TEXT DEFAULT 'open',
            approved   INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS applications (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            tutor_id   INTEGER,
            request_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(tutor_id, request_id)
        );
        CREATE INDEX IF NOT EXISTS idx_tutors_avail  ON tutors(available, approved);
        CREATE INDEX IF NOT EXISTS idx_req_status    ON requests(status, approved);
        CREATE INDEX IF NOT EXISTS idx_req_parent    ON requests(parent_id);
        CREATE INDEX IF NOT EXISTS idx_apps_tutor    ON applications(tutor_id);
    """)
    # Always ensure super admin is in the admins table
    conn.execute(
        "INSERT OR IGNORE INTO admins(user_id, username, name, added_by) VALUES (?,?,?,?)",
        (SUPER_ADMIN_ID, "superadmin", "Super Admin", SUPER_ADMIN_ID)
    )
    conn.commit()
    conn.close()

def db():
    conn = sqlite3.connect("cognify.db", check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn

# ── ADMIN HELPERS ──────────────────────────────────────────────────────────────
def get_all_admins():
    conn = db()
    rows = conn.execute("SELECT user_id FROM admins").fetchall()
    conn.close()
    return [r["user_id"] for r in rows]

def is_admin(user_id):
    conn = db()
    row = conn.execute("SELECT 1 FROM admins WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return row is not None

async def notify_all_admins(bot, text, reply_markup=None):
    for admin_id in get_all_admins():
        try:
            await bot.send_message(admin_id, text, reply_markup=reply_markup, parse_mode="Markdown")
        except Exception:
            pass

# ── UI HELPERS ─────────────────────────────────────────────────────────────────
DIV  = "━━━━━━━━━━━━━━━━━━━━"
DIV2 = "──────────────────────"

def header(icon, title):
    return f"{icon}  *{title}*\n{DIV}"

def field(label, value):
    return f"▸ *{label}:* {value}"

def ms_keyboard(options, selected, prefix):
    rows, row = [], []
    for opt in options:
        tick = "✅ " if opt in selected else "◻️ "
        row.append(InlineKeyboardButton(f"{tick}{opt}", callback_data=f"{prefix}|{opt}"))
        if len(row) == 3:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("Confirm Selection  ✅", callback_data=f"{prefix}|DONE")])
    return InlineKeyboardMarkup(rows)

# ── STATES ─────────────────────────────────────────────────────────────────────
(CAPTCHA,
 ROLE_SELECT,
 T_NAME, T_PHONE, T_SUBJECTS, T_LEVELS, T_AREAS, T_RATE,
 P_NAME, P_PHONE, P_SUBJECT,  P_LEVEL,  P_AREA,  P_BUDGET) = range(14)

ALL_SUBJECTS = ["Maths","English","Science","Chinese","Malay","Tamil",
                "Physics","Chemistry","Biology","History","Geography","Literature"]
ALL_LEVELS   = ["Primary 1–3","Primary 4–6","Lower Sec","Upper Sec","JC","IB/IP","Poly/ITE"]
ALL_AREAS    = ["North","South","East","West","Central","Online"]

# ── VALIDATION ─────────────────────────────────────────────────────────────────
def valid_name(t):  return bool(re.match(r"^[A-Za-z\s\-'\.]{2,50}$", t.strip()))
def valid_phone(t): return bool(re.match(r"^[89]\d{7}$", t.strip().replace(" ","")))
def valid_rate(t):
    t = t.strip().replace("$","").replace("/hr","").replace(" ","")
    return t.isdigit() and int(t) > 0
def clean_rate(t):  return int(t.strip().replace("$","").replace("/hr","").replace(" ",""))

# ── CAPTCHA ────────────────────────────────────────────────────────────────────
def gen_captcha():
    a, b  = random.randint(2, 9), random.randint(2, 9)
    ans   = a + b
    wrong = random.sample([x for x in range(2, 19) if x != ans], 3)
    opts  = wrong + [ans]
    random.shuffle(opts)
    return a, b, ans, opts

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    uid  = update.effective_user.id
    conn = db()

    if conn.execute("SELECT 1 FROM blocked WHERE user_id=?", (uid,)).fetchone():
        conn.close()
        await update.message.reply_text(
            f"{header('🚫', 'Access Denied')}\n\n"
            "Your account has been flagged and restricted.\n"
            "_Contact support if you believe this is an error._",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    conn.execute("INSERT OR IGNORE INTO captcha_attempts(user_id,attempts) VALUES(?,0)", (uid,))
    conn.commit(); conn.close()

    a, b, ans, opts = gen_captcha()
    ctx.user_data.update({"captcha_ans": ans, "captcha_a": a, "captcha_b": b})
    kb = [[InlineKeyboardButton(str(o), callback_data=f"captcha|{o}") for o in opts]]
    await update.message.reply_text(
        f"{header('🔐', 'Security Verification')}\n\n"
        "Please confirm you are human before proceeding.\n\n"
        f"{DIV2}\n❓  *What is  {a} + {b}?*\n{DIV2}\n\n"
        "_Select the correct answer:_",
        reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown"
    )
    return CAPTCHA

async def captcha_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    uid    = q.from_user.id
    chosen = int(q.data.split("|")[1])
    conn   = db()
    row    = conn.execute("SELECT attempts FROM captcha_attempts WHERE user_id=?", (uid,)).fetchone()
    attempts = (row["attempts"] if row else 0) + 1
    conn.execute("INSERT OR REPLACE INTO captcha_attempts(user_id,attempts) VALUES(?,?)", (uid, attempts))
    conn.commit()

    if chosen == ctx.user_data.get("captcha_ans"):
        conn.execute("DELETE FROM captcha_attempts WHERE user_id=?", (uid,))
        conn.commit(); conn.close()
        kb = [[
            InlineKeyboardButton("👨‍🏫  I am a Tutor",  callback_data="role_tutor"),
            InlineKeyboardButton("👨‍👩‍👧  I am a Parent", callback_data="role_parent"),
        ]]
        await q.edit_message_text(
            f"{header('🎓', 'Welcome to CognifySG')}\n\n"
            "Singapore's premier tuition matching platform.\n\n"
            f"{DIV2}\nPlease identify yourself to continue:\n{DIV2}",
            reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown"
        )
        return ROLE_SELECT

    remaining = MAX_CAPTCHA - attempts
    if remaining <= 0:
        conn.execute("INSERT OR IGNORE INTO blocked(user_id) VALUES(?)", (uid,))
        conn.commit(); conn.close()
        await q.edit_message_text(
            f"{header('🚫', 'Access Denied')}\n\nMaximum attempts exceeded.\nYour access has been permanently restricted.",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    conn.close()
    a2, b2, ans2, opts2 = gen_captcha()
    ctx.user_data.update({"captcha_ans": ans2, "captcha_a": a2, "captcha_b": b2})
    kb = [[InlineKeyboardButton(str(o), callback_data=f"captcha|{o}") for o in opts2]]
    await q.edit_message_text(
        f"{header('🔐', 'Verification Failed')}\n\n"
        f"❌  Incorrect.  ⚠️  *{remaining} attempt{'s' if remaining>1 else ''} remaining.*\n\n"
        f"{DIV2}\n❓  *What is  {a2} + {b2}?*\n{DIV2}",
        reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown"
    )
    return CAPTCHA

# ── ROLE SELECT ────────────────────────────────────────────────────────────────
async def role_select(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    conn = db()
    if q.data == "role_tutor":
        existing = conn.execute("SELECT approved FROM tutors WHERE user_id=?", (q.from_user.id,)).fetchone()
        conn.close()
        if existing:
            if existing["approved"]: return await tutor_menu(update, ctx)
            await q.edit_message_text(
                f"{header('⏳', 'Approval Pending')}\n\n"
                f"{field('Status', '🟡 Pending Admin Approval')}\n\n"
                "_You will be notified once your account is approved._",
                parse_mode="Markdown"
            )
            return ConversationHandler.END
        await q.edit_message_text(
            f"{header('👨‍🏫', 'Tutor Registration')}\n\n"
            f"_Step 1 of 5_  —  Please enter your *full name:*",
            parse_mode="Markdown"
        )
        return T_NAME
    conn.close()
    return await parent_menu(update, ctx)

# ── TUTOR FLOW ─────────────────────────────────────────────────────────────────
async def t_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    if not valid_name(txt):
        await update.message.reply_text(
            "⚠️  *Invalid name.* Letters and spaces only, min 2 characters.", parse_mode="Markdown")
        return T_NAME
    ctx.user_data["t_name"] = txt
    await update.message.reply_text(
        f"{header('📱', 'WhatsApp Number')}\n\n_Step 2 of 5_\n\n"
        "Enter your *8-digit SG WhatsApp number.*\n\n"
        f"{DIV2}\n✳️  Starts with 8 or 9\n✳️  No country code\n✳️  Example: `91234567`",
        parse_mode="Markdown")
    return T_PHONE

async def t_phone(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    if not valid_phone(txt):
        await update.message.reply_text(
            "⚠️  *Invalid number.* Must be 8 digits starting with 8 or 9.\n_Example: `91234567`_",
            parse_mode="Markdown")
        return T_PHONE
    ctx.user_data["t_phone"]    = txt
    ctx.user_data["t_subjects"] = []
    await update.message.reply_text(
        f"{header('📚', 'Subjects')}\n\n_Step 3 of 5_\n\n"
        "Select *all subjects* you teach, then tap *Confirm:*",
        reply_markup=ms_keyboard(ALL_SUBJECTS, [], "tsubj"), parse_mode="Markdown")
    return T_SUBJECTS

async def t_subjects(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    val = q.data.split("|",1)[1]
    if val == "DONE":
        if not ctx.user_data.get("t_subjects"):
            await q.answer("⚠️  Select at least one subject.", show_alert=True); return T_SUBJECTS
        ctx.user_data["t_levels"] = []
        await q.edit_message_text(
            f"{header('🎓', 'Academic Levels')}\n\n_Step 3 of 5 (cont.)_\n\n"
            f"Subjects: *{', '.join(ctx.user_data['t_subjects'])}*\n\n{DIV2}\n"
            "Select the *levels* you teach:",
            reply_markup=ms_keyboard(ALL_LEVELS, [], "tlvl"), parse_mode="Markdown")
        return T_LEVELS
    sel = ctx.user_data.get("t_subjects", [])
    sel.remove(val) if val in sel else sel.append(val)
    ctx.user_data["t_subjects"] = sel
    await q.edit_message_reply_markup(reply_markup=ms_keyboard(ALL_SUBJECTS, sel, "tsubj"))
    return T_SUBJECTS

async def t_levels(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    val = q.data.split("|",1)[1]
    if val == "DONE":
        if not ctx.user_data.get("t_levels"):
            await q.answer("⚠️  Select at least one level.", show_alert=True); return T_LEVELS
        ctx.user_data["t_areas"] = []
        await q.edit_message_text(
            f"{header('📍', 'Travel Areas')}\n\n_Step 4 of 5_\n\n"
            f"Levels: *{', '.join(ctx.user_data['t_levels'])}*\n\n{DIV2}\n"
            "Select *areas* you can travel to:",
            reply_markup=ms_keyboard(ALL_AREAS, [], "tarea"), parse_mode="Markdown")
        return T_AREAS
    sel = ctx.user_data.get("t_levels", [])
    sel.remove(val) if val in sel else sel.append(val)
    ctx.user_data["t_levels"] = sel
    await q.edit_message_reply_markup(reply_markup=ms_keyboard(ALL_LEVELS, sel, "tlvl"))
    return T_LEVELS

async def t_areas(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    val = q.data.split("|",1)[1]
    if val == "DONE":
        if not ctx.user_data.get("t_areas"):
            await q.answer("⚠️  Select at least one area.", show_alert=True); return T_AREAS
        await q.edit_message_text(
            f"{header('💰', 'Hourly Rate')}\n\n_Step 5 of 5_\n\n"
            "Enter your *hourly rate in SGD.*\n\n"
            f"{DIV2}\n✳️  Numbers only\n✳️  Positive whole number\n✳️  Example: `35`",
            parse_mode="Markdown")
        return T_RATE
    sel = ctx.user_data.get("t_areas", [])
    sel.remove(val) if val in sel else sel.append(val)
    ctx.user_data["t_areas"] = sel
    await q.edit_message_reply_markup(reply_markup=ms_keyboard(ALL_AREAS, sel, "tarea"))
    return T_AREAS

async def t_rate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    if not valid_rate(txt):
        await update.message.reply_text(
            "⚠️  *Invalid rate.* Positive whole numbers only.\n_Example: `35`_", parse_mode="Markdown")
        return T_RATE
    rate = clean_rate(txt)
    ctx.user_data["t_rate"] = rate
    u = update.effective_user
    conn = db()
    conn.execute(
        "INSERT OR REPLACE INTO tutors (user_id,username,name,phone,subjects,levels,areas,rate,available,approved) VALUES (?,?,?,?,?,?,?,?,1,0)",
        (u.id, u.username or "", ctx.user_data["t_name"], ctx.user_data["t_phone"],
         ", ".join(ctx.user_data["t_subjects"]), ", ".join(ctx.user_data["t_levels"]),
         ", ".join(ctx.user_data["t_areas"]), rate))
    conn.commit(); conn.close()

    kb = [[
        InlineKeyboardButton("✅  Approve", callback_data=f"approve_tutor_{u.id}"),
        InlineKeyboardButton("❌  Reject",  callback_data=f"reject_tutor_{u.id}"),
    ]]
    await notify_all_admins(
        update.get_bot(),
        f"{header('📋', 'New Tutor Application')}\n\n"
        f"{field('Name',     ctx.user_data['t_name'])}\n"
        f"{field('WhatsApp', ctx.user_data['t_phone'])}\n"
        f"{field('Telegram', f'@{u.username or \"none\"}')}\n"
        f"{field('Subjects', ', '.join(ctx.user_data['t_subjects']))}\n"
        f"{field('Levels',   ', '.join(ctx.user_data['t_levels']))}\n"
        f"{field('Areas',    ', '.join(ctx.user_data['t_areas']))}\n"
        f"{field('Rate',     f'${rate}/hr')}\n\n{DIV2}\n"
        "_Action required: Approve or reject this application._",
        reply_markup=InlineKeyboardMarkup(kb)
    )
    await update.message.reply_text(
        f"{header('✅', 'Application Submitted')}\n\n"
        f"{field('Name',     ctx.user_data['t_name'])}\n"
        f"{field('Subjects', ', '.join(ctx.user_data['t_subjects']))}\n"
        f"{field('Levels',   ', '.join(ctx.user_data['t_levels']))}\n"
        f"{field('Areas',    ', '.join(ctx.user_data['t_areas']))}\n"
        f"{field('Rate',     f'${rate}/hr')}\n\n{DIV2}\n"
        "⏳  Your profile is *pending admin approval.*\n"
        "You will be notified once reviewed.",
        parse_mode="Markdown")
    return ConversationHandler.END

# ── TUTOR MENU ─────────────────────────────────────────────────────────────────
async def tutor_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    conn = db()
    t = conn.execute("SELECT available FROM tutors WHERE user_id=?", (update.effective_user.id,)).fetchone()
    conn.close()
    status = "🟢  Available" if (t and t["available"]) else "🔴  Unavailable"
    kb = [
        [InlineKeyboardButton("📋  Browse Requests",     callback_data="browse_requests")],
        [InlineKeyboardButton("👤  My Profile",          callback_data="view_t_profile")],
        [InlineKeyboardButton("🔄  Toggle Availability", callback_data="toggle_avail")],
    ]
    text = (f"{header('🎓', 'Tutor Dashboard')}\n\n{field('Status', status)}\n\n{DIV2}\n"
            "_Select an option to continue:_")
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    return ConversationHandler.END

# ── PARENT FLOW ────────────────────────────────────────────────────────────────
async def parent_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    kb = [
        [InlineKeyboardButton("📝  Post a Request", callback_data="post_request")],
        [InlineKeyboardButton("📋  My Requests",    callback_data="my_requests")],
    ]
    text = (f"{header('👨‍👩‍👧', 'Parent Dashboard')}\n\nWelcome to *CognifySG.*\n"
            "We match your child with the right tutor — professionally.\n\n"
            f"{DIV2}\n_Select an option to continue:_")
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    return ConversationHandler.END

async def post_request_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    await q.edit_message_text(
        f"{header('📝', 'New Tutor Request')}\n\n_Step 1 of 5_  —  Please enter your *full name:*",
        parse_mode="Markdown")
    return P_NAME

async def p_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    if not valid_name(txt):
        await update.message.reply_text(
            "⚠️  *Invalid name.* Letters and spaces only, min 2 characters.", parse_mode="Markdown")
        return P_NAME
    ctx.user_data["p_name"] = txt
    await update.message.reply_text(
        f"{header('📱', 'WhatsApp Number')}\n\n_Step 2 of 5_\n\n"
        "Enter your *8-digit SG WhatsApp number.*\n\n"
        f"{DIV2}\n✳️  Starts with 8 or 9\n✳️  No country code\n✳️  Example: `91234567`",
        parse_mode="Markdown")
    return P_PHONE

async def p_phone(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    if not valid_phone(txt):
        await update.message.reply_text(
            "⚠️  *Invalid number.* 8 digits starting with 8 or 9.\n_Example: `91234567`_",
            parse_mode="Markdown")
        return P_PHONE
    ctx.user_data["p_phone"]   = txt
    ctx.user_data["p_subject"] = []
    await update.message.reply_text(
        f"{header('📚', 'Subject Required')}\n\n_Step 3 of 5_\n\n"
        "Select the *subject(s)* your child needs help with:",
        reply_markup=ms_keyboard(ALL_SUBJECTS, [], "psubj"), parse_mode="Markdown")
    return P_SUBJECT

async def p_subject(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    val = q.data.split("|",1)[1]
    if val == "DONE":
        if not ctx.user_data.get("p_subject"):
            await q.answer("⚠️  Select at least one subject.", show_alert=True); return P_SUBJECT
        ctx.user_data["p_level"] = []
        await q.edit_message_text(
            f"{header('🎓', 'Academic Level')}\n\n_Step 4 of 5_\n\n"
            f"Subject: *{', '.join(ctx.user_data['p_subject'])}*\n\n{DIV2}\n"
            "Select your child's *current level:*",
            reply_markup=ms_keyboard(ALL_LEVELS, [], "plvl"), parse_mode="Markdown")
        return P_LEVEL
    sel = ctx.user_data.get("p_subject", [])
    sel.remove(val) if val in sel else sel.append(val)
    ctx.user_data["p_subject"] = sel
    await q.edit_message_reply_markup(reply_markup=ms_keyboard(ALL_SUBJECTS, sel, "psubj"))
    return P_SUBJECT

async def p_level(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    val = q.data.split("|",1)[1]
    if val == "DONE":
        if not ctx.user_data.get("p_level"):
            await q.answer("⚠️  Select at least one level.", show_alert=True); return P_LEVEL
        ctx.user_data["p_area"] = []
        await q.edit_message_text(
            f"{header('📍', 'Location')}\n\n_Step 4 of 5 (cont.)_\n\n"
            f"Level: *{', '.join(ctx.user_data['p_level'])}*\n\n{DIV2}\n"
            "Select your *preferred area:*",
            reply_markup=ms_keyboard(ALL_AREAS, [], "parea"), parse_mode="Markdown")
        return P_AREA
    sel = ctx.user_data.get("p_level", [])
    sel.remove(val) if val in sel else sel.append(val)
    ctx.user_data["p_level"] = sel
    await q.edit_message_reply_markup(reply_markup=ms_keyboard(ALL_LEVELS, sel, "plvl"))
    return P_LEVEL

async def p_area(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    val = q.data.split("|",1)[1]
    if val == "DONE":
        if not ctx.user_data.get("p_area"):
            await q.answer("⚠️  Select at least one area.", show_alert=True); return P_AREA
        await q.edit_message_text(
            f"{header('💰', 'Budget')}\n\n_Step 5 of 5_\n\n"
            "Enter your *max hourly budget in SGD.*\n\n"
            f"{DIV2}\n✳️  Numbers only\n✳️  Positive whole number\n✳️  Example: `35`",
            parse_mode="Markdown")
        return P_BUDGET
    sel = ctx.user_data.get("p_area", [])
    sel.remove(val) if val in sel else sel.append(val)
    ctx.user_data["p_area"] = sel
    await q.edit_message_reply_markup(reply_markup=ms_keyboard(ALL_AREAS, sel, "parea"))
    return P_AREA

async def p_budget(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    if not valid_rate(txt):
        await update.message.reply_text(
            "⚠️  *Invalid budget.* Positive whole number only.\n_Example: `35`_", parse_mode="Markdown")
        return P_BUDGET
    budget = clean_rate(txt)
    ctx.user_data["p_budget"] = budget
    u = update.effective_user
    conn = db()
    conn.execute(
        "INSERT INTO requests (parent_id,username,name,phone,subject,level,areas,budget,approved) VALUES (?,?,?,?,?,?,?,?,0)",
        (u.id, u.username or "", ctx.user_data["p_name"], ctx.user_data["p_phone"],
         ", ".join(ctx.user_data["p_subject"]), ", ".join(ctx.user_data["p_level"]),
         ", ".join(ctx.user_data["p_area"]), budget))
    req_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit(); conn.close()

    kb = [[
        InlineKeyboardButton("✅  Approve", callback_data=f"approve_req_{req_id}"),
        InlineKeyboardButton("❌  Reject",  callback_data=f"reject_req_{req_id}"),
    ]]
    await notify_all_admins(
        update.get_bot(),
        f"{header('🆕', 'New Parent Request')}\n\n"
        f"{field('Name',     ctx.user_data['p_name'])}\n"
        f"{field('WhatsApp', ctx.user_data['p_phone'])}\n"
        f"{field('Telegram', f'@{u.username or \"none\"}')}\n"
        f"{field('Subject',  ', '.join(ctx.user_data['p_subject']))}\n"
        f"{field('Level',    ', '.join(ctx.user_data['p_level']))}\n"
        f"{field('Area',     ', '.join(ctx.user_data['p_area']))}\n"
        f"{field('Budget',   f'${budget}/hr')}\n\n{DIV2}\n"
        f"_Request ID: #{req_id} — Action required._",
        reply_markup=InlineKeyboardMarkup(kb)
    )
    await update.message.reply_text(
        f"{header('✅', 'Request Submitted')}\n\n"
        f"{field('Subject', ', '.join(ctx.user_data['p_subject']))}\n"
        f"{field('Level',   ', '.join(ctx.user_data['p_level']))}\n"
        f"{field('Area',    ', '.join(ctx.user_data['p_area']))}\n"
        f"{field('Budget',  f'${budget}/hr')}\n\n{DIV2}\n"
        "⏳  Your request is *pending admin approval.*\n"
        "Our team will contact you on WhatsApp once a match is confirmed.",
        parse_mode="Markdown")
    return ConversationHandler.END

# ── ADMIN APPROVAL ─────────────────────────────────────────────────────────────
async def approve_tutor(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if not is_admin(update.effective_user.id): return
    uid = int(q.data.replace("approve_tutor_", ""))
    conn = db()
    conn.execute("UPDATE tutors SET approved=1 WHERE user_id=?", (uid,))
    conn.commit(); conn.close()
    await q.edit_message_text(q.message.text + f"\n\n{DIV2}\n✅  *Approved* by @{update.effective_user.username}.", parse_mode="Markdown")
    try:
        await ctx.bot.send_message(uid,
            f"{header('✅', 'Profile Approved')}\n\n"
            "Your tutor profile has been *approved* by CognifySG.\n\n{DIV2}\n"
            "Use /start to access your dashboard and browse requests.",
            parse_mode="Markdown")
    except: pass

async def reject_tutor(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if not is_admin(update.effective_user.id): return
    uid = int(q.data.replace("reject_tutor_", ""))
    conn = db()
    conn.execute("DELETE FROM tutors WHERE user_id=?", (uid,))
    conn.commit(); conn.close()
    await q.edit_message_text(q.message.text + f"\n\n{DIV2}\n❌  *Rejected* by @{update.effective_user.username}.", parse_mode="Markdown")
    try:
        await ctx.bot.send_message(uid,
            f"{header('❌', 'Application Unsuccessful')}\n\n"
            "Your tutor profile did not meet our current requirements.\n"
            "_You may re-apply using /start._", parse_mode="Markdown")
    except: pass

async def approve_req(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if not is_admin(update.effective_user.id): return
    req_id = int(q.data.replace("approve_req_", ""))
    conn = db()
    req = conn.execute("SELECT * FROM requests WHERE id=?", (req_id,)).fetchone()
    conn.execute("UPDATE requests SET approved=1 WHERE id=?", (req_id,))
    conn.commit(); conn.close()
    await q.edit_message_text(q.message.text + f"\n\n{DIV2}\n✅  *Approved* by @{update.effective_user.username}.", parse_mode="Markdown")
    if req:
        try:
            await ctx.bot.send_message(req["parent_id"],
                f"{header('✅', 'Request Approved')}\n\n"
                "Your request has been *approved* by CognifySG.\n\n"
                "Our team is now matching you with suitable tutors.\n"
                "You will be contacted on WhatsApp once a match is found.",
                parse_mode="Markdown")
        except: pass

async def reject_req(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if not is_admin(update.effective_user.id): return
    req_id = int(q.data.replace("reject_req_", ""))
    conn = db()
    req = conn.execute("SELECT * FROM requests WHERE id=?", (req_id,)).fetchone()
    conn.execute("DELETE FROM requests WHERE id=?", (req_id,))
    conn.commit(); conn.close()
    await q.edit_message_text(q.message.text + f"\n\n{DIV2}\n❌  *Rejected* by @{update.effective_user.username}.", parse_mode="Markdown")
    if req:
        try:
            await ctx.bot.send_message(req["parent_id"],
                f"{header('❌', 'Request Unsuccessful')}\n\n"
                "We were unable to process your request at this time.\n"
                "_Please use /start to submit a new request._", parse_mode="Markdown")
        except: pass

# ── ADMIN MANAGEMENT COMMANDS ──────────────────────────────────────────────────
async def add_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid != SUPER_ADMIN_ID:
        await update.message.reply_text("⛔️  Only the Super Admin can add new admins.")
        return
    if not ctx.args:
        await update.message.reply_text(
            f"{header('➕', 'Add Admin')}\n\nUsage: `/addadmin TELEGRAM_USER_ID`\n\n"
            "_Ask your team member to send any message to @userinfobot to get their ID._",
            parse_mode="Markdown")
        return
    try:
        new_id = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("⚠️  Please provide a valid numeric Telegram user ID.")
        return
    conn = db()
    conn.execute(
        "INSERT OR IGNORE INTO admins(user_id, username, name, added_by) VALUES (?,?,?,?)",
        (new_id, "", "Admin", uid))
    conn.commit(); conn.close()
    await update.message.reply_text(
        f"{header('✅', 'Admin Added')}\n\n"
        f"User `{new_id}` has been granted admin access.\n\n"
        "They will now receive all notifications and can approve/reject applications.",
        parse_mode="Markdown")
    try:
        await ctx.bot.send_message(new_id,
            f"{header('🔑', 'Admin Access Granted')}\n\n"
            "You have been granted *admin access* to CognifySG.\n\n"
            f"{DIV2}\n"
            "You will now receive all tutor and parent notifications.\n"
            "Use the approve/reject buttons directly in your notifications.",
            parse_mode="Markdown")
    except: pass

async def remove_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid != SUPER_ADMIN_ID:
        await update.message.reply_text("⛔️  Only the Super Admin can remove admins.")
        return
    if not ctx.args:
        await update.message.reply_text(
            f"{header('➖', 'Remove Admin')}\n\nUsage: `/removeadmin TELEGRAM_USER_ID`",
            parse_mode="Markdown")
        return
    try:
        rem_id = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("⚠️  Please provide a valid numeric Telegram user ID.")
        return
    if rem_id == SUPER_ADMIN_ID:
        await update.message.reply_text("⛔️  You cannot remove the Super Admin.")
        return
    conn = db()
    conn.execute("DELETE FROM admins WHERE user_id=?", (rem_id,))
    conn.commit(); conn.close()
    await update.message.reply_text(
        f"{header('✅', 'Admin Removed')}\n\nUser `{rem_id}` has been removed from admin access.",
        parse_mode="Markdown")

async def list_admins(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔️  Admin access required.")
        return
    conn = db()
    admins = conn.execute("SELECT user_id, username, added_at FROM admins ORDER BY added_at").fetchall()
    conn.close()
    lines = []
    for a in admins:
        crown = "👑 " if a["user_id"] == SUPER_ADMIN_ID else "🔑 "
        handle = f"@{a['username']}" if a["username"] else f"ID: `{a['user_id']}`"
        lines.append(f"{crown}{handle}")
    await update.message.reply_text(
        f"{header('👥', 'Admin Team')}\n\n" + "\n".join(lines) + f"\n\n{DIV2}\n"
        f"_Total: {len(admins)} admin{'s' if len(admins)>1 else ''}_\n\n"
        "👑 Super Admin  |  🔑 Admin",
        parse_mode="Markdown")

async def admin_panel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔️  Admin access required.")
        return
    conn = db()
    tutors_pending  = conn.execute("SELECT COUNT(*) FROM tutors   WHERE approved=0").fetchone()[0]
    tutors_active   = conn.execute("SELECT COUNT(*) FROM tutors   WHERE approved=1").fetchone()[0]
    reqs_pending    = conn.execute("SELECT COUNT(*) FROM requests WHERE approved=0").fetchone()[0]
    reqs_open       = conn.execute("SELECT COUNT(*) FROM requests WHERE approved=1 AND status='open'").fetchone()[0]
    reqs_matched    = conn.execute("SELECT COUNT(*) FROM requests WHERE status='matched'").fetchone()[0]
    applications    = conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0]
    blocked         = conn.execute("SELECT COUNT(*) FROM blocked").fetchone()[0]
    conn.close()
    await update.message.reply_text(
        f"{header('⚙️', 'Admin Panel — CognifySG')}\n\n"
        f"👨‍🏫  *Tutors*\n"
        f"  {field('Active',    tutors_active)}\n"
        f"  {field('Pending',   tutors_pending)}\n\n"
        f"👨‍👩‍👧  *Requests*\n"
        f"  {field('Open',      reqs_open)}\n"
        f"  {field('Pending',   reqs_pending)}\n"
        f"  {field('Matched',   reqs_matched)}\n\n"
        f"📨  {field('Total Applications', applications)}\n"
        f"🚫  {field('Blocked Users',      blocked)}\n\n"
        f"{DIV2}\n"
        "_Commands:_\n"
        "`/addadmin ID`  — Add a new admin\n"
        "`/removeadmin ID`  — Remove an admin\n"
        "`/listadmins`  — View all admins",
        parse_mode="Markdown")

# ── BROWSE / APPLY ─────────────────────────────────────────────────────────────
async def browse_requests(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    uid = q.from_user.id
    conn = db()
    t = conn.execute("SELECT approved FROM tutors WHERE user_id=?", (uid,)).fetchone()
    if not t or not t["approved"]:
        conn.close()
        await q.edit_message_text(
            f"{header('⏳', 'Access Restricted')}\n\nYour profile is pending admin approval.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙  Back", callback_data="back_tutor")]]),
            parse_mode="Markdown"); return
    reqs = conn.execute(
        "SELECT id,subject,level,areas,budget FROM requests WHERE status='open' AND approved=1 ORDER BY created_at DESC"
    ).fetchall()
    conn.close()
    if not reqs:
        await q.edit_message_text(
            f"{header('📋', 'Open Requests')}\n\nNo open requests at this time.\n_Please check back later._",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙  Back", callback_data="back_tutor")]]),
            parse_mode="Markdown"); return
    ctx.user_data["req_list"] = [dict(r) for r in reqs]
    ctx.user_data["req_idx"]  = 0
    await show_req_card(q, ctx)

async def show_req_card(q, ctx):
    reqs = ctx.user_data["req_list"]
    idx  = ctx.user_data["req_idx"]
    r    = reqs[idx]
    nav  = []
    if idx > 0:              nav.append(InlineKeyboardButton("◀  Previous", callback_data="req_prev"))
    if idx < len(reqs) - 1: nav.append(InlineKeyboardButton("Next  ▶",    callback_data="req_next"))
    kb = []
    if nav: kb.append(nav)
    kb.append([InlineKeyboardButton("✅  Apply for this Request", callback_data=f"apply_{r['id']}")])
    kb.append([InlineKeyboardButton("🔙  Back to Dashboard",     callback_data="back_tutor")])
    await q.edit_message_text(
        f"{header('📋', 'Open Request')}\n\n_Listing {idx+1} of {len(reqs)}_\n\n"
        f"{field('Ref',     f'#{r[\"id\"]}')}\n"
        f"{field('Subject', r['subject'])}\n"
        f"{field('Level',   r['level'])}\n"
        f"{field('Area',    r['areas'])}\n"
        f"{field('Budget',  f'${r[\"budget\"]}/hr')}\n\n{DIV2}\n"
        "_Contact details withheld until match is confirmed._",
        reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")

async def req_nav(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    ctx.user_data["req_idx"] += 1 if q.data == "req_next" else -1
    await show_req_card(q, ctx)

async def apply_request(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    req_id   = int(q.data.replace("apply_", ""))
    tutor_id = q.from_user.id
    conn     = db()
    if conn.execute("SELECT id FROM applications WHERE tutor_id=? AND request_id=?", (tutor_id, req_id)).fetchone():
        conn.close()
        await q.answer("⚠️  You have already applied for this request.", show_alert=True); return
    tutor = conn.execute("SELECT * FROM tutors   WHERE user_id=?", (tutor_id,)).fetchone()
    req   = conn.execute("SELECT * FROM requests WHERE id=?",      (req_id,)).fetchone()
    if tutor and req:
        conn.execute("INSERT INTO applications (tutor_id,request_id) VALUES (?,?)", (tutor_id, req_id))
        conn.commit()
        await notify_all_admins(
            ctx.bot,
            f"{header('🎯', 'New Application — Action Required')}\n\n"
            f"{DIV2}\n📌  *JOB REQUEST*\n"
            f"{field('Ref',     f'#{req[\"id\"]}')}\n"
            f"{field('Subject', req['subject'])}\n"
            f"{field('Level',   req['level'])}\n"
            f"{field('Area',    req['areas'])}\n"
            f"{field('Budget',  f'${req[\"budget\"]}/hr')}\n\n"
            f"{DIV2}\n👨‍🏫  *TUTOR*\n"
            f"{field('Name',     tutor['name'])}\n"
            f"{field('WhatsApp', tutor['phone'])}\n"
            f"{field('Telegram', f'@{tutor[\"username\"] or \"none\"}')}\n"
            f"{field('Subjects', tutor['subjects'])}\n"
            f"{field('Levels',   tutor['levels'])}\n"
            f"{field('Rate',     f'${tutor[\"rate\"]}/hr')}\n\n"
            f"{DIV2}\n👨‍👩‍👧  *PARENT*\n"
            f"{field('Name',     req['name'])}\n"
            f"{field('WhatsApp', req['phone'])}\n"
            f"{field('Telegram', f'@{req[\"username\"] or \"none\"}')}\n\n{DIV2}\n"
            "✅  _Contact both parties on WhatsApp to confirm the match._"
        )
    conn.close()
    await q.edit_message_text(
        f"{header('✅', 'Application Submitted')}\n\n"
        "Your application has been received.\n\n{DIV2}\n"
        "Our team will contact you on *WhatsApp* to confirm the match.\n\n"
        "_Use /start to return to the dashboard._", parse_mode="Markdown")

# ── PROFILE / MISC ─────────────────────────────────────────────────────────────
async def view_t_profile(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    conn = db()
    t = conn.execute("SELECT * FROM tutors WHERE user_id=?", (q.from_user.id,)).fetchone()
    conn.close()
    if not t:
        await q.edit_message_text("Profile not found. Use /start to register."); return
    status   = "🟢  Available" if t["available"] else "🔴  Unavailable"
    approved = "✅  Approved"  if t["approved"]  else "⏳  Pending Review"
    await q.edit_message_text(
        f"{header('👤', 'My Tutor Profile')}\n\n"
        f"{field('Name',     t['name'])}\n{field('Phone', t['phone'])}\n"
        f"{field('Subjects', t['subjects'])}\n{field('Levels', t['levels'])}\n"
        f"{field('Areas',    t['areas'])}\n{field('Rate',  f'${t[\"rate\"]}/hr')}\n"
        f"{field('Status',   status)}\n{field('Account', approved)}",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙  Back", callback_data="back_tutor")]]),
        parse_mode="Markdown")

async def toggle_avail(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    uid = q.from_user.id
    conn = db()
    row = conn.execute("SELECT available FROM tutors WHERE user_id=?", (uid,)).fetchone()
    new = 0 if (row and row["available"]) else 1
    conn.execute("UPDATE tutors SET available=? WHERE user_id=?", (new, uid))
    conn.commit(); conn.close()
    label = "🟢  You are now *Available.*" if new else "🔴  You are now *Unavailable.*"
    await q.edit_message_text(
        f"{header('🔄', 'Availability Updated')}\n\n{label}", parse_mode="Markdown")
    return await tutor_menu(update, ctx)

async def my_requests(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    conn = db()
    reqs = conn.execute(
        "SELECT id,subject,level,budget,status,approved FROM requests WHERE parent_id=? ORDER BY created_at DESC",
        (q.from_user.id,)).fetchall()
    conn.close()
    if not reqs:
        await q.edit_message_text(
            f"{header('📋', 'My Requests')}\n\nNo requests submitted yet.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙  Back", callback_data="back_parent")]]),
            parse_mode="Markdown"); return
    lines = []
    for r in reqs:
        if not r["approved"]:          icon = "⏳"
        elif r["status"] == "matched": icon = "✅"
        else:                          icon = "🟡"
        lines.append(f"{icon}  *#{r['id']}*  —  {r['subject']} | ${r['budget']}/hr")
    await q.edit_message_text(
        f"{header('📋', 'My Requests')}\n\n" + "\n".join(lines) + f"\n\n{DIV2}\n"
        "_⏳ Pending  |  🟡 Open  |  ✅ Matched_",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙  Back", callback_data="back_parent")]]),
        parse_mode="Markdown")

async def back_tutor(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    return await tutor_menu(update, ctx)

async def back_parent(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    return await parent_menu(update, ctx)

# ── MAIN ───────────────────────────────────────────────────────────────────────
def main():
    init_db()
    app = (
        Application.builder()
        .token(TOKEN)
        .concurrent_updates(True)
        .build()
    )
    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CallbackQueryHandler(post_request_start, pattern="^post_request$"),
        ],
        states={
            CAPTCHA:    [CallbackQueryHandler(captcha_handler, pattern="^captcha\\|")],
            ROLE_SELECT:[CallbackQueryHandler(role_select,     pattern="^role_")],
            T_NAME:     [MessageHandler(filters.TEXT & ~filters.COMMAND, t_name)],
            T_PHONE:    [MessageHandler(filters.TEXT & ~filters.COMMAND, t_phone)],
            T_SUBJECTS: [CallbackQueryHandler(t_subjects, pattern="^tsubj\\|")],
            T_LEVELS:   [CallbackQueryHandler(t_levels,   pattern="^tlvl\\|")],
            T_AREAS:    [CallbackQueryHandler(t_areas,    pattern="^tarea\\|")],
            T_RATE:     [MessageHandler(filters.TEXT & ~filters.COMMAND, t_rate)],
            P_NAME:     [MessageHandler(filters.TEXT & ~filters.COMMAND, p_name)],
            P_PHONE:    [MessageHandler(filters.TEXT & ~filters.COMMAND, p_phone)],
            P_SUBJECT:  [CallbackQueryHandler(p_subject, pattern="^psubj\\|")],
            P_LEVEL:    [CallbackQueryHandler(p_level,   pattern="^plvl\\|")],
            P_AREA:     [CallbackQueryHandler(p_area,    pattern="^parea\\|")],
            P_BUDGET:   [MessageHandler(filters.TEXT & ~filters.COMMAND, p_budget)],
        },
        fallbacks=[CommandHandler("start", start)],
        per_message=False,
        allow_reentry=True,
    )
    app.add_handler(conv)
    app.add_handler(CommandHandler("addadmin",    add_admin))
    app.add_handler(CommandHandler("removeadmin", remove_admin))
    app.add_handler(CommandHandler("listadmins",  list_admins))
    app.add_handler(CommandHandler("admin",       admin_panel))
    app.add_handler(CallbackQueryHandler(browse_requests, pattern="^browse_requests$"))
    app.add_handler(CallbackQueryHandler(req_nav,         pattern="^req_(next|prev)$"))
    app.add_handler(CallbackQueryHandler(apply_request,   pattern="^apply_\\d+$"))
    app.add_handler(CallbackQueryHandler(view_t_profile,  pattern="^view_t_profile$"))
    app.add_handler(CallbackQueryHandler(toggle_avail,    pattern="^toggle_avail$"))
    app.add_handler(CallbackQueryHandler(my_requests,     pattern="^my_requests$"))
    app.add_handler(CallbackQueryHandler(back_tutor,      pattern="^back_tutor$"))
    app.add_handler(CallbackQueryHandler(back_parent,     pattern="^back_parent$"))
    app.add_handler(CallbackQueryHandler(approve_tutor,   pattern="^approve_tutor_\\d+$"))
    app.add_handler(CallbackQueryHandler(reject_tutor,    pattern="^reject_tutor_\\d+$"))
    app.add_handler(CallbackQueryHandler(approve_req,     pattern="^approve_req_\\d+$"))
    app.add_handler(CallbackQueryHandler(reject_req,      pattern="^reject_req_\\d+$"))

    print("CognifySG v4 is running!")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    main()

"""
CognifySG — Telegram Tuition Agency Bot
Connects tutors and parents. Admin manually matches via WhatsApp.

Setup:
1. pip install python-telegram-bot==20.7
2. Get token from @BotFather on Telegram
3. Get your ADMIN_CHAT_ID from @userinfobot on Telegram
4. Set TOKEN and ADMIN_CHAT_ID below
5. python main.py
"""

import os
import sqlite3
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)

# ── CONFIG ─────────────────────────────────────────────────────────────────────
TOKEN        = os.environ.get("TOKEN", "YOUR_BOT_TOKEN_HERE")
ADMIN_CHAT_ID = int(os.environ.get("ADMIN_CHAT_ID", "0"))  # set this!

# ── UPTIME ROBOT KEEPALIVE ─────────────────────────────────────────────────────
class KeepAlive(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"CognifySG is running!")
    def log_message(self, *args):
        pass

def run_keepalive():
    server = HTTPServer(("0.0.0.0", 8080), KeepAlive)
    server.serve_forever()

threading.Thread(target=run_keepalive, daemon=True).start()

# ── DATABASE ───────────────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect("cognify.db")
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS tutors (
            user_id    INTEGER PRIMARY KEY,
            username   TEXT,
            name       TEXT,
            phone      TEXT,
            subjects   TEXT,
            levels     TEXT,
            areas      TEXT,
            rate       TEXT,
            available  INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS requests (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            parent_id  INTEGER,
            username   TEXT,
            name       TEXT,
            phone      TEXT,
            subject    TEXT,
            level      TEXT,
            areas      TEXT,
            budget     TEXT,
            status     TEXT DEFAULT 'open',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS applications (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            tutor_id   INTEGER,
            request_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(tutor_id, request_id)
        )
    """)
    conn.commit()
    conn.close()

def db():
    return sqlite3.connect("cognify.db")

# ── CONVERSATION STATES ────────────────────────────────────────────────────────
(
    ROLE_SELECT,
    T_NAME, T_PHONE, T_SUBJECTS, T_LEVELS, T_AREAS, T_RATE,
    P_NAME, P_PHONE, P_SUBJECT, P_LEVEL, P_AREA, P_BUDGET,
) = range(13)

# ── OPTIONS ────────────────────────────────────────────────────────────────────
ALL_SUBJECTS = ["Maths", "English", "Science", "Chinese", "Malay", "Tamil",
                "Physics", "Chemistry", "Biology", "History", "Geography", "Literature"]
ALL_LEVELS   = ["Primary 1-3", "Primary 4-6", "Lower Sec", "Upper Sec", "JC", "IB/IP", "Poly/ITE"]
ALL_AREAS    = ["North", "South", "East", "West", "Central", "Online"]

def multiselect_keyboard(options, selected, prefix, done_label="Done ✅"):
    buttons = []
    row = []
    for i, opt in enumerate(options):
        tick = "✅ " if opt in selected else ""
        row.append(InlineKeyboardButton(f"{tick}{opt}", callback_data=f"{prefix}|{opt}"))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton(done_label, callback_data=f"{prefix}|DONE")])
    return InlineKeyboardMarkup(buttons)

# ── /START ─────────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    keyboard = [[
        InlineKeyboardButton("I am a Tutor 👨‍🏫", callback_data="role_tutor"),
        InlineKeyboardButton("I am a Parent 👨‍👩‍👧", callback_data="role_parent"),
    ]]
    text = (
        "Welcome to *CognifySG* 🎓\n\n"
        "Singapore's smartest tuition matching service.\n\n"
        "Are you a tutor or a parent?"
    )
    if update.message:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return ROLE_SELECT

async def role_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "role_tutor":
        conn = db()
        existing = conn.execute("SELECT user_id FROM tutors WHERE user_id=?", (query.from_user.id,)).fetchone()
        conn.close()
        if existing:
            return await tutor_menu(update, context)
        await query.edit_message_text("Let's set up your tutor profile!\n\nWhat is your *full name*?", parse_mode="Markdown")
        return T_NAME
    else:
        conn = db()
        existing = conn.execute("SELECT id FROM requests WHERE parent_id=? AND status='open'", (query.from_user.id,)).fetchone()
        conn.close()
        return await parent_menu(update, context)

# ── TUTOR REGISTRATION ─────────────────────────────────────────────────────────
async def t_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["t_name"] = update.message.text
    await update.message.reply_text("What is your *WhatsApp number*?\nExample: `91234567`", parse_mode="Markdown")
    return T_PHONE

async def t_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["t_phone"] = update.message.text
    context.user_data["t_subjects"] = []
    await update.message.reply_text(
        "Select the subjects you teach.\nTap each subject, then tap *Done ✅*",
        reply_markup=multiselect_keyboard(ALL_SUBJECTS, [], "tsubj"),
        parse_mode="Markdown"
    )
    return T_SUBJECTS

async def t_subjects(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, val = query.data.split("|", 1)
    if val == "DONE":
        if not context.user_data.get("t_subjects"):
            await query.answer("Please select at least one subject!", show_alert=True)
            return T_SUBJECTS
        context.user_data["t_levels"] = []
        await query.edit_message_text(
            f"Subjects: *{', '.join(context.user_data['t_subjects'])}*\n\nNow select the levels you teach:",
            reply_markup=multiselect_keyboard(ALL_LEVELS, [], "tlvl"),
            parse_mode="Markdown"
        )
        return T_LEVELS
    selected = context.user_data.get("t_subjects", [])
    if val in selected:
        selected.remove(val)
    else:
        selected.append(val)
    context.user_data["t_subjects"] = selected
    await query.edit_message_reply_markup(reply_markup=multiselect_keyboard(ALL_SUBJECTS, selected, "tsubj"))
    return T_SUBJECTS

async def t_levels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, val = query.data.split("|", 1)
    if val == "DONE":
        if not context.user_data.get("t_levels"):
            await query.answer("Please select at least one level!", show_alert=True)
            return T_LEVELS
        context.user_data["t_areas"] = []
        await query.edit_message_text(
            f"Levels: *{', '.join(context.user_data['t_levels'])}*\n\nWhich areas can you travel to?",
            reply_markup=multiselect_keyboard(ALL_AREAS, [], "tarea"),
            parse_mode="Markdown"
        )
        return T_AREAS
    selected = context.user_data.get("t_levels", [])
    if val in selected:
        selected.remove(val)
    else:
        selected.append(val)
    context.user_data["t_levels"] = selected
    await query.edit_message_reply_markup(reply_markup=multiselect_keyboard(ALL_LEVELS, selected, "tlvl"))
    return T_LEVELS

async def t_areas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, val = query.data.split("|", 1)
    if val == "DONE":
        if not context.user_data.get("t_areas"):
            await query.answer("Please select at least one area!", show_alert=True)
            return T_AREAS
        await query.edit_message_text(
            f"Areas: *{', '.join(context.user_data['t_areas'])}*\n\nWhat is your hourly rate?\nExample: `$30/hr` or `$25–35/hr`",
            parse_mode="Markdown"
        )
        return T_RATE
    selected = context.user_data.get("t_areas", [])
    if val in selected:
        selected.remove(val)
    else:
        selected.append(val)
    context.user_data["t_areas"] = selected
    await query.edit_message_reply_markup(reply_markup=multiselect_keyboard(ALL_AREAS, selected, "tarea"))
    return T_AREAS

async def t_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["t_rate"] = update.message.text
    user = update.effective_user

    conn = db()
    conn.execute(
        """INSERT OR REPLACE INTO tutors
           (user_id, username, name, phone, subjects, levels, areas, rate, available)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)""",
        (
            user.id,
            user.username or "",
            context.user_data["t_name"],
            context.user_data["t_phone"],
            ", ".join(context.user_data["t_subjects"]),
            ", ".join(context.user_data["t_levels"]),
            ", ".join(context.user_data["t_areas"]),
            context.user_data["t_rate"],
        )
    )
    conn.commit()
    conn.close()

    # Notify admin
    if ADMIN_CHAT_ID:
        await context.bot.send_message(
            ADMIN_CHAT_ID,
            f"📋 *NEW TUTOR REGISTERED*\n\n"
            f"Name: {context.user_data['t_name']}\n"
            f"WhatsApp: {context.user_data['t_phone']}\n"
            f"Telegram: @{user.username or 'no username'}\n"
            f"Subjects: {', '.join(context.user_data['t_subjects'])}\n"
            f"Levels: {', '.join(context.user_data['t_levels'])}\n"
            f"Areas: {', '.join(context.user_data['t_areas'])}\n"
            f"Rate: {context.user_data['t_rate']}",
            parse_mode="Markdown"
        )

    await update.message.reply_text(
        "Profile saved! ✅\n\n"
        f"Name: {context.user_data['t_name']}\n"
        f"Subjects: {', '.join(context.user_data['t_subjects'])}\n"
        f"Levels: {', '.join(context.user_data['t_levels'])}\n"
        f"Areas: {', '.join(context.user_data['t_areas'])}\n"
        f"Rate: {context.user_data['t_rate']}\n\n"
        "You can now browse and apply for parent requests!"
    )
    return await tutor_menu_msg(update, context)

# ── TUTOR MENU ─────────────────────────────────────────────────────────────────
async def tutor_menu_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("Browse parent requests 📋", callback_data="browse_requests")],
        [InlineKeyboardButton("My profile 👤", callback_data="view_t_profile")],
        [InlineKeyboardButton("Toggle availability 🔄", callback_data="toggle_avail")],
    ]
    await update.message.reply_text(
        "*Tutor Dashboard* — CognifySG\n\nWhat would you like to do?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    return ConversationHandler.END

async def tutor_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    keyboard = [
        [InlineKeyboardButton("Browse parent requests 📋", callback_data="browse_requests")],
        [InlineKeyboardButton("My profile 👤", callback_data="view_t_profile")],
        [InlineKeyboardButton("Toggle availability 🔄", callback_data="toggle_avail")],
    ]
    await query.edit_message_text(
        "*Tutor Dashboard* — CognifySG\n\nWhat would you like to do?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    return ConversationHandler.END

# ── PARENT REGISTRATION ────────────────────────────────────────────────────────
async def parent_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("Post a request 📝", callback_data="post_request")],
        [InlineKeyboardButton("My requests 📋", callback_data="my_requests")],
    ]
    text = "*Parent Dashboard* — CognifySG\n\nWhat would you like to do?"
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return ConversationHandler.END

async def post_request_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Let's post your tutor request!\n\nWhat is your *name*?", parse_mode="Markdown")
    return P_NAME

async def p_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["p_name"] = update.message.text
    await update.message.reply_text("What is your *WhatsApp number*?\nExample: `91234567`", parse_mode="Markdown")
    return P_PHONE

async def p_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["p_phone"] = update.message.text
    context.user_data["p_subject"] = []
    await update.message.reply_text(
        "Which subject do you need help with?",
        reply_markup=multiselect_keyboard(ALL_SUBJECTS, [], "psubj", "Done ✅")
    )
    return P_SUBJECT

async def p_subject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, val = query.data.split("|", 1)
    if val == "DONE":
        if not context.user_data.get("p_subject"):
            await query.answer("Please select at least one subject!", show_alert=True)
            return P_SUBJECT
        context.user_data["p_level"] = []
        await query.edit_message_text(
            f"Subject: *{', '.join(context.user_data['p_subject'])}*\n\nWhat level is your child?",
            reply_markup=multiselect_keyboard(ALL_LEVELS, [], "plvl"),
            parse_mode="Markdown"
        )
        return P_LEVEL
    selected = context.user_data.get("p_subject", [])
    if val in selected:
        selected.remove(val)
    else:
        selected.append(val)
    context.user_data["p_subject"] = selected
    await query.edit_message_reply_markup(reply_markup=multiselect_keyboard(ALL_SUBJECTS, selected, "psubj"))
    return P_SUBJECT

async def p_level(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, val = query.data.split("|", 1)
    if val == "DONE":
        if not context.user_data.get("p_level"):
            await query.answer("Please select at least one level!", show_alert=True)
            return P_LEVEL
        context.user_data["p_area"] = []
        await query.edit_message_text(
            f"Level: *{', '.join(context.user_data['p_level'])}*\n\nWhich area are you in?",
            reply_markup=multiselect_keyboard(ALL_AREAS, [], "parea"),
            parse_mode="Markdown"
        )
        return P_AREA
    selected = context.user_data.get("p_level", [])
    if val in selected:
        selected.remove(val)
    else:
        selected.append(val)
    context.user_data["p_level"] = selected
    await query.edit_message_reply_markup(reply_markup=multiselect_keyboard(ALL_LEVELS, selected, "plvl"))
    return P_LEVEL

async def p_area(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, val = query.data.split("|", 1)
    if val == "DONE":
        if not context.user_data.get("p_area"):
            await query.answer("Please select at least one area!", show_alert=True)
            return P_AREA
        await query.edit_message_text(
            f"Area: *{', '.join(context.user_data['p_area'])}*\n\nWhat is your budget per hour?\nExample: `$30/hr` or `up to $40/hr`",
            parse_mode="Markdown"
        )
        return P_BUDGET
    selected = context.user_data.get("p_area", [])
    if val in selected:
        selected.remove(val)
    else:
        selected.append(val)
    context.user_data["p_area"] = selected
    await query.edit_message_reply_markup(reply_markup=multiselect_keyboard(ALL_AREAS, selected, "parea"))
    return P_AREA

async def p_budget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["p_budget"] = update.message.text
    user = update.effective_user

    conn = db()
    conn.execute(
        """INSERT INTO requests
           (parent_id, username, name, phone, subject, level, areas, budget)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            user.id,
            user.username or "",
            context.user_data["p_name"],
            context.user_data["p_phone"],
            ", ".join(context.user_data["p_subject"]),
            ", ".join(context.user_data["p_level"]),
            ", ".join(context.user_data["p_area"]),
            context.user_data["p_budget"],
        )
    )
    conn.commit()
    conn.close()

    # Notify admin
    if ADMIN_CHAT_ID:
        await context.bot.send_message(
            ADMIN_CHAT_ID,
            f"🆕 *NEW PARENT REQUEST*\n\n"
            f"Name: {context.user_data['p_name']}\n"
            f"WhatsApp: {context.user_data['p_phone']}\n"
            f"Telegram: @{user.username or 'no username'}\n"
            f"Subject: {', '.join(context.user_data['p_subject'])}\n"
            f"Level: {', '.join(context.user_data['p_level'])}\n"
            f"Area: {', '.join(context.user_data['p_area'])}\n"
            f"Budget: {context.user_data['p_budget']}\n\n"
            f"_Waiting for a tutor to apply._",
            parse_mode="Markdown"
        )

    await update.message.reply_text(
        "Request posted! ✅\n\n"
        f"Subject: {', '.join(context.user_data['p_subject'])}\n"
        f"Level: {', '.join(context.user_data['p_level'])}\n"
        f"Area: {', '.join(context.user_data['p_area'])}\n"
        f"Budget: {context.user_data['p_budget']}\n\n"
        "Our team will contact you on WhatsApp once we find a suitable tutor! 🎓"
    )
    return ConversationHandler.END

# ── BROWSE REQUESTS (Tutor) ────────────────────────────────────────────────────
async def browse_requests(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    conn = db()
    requests = conn.execute(
        "SELECT id, subject, level, areas, budget FROM requests WHERE status='open' ORDER BY created_at DESC"
    ).fetchall()
    conn.close()

    if not requests:
        await query.edit_message_text(
            "No open requests right now. Check back soon!",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back 🔙", callback_data="back_tutor")]])
        )
        return

    context.user_data["req_list"] = requests
    context.user_data["req_idx"] = 0
    await show_request_card(query, context)

async def show_request_card(query, context):
    requests = context.user_data["req_list"]
    idx = context.user_data["req_idx"]
    r = requests[idx]
    # r = (id, subject, level, areas, budget)

    text = (
        f"📋 *Request {idx + 1} of {len(requests)}*\n\n"
        f"Subject: {r[1]}\n"
        f"Level: {r[2]}\n"
        f"Area: {r[3]}\n"
        f"Budget: {r[4]}\n"
        f"Request ID: #{r[0]}"
    )
    nav = []
    if idx > 0:
        nav.append(InlineKeyboardButton("◀ Prev", callback_data="req_prev"))
    if idx < len(requests) - 1:
        nav.append(InlineKeyboardButton("Next ▶", callback_data="req_next"))

    keyboard = []
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("Apply ✅", callback_data=f"apply_{r[0]}")])
    keyboard.append([InlineKeyboardButton("Back 🔙", callback_data="back_tutor")])

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def req_nav(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["req_idx"] += 1 if query.data == "req_next" else -1
    await show_request_card(query, context)

async def apply_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    req_id = int(query.data.replace("apply_", ""))
    tutor_id = query.from_user.id

    conn = db()

    # Check duplicate application
    already = conn.execute(
        "SELECT id FROM applications WHERE tutor_id=? AND request_id=?", (tutor_id, req_id)
    ).fetchone()
    if already:
        conn.close()
        await query.answer("You already applied for this request!", show_alert=True)
        return

    # Get tutor + request details
    tutor = conn.execute("SELECT * FROM tutors WHERE user_id=?", (tutor_id,)).fetchone()
    req   = conn.execute("SELECT * FROM requests WHERE id=?", (req_id,)).fetchone()

    if tutor and req:
        conn.execute(
            "INSERT INTO applications (tutor_id, request_id) VALUES (?, ?)", (tutor_id, req_id)
        )
        conn.commit()

        # Notify admin with FULL details of both
        if ADMIN_CHAT_ID:
            await context.bot.send_message(
                ADMIN_CHAT_ID,
                f"🎯 *NEW APPLICATION — ACTION REQUIRED*\n\n"
                f"━━━━━━━━━━━━━━━━━\n"
                f"📌 JOB POSTING\n"
                f"Request ID: #{req[0]}\n"
                f"Subject: {req[5]}\n"
                f"Level: {req[6]}\n"
                f"Area: {req[7]}\n"
                f"Budget: {req[8]}\n\n"
                f"━━━━━━━━━━━━━━━━━\n"
                f"👨‍🏫 TUTOR PROFILE\n"
                f"Name: {tutor[2]}\n"
                f"WhatsApp: {tutor[3]}\n"
                f"Telegram: @{tutor[1] or 'no username'}\n"
                f"Subjects: {tutor[4]}\n"
                f"Levels: {tutor[5]}\n"
                f"Areas: {tutor[6]}\n"
                f"Rate: {tutor[7]}\n\n"
                f"━━━━━━━━━━━━━━━━━\n"
                f"👨‍👩‍👧 PARENT DETAILS\n"
                f"Name: {req[3]}\n"
                f"WhatsApp: {req[4]}\n"
                f"Telegram: @{req[2] or 'no username'}\n\n"
                f"✅ Contact both parties on WhatsApp to confirm the match!",
                parse_mode="Markdown"
            )

    conn.close()

    await query.edit_message_text(
        "Application sent! ✅\n\n"
        "The CognifySG team will review your application and contact you on WhatsApp to confirm the match.\n\n"
        "Use /start to go back to the menu."
    )

# ── VIEW TUTOR PROFILE ─────────────────────────────────────────────────────────
async def view_t_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    conn = db()
    t = conn.execute("SELECT * FROM tutors WHERE user_id=?", (query.from_user.id,)).fetchone()
    conn.close()

    if not t:
        await query.edit_message_text("Profile not found. Use /start to register.")
        return

    status = "🟢 Available" if t[8] else "🔴 Unavailable"
    await query.edit_message_text(
        f"*Your Profile*\n\n"
        f"Name: {t[2]}\n"
        f"WhatsApp: {t[3]}\n"
        f"Subjects: {t[4]}\n"
        f"Levels: {t[5]}\n"
        f"Areas: {t[6]}\n"
        f"Rate: {t[7]}\n"
        f"Status: {status}",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back 🔙", callback_data="back_tutor")]]),
        parse_mode="Markdown"
    )

# ── TOGGLE AVAILABILITY ────────────────────────────────────────────────────────
async def toggle_avail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    conn = db()
    row = conn.execute("SELECT available FROM tutors WHERE user_id=?", (user_id,)).fetchone()
    new = 0 if (row and row[0]) else 1
    conn.execute("UPDATE tutors SET available=? WHERE user_id=?", (new, user_id))
    conn.commit()
    conn.close()

    label = "🟢 You are now *available*" if new else "🔴 You are now *unavailable*"
    await query.edit_message_text(f"{label} for new requests.", parse_mode="Markdown")
    return await tutor_menu(update, context)

# ── MY REQUESTS (Parent) ───────────────────────────────────────────────────────
async def my_requests(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    conn = db()
    reqs = conn.execute(
        "SELECT id, subject, level, areas, budget, status FROM requests WHERE parent_id=? ORDER BY created_at DESC",
        (query.from_user.id,)
    ).fetchall()
    conn.close()

    if not reqs:
        await query.edit_message_text(
            "You have no requests yet.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back 🔙", callback_data="back_parent")]])
        )
        return

    text = "*Your Requests*\n\n"
    for r in reqs:
        icon = "✅" if r[5] == "matched" else "🟡"
        text += f"{icon} #{r[0]} — {r[1]} | {r[2]} | {r[4]} — _{r[5].capitalize()}_\n"

    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back 🔙", callback_data="back_parent")]]),
        parse_mode="Markdown"
    )

# ── BACK HANDLERS ──────────────────────────────────────────────────────────────
async def back_tutor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await tutor_menu(update, context)

async def back_parent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await parent_menu(update, context)

# ── MAIN ───────────────────────────────────────────────────────────────────────
def main():
    init_db()
    app = Application.builder().token(TOKEN).build()

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CallbackQueryHandler(post_request_start, pattern="^post_request$"),
        ],
        states={
            ROLE_SELECT: [CallbackQueryHandler(role_select,  pattern="^role_")],
            T_NAME:      [MessageHandler(filters.TEXT & ~filters.COMMAND, t_name)],
            T_PHONE:     [MessageHandler(filters.TEXT & ~filters.COMMAND, t_phone)],
            T_SUBJECTS:  [CallbackQueryHandler(t_subjects,  pattern="^tsubj\\|")],
            T_LEVELS:    [CallbackQueryHandler(t_levels,    pattern="^tlvl\\|")],
            T_AREAS:     [CallbackQueryHandler(t_areas,     pattern="^tarea\\|")],
            T_RATE:      [MessageHandler(filters.TEXT & ~filters.COMMAND, t_rate)],
            P_NAME:      [MessageHandler(filters.TEXT & ~filters.COMMAND, p_name)],
            P_PHONE:     [MessageHandler(filters.TEXT & ~filters.COMMAND, p_phone)],
            P_SUBJECT:   [CallbackQueryHandler(p_subject,   pattern="^psubj\\|")],
            P_LEVEL:     [CallbackQueryHandler(p_level,     pattern="^plvl\\|")],
            P_AREA:      [CallbackQueryHandler(p_area,      pattern="^parea\\|")],
            P_BUDGET:    [MessageHandler(filters.TEXT & ~filters.COMMAND, p_budget)],
        },
        fallbacks=[CommandHandler("start", start)],
        per_message=False,
    )

    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(browse_requests, pattern="^browse_requests$"))
    app.add_handler(CallbackQueryHandler(req_nav,         pattern="^req_(next|prev)$"))
    app.add_handler(CallbackQueryHandler(apply_request,   pattern="^apply_\\d+$"))
    app.add_handler(CallbackQueryHandler(view_t_profile,  pattern="^view_t_profile$"))
    app.add_handler(CallbackQueryHandler(toggle_avail,    pattern="^toggle_avail$"))
    app.add_handler(CallbackQueryHandler(my_requests,     pattern="^my_requests$"))
    app.add_handler(CallbackQueryHandler(back_tutor,      pattern="^back_tutor$"))
    app.add_handler(CallbackQueryHandler(back_parent,     pattern="^back_parent$"))

    print("CognifySG bot is running!")
    app.run_polling()

if __name__ == "__main__":
    main()

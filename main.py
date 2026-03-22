"""
CognifySG - Telegram Tuition Agency Bot
Compatible with python-telegram-bot==21.3 and Python 3.13
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

TOKEN         = os.environ.get("TOKEN")
ADMIN_CHAT_ID = int(os.environ.get("ADMIN_CHAT_ID", "0"))

# ── KEEPALIVE SERVER ───────────────────────────────────────────────────────────
class KeepAlive(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"CognifySG is running!")
    def log_message(self, *args):
        pass

def run_keepalive():
    HTTPServer(("0.0.0.0", 8080), KeepAlive).serve_forever()

threading.Thread(target=run_keepalive, daemon=True).start()

# ── DATABASE ───────────────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect("cognify.db")
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS tutors (
        user_id INTEGER PRIMARY KEY, username TEXT, name TEXT, phone TEXT,
        subjects TEXT, levels TEXT, areas TEXT, rate TEXT,
        available INTEGER DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    c.execute("""CREATE TABLE IF NOT EXISTS requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        parent_id INTEGER, username TEXT, name TEXT, phone TEXT,
        subject TEXT, level TEXT, areas TEXT, budget TEXT,
        status TEXT DEFAULT 'open',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    c.execute("""CREATE TABLE IF NOT EXISTS applications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tutor_id INTEGER, request_id INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(tutor_id, request_id))""")
    conn.commit()
    conn.close()

def get_db():
    return sqlite3.connect("cognify.db")

# ── CONVERSATION STATES ────────────────────────────────────────────────────────
(ROLE_SELECT,
 T_NAME, T_PHONE, T_SUBJECTS, T_LEVELS, T_AREAS, T_RATE,
 P_NAME, P_PHONE, P_SUBJECT,  P_LEVEL,  P_AREA,  P_BUDGET) = range(13)

ALL_SUBJECTS = ["Maths","English","Science","Chinese","Malay","Tamil",
                "Physics","Chemistry","Biology","History","Geography","Literature"]
ALL_LEVELS   = ["Primary 1-3","Primary 4-6","Lower Sec","Upper Sec","JC","IB/IP","Poly/ITE"]
ALL_AREAS    = ["North","South","East","West","Central","Online"]

def ms_keyboard(options, selected, prefix):
    rows, row = [], []
    for opt in options:
        tick = "✅ " if opt in selected else ""
        row.append(InlineKeyboardButton(f"{tick}{opt}", callback_data=f"{prefix}|{opt}"))
        if len(row) == 3:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("Done ✅", callback_data=f"{prefix}|DONE")])
    return InlineKeyboardMarkup(rows)

# ── /START ─────────────────────────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    kb = [[InlineKeyboardButton("I am a Tutor 👨‍🏫", callback_data="role_tutor"),
           InlineKeyboardButton("I am a Parent 👨‍👩‍👧", callback_data="role_parent")]]
    msg = "Welcome to *CognifySG* 🎓\nSingapore's smartest tuition matching service.\n\nAre you a tutor or a parent?"
    if update.message:
        await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    else:
        await update.callback_query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    return ROLE_SELECT

async def role_select(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "role_tutor":
        conn = get_db()
        exists = conn.execute("SELECT user_id FROM tutors WHERE user_id=?", (q.from_user.id,)).fetchone()
        conn.close()
        if exists:
            return await tutor_menu(update, ctx)
        await q.edit_message_text("Let's set up your tutor profile!\n\nWhat is your *full name*?", parse_mode="Markdown")
        return T_NAME
    return await parent_menu(update, ctx)

# ── TUTOR FLOW ─────────────────────────────────────────────────────────────────
async def t_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["t_name"] = update.message.text
    await update.message.reply_text("What is your *WhatsApp number*? (e.g. `91234567`)", parse_mode="Markdown")
    return T_PHONE

async def t_phone(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["t_phone"] = update.message.text
    ctx.user_data["t_subjects"] = []
    await update.message.reply_text("Select subjects you teach, then tap *Done ✅*",
        reply_markup=ms_keyboard(ALL_SUBJECTS, [], "tsubj"), parse_mode="Markdown")
    return T_SUBJECTS

async def t_subjects(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    val = q.data.split("|", 1)[1]
    if val == "DONE":
        if not ctx.user_data.get("t_subjects"):
            await q.answer("Pick at least one subject!", show_alert=True); return T_SUBJECTS
        ctx.user_data["t_levels"] = []
        await q.edit_message_text(f"Subjects: *{', '.join(ctx.user_data['t_subjects'])}*\n\nSelect levels you teach:",
            reply_markup=ms_keyboard(ALL_LEVELS, [], "tlvl"), parse_mode="Markdown")
        return T_LEVELS
    sel = ctx.user_data.get("t_subjects", [])
    sel.remove(val) if val in sel else sel.append(val)
    ctx.user_data["t_subjects"] = sel
    await q.edit_message_reply_markup(reply_markup=ms_keyboard(ALL_SUBJECTS, sel, "tsubj"))
    return T_SUBJECTS

async def t_levels(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    val = q.data.split("|", 1)[1]
    if val == "DONE":
        if not ctx.user_data.get("t_levels"):
            await q.answer("Pick at least one level!", show_alert=True); return T_LEVELS
        ctx.user_data["t_areas"] = []
        await q.edit_message_text(f"Levels: *{', '.join(ctx.user_data['t_levels'])}*\n\nSelect areas you can travel to:",
            reply_markup=ms_keyboard(ALL_AREAS, [], "tarea"), parse_mode="Markdown")
        return T_AREAS
    sel = ctx.user_data.get("t_levels", [])
    sel.remove(val) if val in sel else sel.append(val)
    ctx.user_data["t_levels"] = sel
    await q.edit_message_reply_markup(reply_markup=ms_keyboard(ALL_LEVELS, sel, "tlvl"))
    return T_LEVELS

async def t_areas(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    val = q.data.split("|", 1)[1]
    if val == "DONE":
        if not ctx.user_data.get("t_areas"):
            await q.answer("Pick at least one area!", show_alert=True); return T_AREAS
        await q.edit_message_text(f"Areas: *{', '.join(ctx.user_data['t_areas'])}*\n\nWhat is your hourly rate? (e.g. `$30/hr`)",
            parse_mode="Markdown")
        return T_RATE
    sel = ctx.user_data.get("t_areas", [])
    sel.remove(val) if val in sel else sel.append(val)
    ctx.user_data["t_areas"] = sel
    await q.edit_message_reply_markup(reply_markup=ms_keyboard(ALL_AREAS, sel, "tarea"))
    return T_AREAS

async def t_rate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["t_rate"] = update.message.text
    u = update.effective_user
    conn = get_db()
    conn.execute("INSERT OR REPLACE INTO tutors (user_id,username,name,phone,subjects,levels,areas,rate,available) VALUES (?,?,?,?,?,?,?,?,1)",
        (u.id, u.username or "", ctx.user_data["t_name"], ctx.user_data["t_phone"],
         ", ".join(ctx.user_data["t_subjects"]), ", ".join(ctx.user_data["t_levels"]),
         ", ".join(ctx.user_data["t_areas"]), ctx.user_data["t_rate"]))
    conn.commit(); conn.close()
    if ADMIN_CHAT_ID:
        await ctx.bot.send_message(ADMIN_CHAT_ID,
            f"📋 *NEW TUTOR REGISTERED*\n\n"
            f"Name: {ctx.user_data['t_name']}\nWhatsApp: {ctx.user_data['t_phone']}\n"
            f"Telegram: @{u.username or 'no username'}\n"
            f"Subjects: {', '.join(ctx.user_data['t_subjects'])}\n"
            f"Levels: {', '.join(ctx.user_data['t_levels'])}\n"
            f"Areas: {', '.join(ctx.user_data['t_areas'])}\n"
            f"Rate: {ctx.user_data['t_rate']}", parse_mode="Markdown")
    await update.message.reply_text(
        f"Profile saved! ✅\n\nName: {ctx.user_data['t_name']}\n"
        f"Subjects: {', '.join(ctx.user_data['t_subjects'])}\n"
        f"Levels: {', '.join(ctx.user_data['t_levels'])}\n"
        f"Areas: {', '.join(ctx.user_data['t_areas'])}\n"
        f"Rate: {ctx.user_data['t_rate']}\n\nYou can now browse and apply for parent requests!")
    kb = [[InlineKeyboardButton("Browse parent requests 📋", callback_data="browse_requests")],
          [InlineKeyboardButton("My profile 👤", callback_data="view_t_profile")],
          [InlineKeyboardButton("Toggle availability 🔄", callback_data="toggle_avail")]]
    await update.message.reply_text("*Tutor Dashboard* — CognifySG\n\nWhat would you like to do?",
        reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    return ConversationHandler.END

# ── TUTOR MENU ─────────────────────────────────────────────────────────────────
async def tutor_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    kb = [[InlineKeyboardButton("Browse parent requests 📋", callback_data="browse_requests")],
          [InlineKeyboardButton("My profile 👤", callback_data="view_t_profile")],
          [InlineKeyboardButton("Toggle availability 🔄", callback_data="toggle_avail")]]
    await update.callback_query.edit_message_text("*Tutor Dashboard* — CognifySG\n\nWhat would you like to do?",
        reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    return ConversationHandler.END

# ── PARENT FLOW ────────────────────────────────────────────────────────────────
async def parent_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    kb = [[InlineKeyboardButton("Post a request 📝", callback_data="post_request")],
          [InlineKeyboardButton("My requests 📋", callback_data="my_requests")]]
    msg = "*Parent Dashboard* — CognifySG\n\nWhat would you like to do?"
    if update.callback_query:
        await update.callback_query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    else:
        await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    return ConversationHandler.END

async def post_request_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    await q.edit_message_text("Let's post your tutor request!\n\nWhat is your *name*?", parse_mode="Markdown")
    return P_NAME

async def p_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["p_name"] = update.message.text
    await update.message.reply_text("What is your *WhatsApp number*? (e.g. `91234567`)", parse_mode="Markdown")
    return P_PHONE

async def p_phone(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["p_phone"] = update.message.text
    ctx.user_data["p_subject"] = []
    await update.message.reply_text("Which subject do you need help with?",
        reply_markup=ms_keyboard(ALL_SUBJECTS, [], "psubj"))
    return P_SUBJECT

async def p_subject(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    val = q.data.split("|", 1)[1]
    if val == "DONE":
        if not ctx.user_data.get("p_subject"):
            await q.answer("Pick at least one subject!", show_alert=True); return P_SUBJECT
        ctx.user_data["p_level"] = []
        await q.edit_message_text(f"Subject: *{', '.join(ctx.user_data['p_subject'])}*\n\nWhat level is your child?",
            reply_markup=ms_keyboard(ALL_LEVELS, [], "plvl"), parse_mode="Markdown")
        return P_LEVEL
    sel = ctx.user_data.get("p_subject", [])
    sel.remove(val) if val in sel else sel.append(val)
    ctx.user_data["p_subject"] = sel
    await q.edit_message_reply_markup(reply_markup=ms_keyboard(ALL_SUBJECTS, sel, "psubj"))
    return P_SUBJECT

async def p_level(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    val = q.data.split("|", 1)[1]
    if val == "DONE":
        if not ctx.user_data.get("p_level"):
            await q.answer("Pick at least one level!", show_alert=True); return P_LEVEL
        ctx.user_data["p_area"] = []
        await q.edit_message_text(f"Level: *{', '.join(ctx.user_data['p_level'])}*\n\nWhich area are you in?",
            reply_markup=ms_keyboard(ALL_AREAS, [], "parea"), parse_mode="Markdown")
        return P_AREA
    sel = ctx.user_data.get("p_level", [])
    sel.remove(val) if val in sel else sel.append(val)
    ctx.user_data["p_level"] = sel
    await q.edit_message_reply_markup(reply_markup=ms_keyboard(ALL_LEVELS, sel, "plvl"))
    return P_LEVEL

async def p_area(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    val = q.data.split("|", 1)[1]
    if val == "DONE":
        if not ctx.user_data.get("p_area"):
            await q.answer("Pick at least one area!", show_alert=True); return P_AREA
        await q.edit_message_text(f"Area: *{', '.join(ctx.user_data['p_area'])}*\n\nWhat is your budget per hour? (e.g. `$30/hr`)",
            parse_mode="Markdown")
        return P_BUDGET
    sel = ctx.user_data.get("p_area", [])
    sel.remove(val) if val in sel else sel.append(val)
    ctx.user_data["p_area"] = sel
    await q.edit_message_reply_markup(reply_markup=ms_keyboard(ALL_AREAS, sel, "parea"))
    return P_AREA

async def p_budget(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["p_budget"] = update.message.text
    u = update.effective_user
    conn = get_db()
    conn.execute("INSERT INTO requests (parent_id,username,name,phone,subject,level,areas,budget) VALUES (?,?,?,?,?,?,?,?)",
        (u.id, u.username or "", ctx.user_data["p_name"], ctx.user_data["p_phone"],
         ", ".join(ctx.user_data["p_subject"]), ", ".join(ctx.user_data["p_level"]),
         ", ".join(ctx.user_data["p_area"]), ctx.user_data["p_budget"]))
    conn.commit(); conn.close()
    if ADMIN_CHAT_ID:
        await ctx.bot.send_message(ADMIN_CHAT_ID,
            f"🆕 *NEW PARENT REQUEST*\n\n"
            f"Name: {ctx.user_data['p_name']}\nWhatsApp: {ctx.user_data['p_phone']}\n"
            f"Telegram: @{u.username or 'no username'}\n"
            f"Subject: {', '.join(ctx.user_data['p_subject'])}\n"
            f"Level: {', '.join(ctx.user_data['p_level'])}\n"
            f"Area: {', '.join(ctx.user_data['p_area'])}\n"
            f"Budget: {ctx.user_data['p_budget']}\n\n_Waiting for a tutor to apply._",
            parse_mode="Markdown")
    await update.message.reply_text(
        f"Request posted! ✅\n\nSubject: {', '.join(ctx.user_data['p_subject'])}\n"
        f"Level: {', '.join(ctx.user_data['p_level'])}\nArea: {', '.join(ctx.user_data['p_area'])}\n"
        f"Budget: {ctx.user_data['p_budget']}\n\nOur team will contact you on WhatsApp once we find a suitable tutor! 🎓")
    return ConversationHandler.END

# ── BROWSE REQUESTS ────────────────────────────────────────────────────────────
async def browse_requests(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    conn = get_db()
    reqs = conn.execute("SELECT id,subject,level,areas,budget FROM requests WHERE status='open' ORDER BY created_at DESC").fetchall()
    conn.close()
    if not reqs:
        await q.edit_message_text("No open requests right now. Check back soon!",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back 🔙", callback_data="back_tutor")]]))
        return
    ctx.user_data["req_list"] = reqs
    ctx.user_data["req_idx"] = 0
    await show_req_card(q, ctx)

async def show_req_card(q, ctx):
    reqs = ctx.user_data["req_list"]
    idx  = ctx.user_data["req_idx"]
    r    = reqs[idx]
    nav  = []
    if idx > 0: nav.append(InlineKeyboardButton("◀ Prev", callback_data="req_prev"))
    if idx < len(reqs) - 1: nav.append(InlineKeyboardButton("Next ▶", callback_data="req_next"))
    kb = []
    if nav: kb.append(nav)
    kb.append([InlineKeyboardButton("Apply ✅", callback_data=f"apply_{r[0]}")])
    kb.append([InlineKeyboardButton("Back 🔙", callback_data="back_tutor")])
    await q.edit_message_text(
        f"📋 *Request {idx+1} of {len(reqs)}*\n\n"
        f"Subject: {r[1]}\nLevel: {r[2]}\nArea: {r[3]}\nBudget: {r[4]}\nRequest ID: #{r[0]}",
        reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")

async def req_nav(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    ctx.user_data["req_idx"] += 1 if q.data == "req_next" else -1
    await show_req_card(q, ctx)

async def apply_request(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    req_id    = int(q.data.replace("apply_", ""))
    tutor_id  = q.from_user.id
    conn      = get_db()
    already   = conn.execute("SELECT id FROM applications WHERE tutor_id=? AND request_id=?", (tutor_id, req_id)).fetchone()
    if already:
        conn.close()
        await q.answer("You already applied for this request!", show_alert=True); return
    tutor = conn.execute("SELECT * FROM tutors   WHERE user_id=?", (tutor_id,)).fetchone()
    req   = conn.execute("SELECT * FROM requests WHERE id=?",      (req_id,)).fetchone()
    if tutor and req:
        conn.execute("INSERT INTO applications (tutor_id, request_id) VALUES (?,?)", (tutor_id, req_id))
        conn.commit()
        if ADMIN_CHAT_ID:
            await ctx.bot.send_message(ADMIN_CHAT_ID,
                f"🎯 *NEW APPLICATION — ACTION REQUIRED*\n\n"
                f"━━━━━━━━━━━━━━━━━\n"
                f"📌 JOB POSTING\n"
                f"Request ID: #{req[0]}\nSubject: {req[5]}\nLevel: {req[6]}\nArea: {req[7]}\nBudget: {req[8]}\n\n"
                f"━━━━━━━━━━━━━━━━━\n"
                f"👨‍🏫 TUTOR\n"
                f"Name: {tutor[2]}\nWhatsApp: {tutor[3]}\nTelegram: @{tutor[1] or 'none'}\n"
                f"Subjects: {tutor[4]}\nLevels: {tutor[5]}\nAreas: {tutor[6]}\nRate: {tutor[7]}\n\n"
                f"━━━━━━━━━━━━━━━━━\n"
                f"👨‍👩‍👧 PARENT\n"
                f"Name: {req[3]}\nWhatsApp: {req[4]}\nTelegram: @{req[2] or 'none'}\n\n"
                f"✅ Contact both on WhatsApp to confirm the match!",
                parse_mode="Markdown")
    conn.close()
    await q.edit_message_text(
        "Application sent! ✅\n\nThe CognifySG team will review and contact you on WhatsApp to confirm the match.\n\nUse /start to go back to the menu.")

# ── PROFILE / AVAILABILITY / MY REQUESTS ──────────────────────────────────────
async def view_t_profile(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    conn = get_db()
    t = conn.execute("SELECT * FROM tutors WHERE user_id=?", (q.from_user.id,)).fetchone()
    conn.close()
    if not t:
        await q.edit_message_text("Profile not found. Use /start to register."); return
    status = "🟢 Available" if t[8] else "🔴 Unavailable"
    await q.edit_message_text(
        f"*Your Profile*\n\nName: {t[2]}\nWhatsApp: {t[3]}\nSubjects: {t[4]}\nLevels: {t[5]}\nAreas: {t[6]}\nRate: {t[7]}\nStatus: {status}",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back 🔙", callback_data="back_tutor")]]),
        parse_mode="Markdown")

async def toggle_avail(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    conn = get_db()
    row = conn.execute("SELECT available FROM tutors WHERE user_id=?", (q.from_user.id,)).fetchone()
    new = 0 if (row and row[0]) else 1
    conn.execute("UPDATE tutors SET available=? WHERE user_id=?", (new, q.from_user.id))
    conn.commit(); conn.close()
    label = "🟢 You are now *available*" if new else "🔴 You are now *unavailable*"
    await q.edit_message_text(f"{label} for new requests.", parse_mode="Markdown")
    return await tutor_menu(update, ctx)

async def my_requests(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    conn = get_db()
    reqs = conn.execute("SELECT id,subject,level,areas,budget,status FROM requests WHERE parent_id=? ORDER BY created_at DESC",
        (q.from_user.id,)).fetchall()
    conn.close()
    if not reqs:
        await q.edit_message_text("You have no requests yet.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back 🔙", callback_data="back_parent")]])); return
    text = "*Your Requests*\n\n"
    for r in reqs:
        icon = "✅" if r[5] == "matched" else "🟡"
        text += f"{icon} #{r[0]} — {r[1]} | {r[2]} | {r[4]} — _{r[5].capitalize()}_\n"
    await q.edit_message_text(text,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back 🔙", callback_data="back_parent")]]),
        parse_mode="Markdown")

async def back_tutor(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    return await tutor_menu(update, ctx)

async def back_parent(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    return await parent_menu(update, ctx)

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
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

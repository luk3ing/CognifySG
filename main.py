"""
CognifySG — Production Bot v6 (FINAL - WORKING)
- Unlimited parent requests
- Async operations for speed
- Complete back buttons
- Robust error handling
- Debug logging for parent requests
"""

import os
import re
import random
import logging
import threading
import traceback
import asyncio
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove, BotCommand
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)

import db
import sheets

# ── LOGGING ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ── KEEPALIVE (single server) ─────────────────────────────────────────────────
class KeepAliveHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"CognifySG v6 running!")
    def log_message(self, *a): pass

def start_keepalive():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), KeepAliveHandler)
    server.serve_forever()

threading.Thread(target=start_keepalive, daemon=True).start()

# ── CONFIG ─────────────────────────────────────────────────────────────────────
TOKEN          = os.environ.get("TOKEN")
SUPER_ADMIN_ID = int(os.environ.get("ADMIN_CHAT_ID", "0"))
MAX_CAPTCHA    = 3
PLACEMENT_FEE  = int(os.environ.get("PLACEMENT_FEE", "40"))
TERMS_URL      = os.environ.get("TERMS_URL", "https://cognifysg.com/terms")
PRIVACY_URL    = os.environ.get("PRIVACY_URL", "https://cognifysg.com/privacy")

REJECT_TUTOR  = ["Qualifications unclear","Rate unreasonable",
                  "Incomplete profile","Duplicate account","Suspected spam"]
REJECT_PARENT = ["Budget too low","Area not covered",
                  "Subject not available","Duplicate request","Suspected spam"]

ALL_SUBJECTS = ["Maths","English","Science","Chinese","Malay","Tamil",
                "Physics","Chemistry","Biology","History","Geography","Literature"]
ALL_LEVELS   = ["Primary 1-3","Primary 4-6","Lower Sec","Upper Sec",
                "JC","IB/IP","Poly/ITE"]
ALL_AREAS    = ["North","South","East","West","Central","Online"]

# ── UI CONSTANTS ───────────────────────────────────────────────────────────────
DIV  = "────────"   # 8 dashes
DIV2 = "──────"     # 6 dashes

def hdr(icon, title):   return icon + "  *" + title + "*\n" + DIV
def fld(label, value):  return "▸ *" + label + ":* " + str(value)
def rate_str(r):        return "$" + str(r) + "/hr"

def ms_kb(options, selected, prefix, show_cancel=True):
    rows, row = [], []
    for opt in options:
        tick = "✅ " if opt in selected else "◻️ "
        row.append(InlineKeyboardButton(tick + opt, callback_data=prefix + "|" + opt))
        if len(row) == 3:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("Confirm Selection ✅", callback_data=prefix + "|DONE")])
    if show_cancel:
        rows.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
    return InlineKeyboardMarkup(rows)

# ── ASYNC HELPERS ─────────────────────────────────────────────────────────────
async def log_to_sheets_async(func, *args):
    """Run sheets operations in thread pool to avoid blocking"""
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: func(*args))
    except Exception as e:
        logger.error(f"Sheets async error: {e}")

# ── ADMIN HELPERS ──────────────────────────────────────────────────────────────
def get_admins():
    rows = db.execute("SELECT user_id FROM admins", fetch="all")
    admins = [r["user_id"] for r in rows] if rows else []
    if SUPER_ADMIN_ID not in admins:
        admins.append(SUPER_ADMIN_ID)
    return admins

def is_admin(uid):
    return bool(db.execute("SELECT 1 FROM admins WHERE user_id=%s", (uid,), fetch="one"))

async def notify_admins(bot, text, markup=None):
    """Notify all admins, fallback to super admin if no admins in DB."""
    admins = get_admins()
    if not admins:
        logger.warning("No admins registered. Set ADMIN_CHAT_ID env var.")
        if SUPER_ADMIN_ID:
            try:
                await bot.send_message(SUPER_ADMIN_ID, text, reply_markup=markup, parse_mode="Markdown")
                logger.info(f"Notification sent to super admin {SUPER_ADMIN_ID}")
            except Exception as e:
                logger.error(f"Failed to notify super admin: {e}")
        return
    for aid in admins:
        try:
            await bot.send_message(aid, text, reply_markup=markup, parse_mode="Markdown")
            logger.info(f"Notification sent to admin {aid}")
        except Exception as e:
            logger.warning(f"Notify admin {aid} failed: {e}")

# ── ERROR HANDLER ──────────────────────────────────────────────────────────────
async def error_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    err = ctx.error
    uid = update.effective_user.id if update and update.effective_user else 0

    logger.error(f"Unhandled exception for user {uid}: {err}", exc_info=True)

    try:
        db.execute(
            "INSERT INTO error_log (user_id, handler, error) VALUES (%s, %s, %s)",
            (uid, str(ctx.match), str(err)[:2000])
        )
    except Exception:
        pass

    try:
        await ctx.bot.send_message(
            SUPER_ADMIN_ID,
            hdr("⚠️", "Bot Error") + "\n\n" +
            fld("User ID", uid) + "\n" +
            fld("Error", str(err)[:300]),
            parse_mode="Markdown"
        )
    except Exception:
        pass

    if update and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ Something went wrong.\nPlease type /start to continue.\n\n_Team notified._",
                parse_mode="Markdown"
            )
        except Exception:
            pass

# ── VALIDATION ─────────────────────────────────────────────────────────────────
def valid_name(t):  return bool(re.match(r"^[A-Za-z\s\-'\.]{2,50}$", t.strip()))
def valid_phone(t): return bool(re.match(r"^[89]\d{7}$", t.strip().replace(" ", "")))
def valid_rate(t):
    t = t.strip().replace("$","").replace("/hr","").replace(" ","")
    return t.isdigit() and 0 < int(t) <= 500
def clean_rate(t):
    return int(t.strip().replace("$","").replace("/hr","").replace(" ",""))

# ── MATCHING SCORE ─────────────────────────────────────────────────────────────
def compute_score(tutor, req):
    score = 0
    t_subjects = [s.strip().lower() for s in tutor["subjects"].split(",")]
    t_levels   = [l.strip().lower() for l in tutor["levels"].split(",")]
    t_areas    = [a.strip().lower() for a in tutor["areas"].split(",")]
    r_subjects = [s.strip().lower() for s in req["subject"].split(",")]
    r_levels   = [l.strip().lower() for l in req["level"].split(",")]
    r_areas    = [a.strip().lower() for a in req["areas"].split(",")]

    if any(s in t_subjects for s in r_subjects): score += 40
    if any(l in t_levels   for l in r_levels):   score += 30
    if any(a in t_areas    for a in r_areas) or "online" in t_areas or "online" in r_areas:
        score += 20
    if tutor["rate"] <= req["budget"]: score += 10

    rating_bonus = min(int(float(tutor.get("rating_avg") or 0) * 2), 10)
    score += rating_bonus
    return min(score, 100)

# ── STATES ─────────────────────────────────────────────────────────────────────
(TERMS,
 CAPTCHA,
 ROLE_SELECT,
 T_NAME, T_PHONE, T_SUBJECTS, T_LEVELS, T_AREAS, T_RATE,
 P_NAME, P_PHONE, P_SUBJECT, P_LEVEL, P_AREA, P_BUDGET,
 EDIT_TUTOR_MENU, EDIT_NAME, EDIT_PHONE, EDIT_SUBJECTS, EDIT_LEVELS, EDIT_AREAS, EDIT_RATE) = range(22)

# ── CAPTCHA ────────────────────────────────────────────────────────────────────
def gen_captcha():
    a, b = random.randint(2, 9), random.randint(2, 9)
    ans = a + b
    wrong = random.sample([x for x in range(2, 19) if x != ans], 3)
    opts = wrong + [ans]
    random.shuffle(opts)
    return a, b, ans, opts

# ── START ENTRY ────────────────────────────────────────────────────────────────
async def send_terms(user_id, bot):
    kb = [[
        InlineKeyboardButton("📄 Read Terms", url=TERMS_URL),
        InlineKeyboardButton("🔒 Privacy Policy", url=PRIVACY_URL),
    ], [
        InlineKeyboardButton("✅ I agree to the Terms & Privacy Policy", callback_data="terms_accept")
    ]]
    try:
        await bot.send_message(
            user_id,
            hdr("📋", "Terms of Service") + "\n\n"
            "Before using *CognifySG*, please read and accept our Terms.\n\n" +
            DIV2 + "\n"
            "By tapping *I agree*, you confirm:\n"
            "▸ You are based in Singapore\n"
            "▸ You will not solicit outside this platform\n"
            "▸ We collect data per PDPA\n"
            "▸ A placement fee applies upon successful match\n\n" +
            DIV2 + "\n"
            "_You can delete all your data at any time using /deleteaccount_",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Failed to send terms to {user_id}: {e}")

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if db.execute("SELECT 1 FROM blocked WHERE user_id=%s", (uid,), fetch="one"):
        await update.message.reply_text(
            hdr("🚫", "Access Denied") + "\n\nYour account has been restricted.",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    if not db.execute("SELECT 1 FROM terms_accepted WHERE user_id=%s", (uid,), fetch="one"):
        await send_terms(uid, update.get_bot())
        return TERMS
    return await show_captcha(update, ctx)

async def start_welcome_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.message.delete()
    uid = q.from_user.id
    
    if db.execute("SELECT 1 FROM blocked WHERE user_id=%s", (uid,), fetch="one"):
        await q.message.reply_text(hdr("🚫", "Access Denied") + "\n\nAccount restricted.", parse_mode="Markdown")
        return ConversationHandler.END

    if not db.execute("SELECT 1 FROM terms_accepted WHERE user_id=%s", (uid,), fetch="one"):
        await send_terms(uid, ctx.bot)
        return TERMS
    
    a, b, ans, opts = gen_captcha()
    ctx.user_data.update({"captcha_ans": ans, "ca": a, "cb": b, "cattempts": 0})
    kb = [[InlineKeyboardButton(str(o), callback_data="cap|" + str(o)) for o in opts]]
    await ctx.bot.send_message(
        uid,
        hdr("🔐", "Security Verification") + "\n\nPlease confirm you are human.\n\n" +
        DIV2 + "\n❓ *What is " + str(a) + " + " + str(b) + "?*\n" + DIV2,
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )
    return CAPTCHA

async def welcome(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if db.execute("SELECT 1 FROM terms_accepted WHERE user_id=%s", (uid,), fetch="one"):
        await update.message.reply_text("Welcome back! Use /start to open the main menu.")
        return
    kb = [[InlineKeyboardButton("▶️ Start", callback_data="start_welcome")]]
    await update.message.reply_text(
        hdr("🎓", "Welcome to CognifySG") + "\n\nSingapore's trusted tuition matching platform.\n\nTap *Start* to begin.",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )

async def terms_accept(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    db.execute("INSERT INTO terms_accepted(user_id) VALUES(%s) ON CONFLICT DO NOTHING", (uid,))
    await q.edit_message_text(hdr("✅", "Terms Accepted") + "\n\nThank you. Let's verify you are human.", parse_mode="Markdown")
    return await show_captcha_query(q, ctx)

async def show_captcha(update, ctx):
    a, b, ans, opts = gen_captcha()
    ctx.user_data.update({"captcha_ans": ans, "ca": a, "cb": b, "cattempts": 0})
    kb = [[InlineKeyboardButton(str(o), callback_data="cap|" + str(o)) for o in opts]]
    await update.message.reply_text(
        hdr("🔐", "Security Verification") + "\n\nPlease confirm you are human.\n\n" +
        DIV2 + "\n❓ *What is " + str(a) + " + " + str(b) + "?*\n" + DIV2,
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )
    return CAPTCHA

async def show_captcha_query(q, ctx):
    a, b, ans, opts = gen_captcha()
    ctx.user_data.update({"captcha_ans": ans, "ca": a, "cb": b, "cattempts": 0})
    kb = [[InlineKeyboardButton(str(o), callback_data="cap|" + str(o)) for o in opts]]
    await q.message.reply_text(
        hdr("🔐", "Security Verification") + "\n\nPlease confirm you are human.\n\n" +
        DIV2 + "\n❓ *What is " + str(a) + " + " + str(b) + "?*\n" + DIV2,
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )
    return CAPTCHA

async def captcha_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    
    callback_data = q.data
    if not callback_data or not callback_data.startswith("cap|"):
        await q.answer("Invalid selection.", show_alert=True)
        return CAPTCHA
    
    parts = callback_data.split("|")
    if len(parts) < 2:
        await q.answer("Invalid format.", show_alert=True)
        return CAPTCHA
    
    try:
        chosen = int(parts[1])
    except ValueError:
        await q.answer("Invalid number.", show_alert=True)
        return CAPTCHA
    
    uid = q.from_user.id
    ctx.user_data["cattempts"] = ctx.user_data.get("cattempts", 0) + 1
    attempts = ctx.user_data["cattempts"]

    if chosen == ctx.user_data.get("captcha_ans"):
        kb = [[
            InlineKeyboardButton("👨‍🏫 I am a Tutor", callback_data="role_tutor"),
            InlineKeyboardButton("👨‍👩‍👧 I am a Parent", callback_data="role_parent"),
        ]]
        await q.edit_message_text(
            hdr("🎓", "Welcome to CognifySG") + "\n\nPlease identify yourself:",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
        return ROLE_SELECT

    remaining = MAX_CAPTCHA - attempts
    if remaining <= 0:
        db.execute("INSERT INTO blocked(user_id) VALUES(%s) ON CONFLICT DO NOTHING", (uid,))
        await q.edit_message_text(hdr("🚫", "Access Denied") + "\n\nMaximum attempts exceeded.", parse_mode="Markdown")
        return ConversationHandler.END

    a2, b2, ans2, opts2 = gen_captcha()
    ctx.user_data.update({"captcha_ans": ans2, "ca": a2, "cb": b2})
    kb = [[InlineKeyboardButton(str(o), callback_data="cap|" + str(o)) for o in opts2]]
    pl = "s" if remaining > 1 else ""
    await q.edit_message_text(
        hdr("🔐", "Verification Failed") + "\n\n"
        f"❌ Incorrect. ⚠️ *{remaining} attempt{pl} remaining.*\n\n" +
        DIV2 + f"\n❓ *What is {a2} + {b2}?*\n" + DIV2,
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )
    return CAPTCHA

async def role_select(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    
    if q.data == "role_tutor":
        row = db.execute("SELECT approved FROM tutors WHERE user_id=%s", (q.from_user.id,), fetch="one")
        if row:
            if row["approved"]:
                return await tutor_menu(update, ctx)
            await q.edit_message_text(
                hdr("⏳", "Approval Pending") + "\n\nYour profile is pending admin approval.",
                parse_mode="Markdown"
            )
            return ConversationHandler.END
        await q.edit_message_text(
            hdr("👨‍🏫", "Tutor Registration") + "\n\n_Step 1 of 5_ — Enter your *full name:*",
            parse_mode="Markdown"
        )
        return T_NAME
    return await parent_menu(update, ctx)

# ── TUTOR REGISTRATION ─────────────────────────────────────────────────────────
async def t_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    if not valid_name(txt):
        await update.message.reply_text("⚠️ *Invalid name.* Letters only, min 2 characters.", parse_mode="Markdown")
        return T_NAME
    ctx.user_data["t_name"] = txt
    await update.message.reply_text(
        hdr("📱", "WhatsApp Number") + "\n\n_Step 2 of 5_\n\n"
        "Enter your *8-digit SG WhatsApp number.*\n\n" +
        DIV2 + "\n✳️ Starts with 8 or 9\n✳️ Example: `91234567`",
        parse_mode="Markdown"
    )
    return T_PHONE

async def t_phone(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    if not valid_phone(txt):
        await update.message.reply_text("⚠️ *Invalid number.* 8 digits starting with 8 or 9.", parse_mode="Markdown")
        return T_PHONE
    if db.execute("SELECT 1 FROM tutors WHERE phone=%s", (txt,), fetch="one"):
        await update.message.reply_text("⚠️ This phone number is already registered.", parse_mode="Markdown")
        return T_PHONE
    ctx.user_data["t_phone"] = txt
    ctx.user_data["t_subjects"] = []
    await update.message.reply_text(
        hdr("📚", "Subjects") + "\n\n_Step 3 of 5_\n\nSelect *all subjects* you teach:",
        reply_markup=ms_kb(ALL_SUBJECTS, [], "tsubj"),
        parse_mode="Markdown"
    )
    return T_SUBJECTS

async def t_subjects(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    val = q.data.split("|", 1)[1]
    if val == "DONE":
        if not ctx.user_data.get("t_subjects"):
            await q.answer("Pick at least one subject!", show_alert=True)
            return T_SUBJECTS
        ctx.user_data["t_levels"] = []
        await q.edit_message_text(
            hdr("🎓", "Academic Levels") + "\n\n_Step 3 of 5 (cont.)_\n\n"
            f"Subjects: *{', '.join(ctx.user_data['t_subjects'])}*\n\n" +
            DIV2 + "\nSelect *levels* you teach:",
            reply_markup=ms_kb(ALL_LEVELS, [], "tlvl"),
            parse_mode="Markdown"
        )
        return T_LEVELS
    sel = ctx.user_data.get("t_subjects", [])
    sel.remove(val) if val in sel else sel.append(val)
    ctx.user_data["t_subjects"] = sel
    await q.edit_message_reply_markup(reply_markup=ms_kb(ALL_SUBJECTS, sel, "tsubj"))
    return T_SUBJECTS

async def t_levels(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    val = q.data.split("|", 1)[1]
    if val == "DONE":
        if not ctx.user_data.get("t_levels"):
            await q.answer("Pick at least one level!", show_alert=True)
            return T_LEVELS
        ctx.user_data["t_areas"] = []
        await q.edit_message_text(
            hdr("📍", "Travel Areas") + "\n\n_Step 4 of 5_\n\n"
            f"Levels: *{', '.join(ctx.user_data['t_levels'])}*\n\n" +
            DIV2 + "\nSelect *areas* you travel to:",
            reply_markup=ms_kb(ALL_AREAS, [], "tarea"),
            parse_mode="Markdown"
        )
        return T_AREAS
    sel = ctx.user_data.get("t_levels", [])
    sel.remove(val) if val in sel else sel.append(val)
    ctx.user_data["t_levels"] = sel
    await q.edit_message_reply_markup(reply_markup=ms_kb(ALL_LEVELS, sel, "tlvl"))
    return T_LEVELS

async def t_areas(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    val = q.data.split("|", 1)[1]
    if val == "DONE":
        if not ctx.user_data.get("t_areas"):
            await q.answer("Pick at least one area!", show_alert=True)
            return T_AREAS
        await q.edit_message_text(
            hdr("💰", "Hourly Rate") + "\n\n_Step 5 of 5_\n\n"
            "Enter your *hourly rate in SGD.*\n\n" +
            DIV2 + "\n✳️ Numbers only (e.g. `35`)\n✳️ Between $15–$500/hr",
            parse_mode="Markdown"
        )
        return T_RATE
    sel = ctx.user_data.get("t_areas", [])
    sel.remove(val) if val in sel else sel.append(val)
    ctx.user_data["t_areas"] = sel
    await q.edit_message_reply_markup(reply_markup=ms_kb(ALL_AREAS, sel, "tarea"))
    return T_AREAS

async def t_rate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    if not valid_rate(txt):
        await update.message.reply_text("⚠️ *Invalid rate.* Enter a number between 15 and 500.", parse_mode="Markdown")
        return T_RATE
    
    rate = clean_rate(txt)
    u = update.effective_user
    ctx.user_data["t_rate"] = rate
    auto_approve = 15 <= rate <= 150

    db.execute(
        "INSERT INTO tutors (user_id,username,name,phone,subjects,levels,areas,rate,approved) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(user_id) DO UPDATE SET "
        "name=EXCLUDED.name,phone=EXCLUDED.phone,subjects=EXCLUDED.subjects,"
        "levels=EXCLUDED.levels,areas=EXCLUDED.areas,rate=EXCLUDED.rate,approved=EXCLUDED.approved",
        (u.id, u.username or "", ctx.user_data["t_name"], ctx.user_data["t_phone"],
         ", ".join(ctx.user_data["t_subjects"]), ", ".join(ctx.user_data["t_levels"]),
         ", ".join(ctx.user_data["t_areas"]), rate, 1 if auto_approve else 0)
    )

    # Background logging
    asyncio.create_task(log_to_sheets_async(
        sheets.log_tutor, u.id, ctx.user_data["t_name"], ctx.user_data["t_phone"],
        u.username or "", ", ".join(ctx.user_data["t_subjects"]),
        ", ".join(ctx.user_data["t_levels"]), ", ".join(ctx.user_data["t_areas"]), rate
    ))

    handle = "@" + u.username if u.username else "No username"
    flag = "🤖 *Auto-approved* (rate in range)\n\n" if auto_approve else "⚠️ *Needs manual review* (rate outside range)\n\n"
    msg = (
        hdr("📋", "New Tutor Application") + "\n\n" + flag +
        fld("Name", ctx.user_data["t_name"]) + "\n" +
        fld("WhatsApp", ctx.user_data["t_phone"]) + "\n" +
        fld("Telegram", handle) + "\n" +
        fld("Subjects", ", ".join(ctx.user_data["t_subjects"])) + "\n" +
        fld("Levels", ", ".join(ctx.user_data["t_levels"])) + "\n" +
        fld("Areas", ", ".join(ctx.user_data["t_areas"])) + "\n" +
        fld("Rate", rate_str(rate)) + "\n\n" +
        DIV2 + "\n_Action required: Approve or reject._"
    )
    
    if not auto_approve:
        kb = [[
            InlineKeyboardButton("✅ Approve", callback_data=f"app_t_{u.id}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"rej_t_{u.id}"),
        ]]
        asyncio.create_task(notify_admins(update.get_bot(), msg, InlineKeyboardMarkup(kb)))
    else:
        asyncio.create_task(notify_admins(update.get_bot(), msg))
        asyncio.create_task(log_to_sheets_async(sheets.approve_tutor_sheet, u.id))

    if auto_approve:
        await update.message.reply_text(
            hdr("✅", "Profile Approved") + "\n\n"
            "Your profile has been *automatically approved!*\n\n" +
            fld("Name", ctx.user_data["t_name"]) + "\n" +
            fld("Subjects", ", ".join(ctx.user_data["t_subjects"])) + "\n" +
            fld("Rate", rate_str(rate)) + "\n\n" +
            DIV2 + "\nYou can now browse and apply for parent requests!",
            parse_mode="Markdown"
        )
        return await tutor_menu_msg(update, ctx)
    else:
        await update.message.reply_text(
            hdr("⏳", "Application Submitted") + "\n\n"
            "Your profile is *pending admin approval.*\nYou will be notified once reviewed.",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

# ── TUTOR MENU ────────────────────────────────────────────────────────────────
async def tutor_menu_msg(update, ctx):
    row = db.execute("SELECT available FROM tutors WHERE user_id=%s", (update.effective_user.id,), fetch="one")
    status = "🟢 Available" if (row and row["available"]) else "🔴 Unavailable"
    kb = [
        [InlineKeyboardButton("📋 Browse Requests", callback_data="browse_reqs")],
        [InlineKeyboardButton("📌 Applied Postings", callback_data="applied_postings")],
        [InlineKeyboardButton("👤 My Profile", callback_data="view_t_profile")],
        [InlineKeyboardButton("✏️ Edit Profile", callback_data="edit_profile")],
        [InlineKeyboardButton("🔄 Toggle Availability", callback_data="toggle_avail")],
    ]
    text = hdr("🎓", "Tutor Dashboard") + "\n\n" + fld("Status", status) + "\n\n" + DIV2 + "\n_Select an option:_"
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    return ConversationHandler.END

async def tutor_menu(update, ctx):
    q = update.callback_query
    row = db.execute("SELECT available FROM tutors WHERE user_id=%s", (update.effective_user.id,), fetch="one")
    status = "🟢 Available" if (row and row["available"]) else "🔴 Unavailable"
    kb = [
        [InlineKeyboardButton("📋 Browse Requests", callback_data="browse_reqs")],
        [InlineKeyboardButton("📌 Applied Postings", callback_data="applied_postings")],
        [InlineKeyboardButton("👤 My Profile", callback_data="view_t_profile")],
        [InlineKeyboardButton("✏️ Edit Profile", callback_data="edit_profile")],
        [InlineKeyboardButton("🔄 Toggle Availability", callback_data="toggle_avail")],
    ]
    text = hdr("🎓", "Tutor Dashboard") + "\n\n" + fld("Status", status) + "\n\n" + DIV2 + "\n_Select an option:_"
    await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    return ConversationHandler.END

async def applied_postings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id

    apps = db.execute("""
        SELECT a.request_id, r.subject, r.level, a.match_score, r.status, a.created_at
        FROM applications a
        JOIN requests r ON r.id = a.request_id
        WHERE a.tutor_id=%s
        ORDER BY a.created_at DESC
    """, (uid,), fetch="all")

    if not apps:
        kb = [[InlineKeyboardButton("🔙 Back", callback_data="back_t")]]
        await q.edit_message_text(
            hdr("📌", "Your Applied Postings") + "\n\nYou have not applied to any requests yet.",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
        return

    lines = [hdr("📌", "Your Applied Postings") + "\n"]
    for a in apps:
        status_icon = "✅ Matched" if a["status"] == "matched" else "🟡 Pending"
        lines.append(
            f"📌 *#{a['request_id']}* — {a['subject']} | {a['level']}\n"
            f"   Score: {a['match_score']}/100 | Status: {status_icon}\n"
            f"   Applied: {a['created_at'].strftime('%d %b %Y')}"
        )
        lines.append(DIV2)

    await q.edit_message_text("\n\n".join(lines), parse_mode="Markdown")

# ── EDIT TUTOR PROFILE ────────────────────────────────────────────────────────
async def edit_profile_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    tutor = db.execute("SELECT * FROM tutors WHERE user_id=%s", (uid,), fetch="one")
    if not tutor:
        await q.edit_message_text("Profile not found. Use /start.")
        return
    ctx.user_data["edit_tutor"] = tutor
    kb = [
        [InlineKeyboardButton("✏️ Name", callback_data="edit_name")],
        [InlineKeyboardButton("📱 Phone", callback_data="edit_phone")],
        [InlineKeyboardButton("📚 Subjects", callback_data="edit_subjects")],
        [InlineKeyboardButton("🎓 Levels", callback_data="edit_levels")],
        [InlineKeyboardButton("📍 Areas", callback_data="edit_areas")],
        [InlineKeyboardButton("💰 Rate", callback_data="edit_rate")],
        [InlineKeyboardButton("🔙 Back", callback_data="back_t")],
    ]
    await q.edit_message_text(
        hdr("✏️", "Edit Profile") + "\n\nSelect what you want to update:",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )
    return EDIT_TUTOR_MENU

async def edit_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text(
        hdr("✏️", "Edit Name") + "\n\nEnter your *new full name:*",
        parse_mode="Markdown"
    )
    return EDIT_NAME

async def edit_phone(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text(
        hdr("✏️", "Edit WhatsApp Number") + "\n\nEnter your *8-digit SG WhatsApp number:*\n\n"
        "✳️ Starts with 8 or 9\n✳️ Example: `91234567`",
        parse_mode="Markdown"
    )
    return EDIT_PHONE

async def edit_subjects(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    tutor = ctx.user_data.get("edit_tutor")
    if not tutor:
        return await edit_profile_menu(update, ctx)
    current = tutor["subjects"].split(",") if tutor["subjects"] else []
    ctx.user_data["edit_subjects"] = current
    await q.edit_message_text(
        hdr("✏️", "Edit Subjects") + "\n\nSelect *all subjects* you teach:",
        reply_markup=ms_kb(ALL_SUBJECTS, current, "esubj", show_cancel=False),
        parse_mode="Markdown"
    )
    return EDIT_SUBJECTS

async def edit_subjects_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    val = q.data.split("|", 1)[1]
    sel = ctx.user_data.get("edit_subjects", [])
    if val == "DONE":
        if not sel:
            await q.answer("Pick at least one subject!", show_alert=True)
            return EDIT_SUBJECTS
        uid = q.from_user.id
        db.execute("UPDATE tutors SET subjects=%s WHERE user_id=%s", (", ".join(sel), uid))
        await q.edit_message_text(hdr("✅", "Subjects Updated") + "\n\nSubjects have been updated.", parse_mode="Markdown")
        return await edit_profile_menu(update, ctx)
    else:
        sel.remove(val) if val in sel else sel.append(val)
        ctx.user_data["edit_subjects"] = sel
        await q.edit_message_reply_markup(reply_markup=ms_kb(ALL_SUBJECTS, sel, "esubj", show_cancel=False))
        return EDIT_SUBJECTS

async def edit_levels(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    tutor = ctx.user_data.get("edit_tutor")
    if not tutor:
        return await edit_profile_menu(update, ctx)
    current = tutor["levels"].split(",") if tutor["levels"] else []
    ctx.user_data["edit_levels"] = current
    await q.edit_message_text(
        hdr("✏️", "Edit Levels") + "\n\nSelect *levels* you teach:",
        reply_markup=ms_kb(ALL_LEVELS, current, "elvl", show_cancel=False),
        parse_mode="Markdown"
    )
    return EDIT_LEVELS

async def edit_levels_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    val = q.data.split("|", 1)[1]
    sel = ctx.user_data.get("edit_levels", [])
    if val == "DONE":
        if not sel:
            await q.answer("Pick at least one level!", show_alert=True)
            return EDIT_LEVELS
        uid = q.from_user.id
        db.execute("UPDATE tutors SET levels=%s WHERE user_id=%s", (", ".join(sel), uid))
        await q.edit_message_text(hdr("✅", "Levels Updated") + "\n\nLevels have been updated.", parse_mode="Markdown")
        return await edit_profile_menu(update, ctx)
    else:
        sel.remove(val) if val in sel else sel.append(val)
        ctx.user_data["edit_levels"] = sel
        await q.edit_message_reply_markup(reply_markup=ms_kb(ALL_LEVELS, sel, "elvl", show_cancel=False))
        return EDIT_LEVELS

async def edit_areas(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    tutor = ctx.user_data.get("edit_tutor")
    if not tutor:
        return await edit_profile_menu(update, ctx)
    current = tutor["areas"].split(",") if tutor["areas"] else []
    ctx.user_data["edit_areas"] = current
    await q.edit_message_text(
        hdr("✏️", "Edit Areas") + "\n\nSelect *areas* you travel to:",
        reply_markup=ms_kb(ALL_AREAS, current, "eara", show_cancel=False),
        parse_mode="Markdown"
    )
    return EDIT_AREAS

async def edit_areas_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    val = q.data.split("|", 1)[1]
    sel = ctx.user_data.get("edit_areas", [])
    if val == "DONE":
        if not sel:
            await q.answer("Pick at least one area!", show_alert=True)
            return EDIT_AREAS
        uid = q.from_user.id
        db.execute("UPDATE tutors SET areas=%s WHERE user_id=%s", (", ".join(sel), uid))
        await q.edit_message_text(hdr("✅", "Areas Updated") + "\n\nAreas have been updated.", parse_mode="Markdown")
        return await edit_profile_menu(update, ctx)
    else:
        sel.remove(val) if val in sel else sel.append(val)
        ctx.user_data["edit_areas"] = sel
        await q.edit_message_reply_markup(reply_markup=ms_kb(ALL_AREAS, sel, "eara", show_cancel=False))
        return EDIT_AREAS

async def edit_rate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text(
        hdr("✏️", "Edit Hourly Rate") + "\n\nEnter your new *hourly rate in SGD.*\n\n"
        "✳️ Numbers only (e.g. `35`)\n✳️ Between $15–$500/hr",
        parse_mode="Markdown"
    )
    return EDIT_RATE

async def update_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    if not valid_name(txt):
        await update.message.reply_text("⚠️ *Invalid name.* Letters only, min 2 characters.", parse_mode="Markdown")
        return EDIT_NAME
    uid = update.effective_user.id
    db.execute("UPDATE tutors SET name=%s WHERE user_id=%s", (txt, uid))
    await update.message.reply_text(hdr("✅", "Name Updated") + "\n\nYour name has been updated.", parse_mode="Markdown")
    return await edit_profile_menu(update, ctx)

async def update_phone(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    if not valid_phone(txt):
        await update.message.reply_text("⚠️ *Invalid number.* 8 digits starting with 8 or 9.", parse_mode="Markdown")
        return EDIT_PHONE
    uid = update.effective_user.id
    existing = db.execute("SELECT user_id FROM tutors WHERE phone=%s AND user_id!=%s", (txt, uid), fetch="one")
    if existing:
        await update.message.reply_text("⚠️ This phone number is already registered to another account.", parse_mode="Markdown")
        return EDIT_PHONE
    db.execute("UPDATE tutors SET phone=%s WHERE user_id=%s", (txt, uid))
    await update.message.reply_text(hdr("✅", "Phone Updated") + "\n\nYour WhatsApp number has been updated.", parse_mode="Markdown")
    return await edit_profile_menu(update, ctx)

async def update_rate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    if not valid_rate(txt):
        await update.message.reply_text("⚠️ *Invalid rate.* Enter a number between 15 and 500.", parse_mode="Markdown")
        return EDIT_RATE
    rate = clean_rate(txt)
    uid = update.effective_user.id
    db.execute("UPDATE tutors SET rate=%s WHERE user_id=%s", (rate, uid))
    await update.message.reply_text(hdr("✅", "Rate Updated") + "\n\nYour hourly rate has been updated.", parse_mode="Markdown")
    return await edit_profile_menu(update, ctx)

# ── PARENT FLOW (MULTIPLE REQUESTS SUPPORT) ────────────────────────────────────
async def parent_menu(update, ctx):
    """Parent dashboard with unlimited requests support"""
    uid = update.effective_user.id
    
    # Get parent info
    parent = db.execute("SELECT name FROM requests WHERE parent_id=%s LIMIT 1", (uid,), fetch="one")
    parent_name = parent["name"] if parent else "Parent"
    
    # Count existing requests
    req_count = db.execute("SELECT COUNT(*) as n FROM requests WHERE parent_id=%s", (uid,), fetch="one")["n"]
    
    kb = [
        [InlineKeyboardButton("📝 Post New Request", callback_data="post_req")],
        [InlineKeyboardButton("📋 View My Requests", callback_data="my_reqs")],
        [InlineKeyboardButton("🔙 Back to Main", callback_data="back_to_start")],
    ]
    
    text = hdr("👨‍👩‍👧", f"Welcome, {parent_name}!") + "\n\n"
    text += f"📊 You have posted *{req_count}* request(s).\n"
    text += "✅ You can post *unlimited* requests.\n\n"
    text += DIV2 + "\n_What would you like to do?_"
    
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    return ConversationHandler.END

async def post_req_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    kb = [[InlineKeyboardButton("🔙 Back", callback_data="back_p")]]
    await q.edit_message_text(
        hdr("📝", "New Tutor Request") + "\n\n_Step 1 of 5_ — Enter your *full name:*",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )
    return P_NAME

async def p_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    if not valid_name(txt):
        await update.message.reply_text("⚠️ *Invalid name.* Letters only, min 2 characters.", parse_mode="Markdown")
        return P_NAME
    ctx.user_data["p_name"] = txt
    kb = [[InlineKeyboardButton("🔙 Back", callback_data="back_p")]]
    await update.message.reply_text(
        hdr("📱", "WhatsApp Number") + "\n\n_Step 2 of 5_\n\n"
        "Enter your *8-digit SG WhatsApp number.*\n\n" +
        DIV2 + "\n✳️ Starts with 8 or 9\n✳️ No country code",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )
    return P_PHONE

async def p_phone(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    if not valid_phone(txt):
        await update.message.reply_text("⚠️ *Invalid number.* 8 digits starting with 8 or 9.", parse_mode="Markdown")
        return P_PHONE
    ctx.user_data["p_phone"] = txt
    ctx.user_data["p_subject"] = []
    await update.message.reply_text(
        hdr("📚", "Subject Required") + "\n\n_Step 3 of 5_\n\nSelect subject(s):",
        reply_markup=ms_kb(ALL_SUBJECTS, [], "psubj"),
        parse_mode="Markdown"
    )
    return P_SUBJECT

async def p_subject(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    val = q.data.split("|", 1)[1]
    if val == "DONE":
        if not ctx.user_data.get("p_subject"):
            await q.answer("Pick at least one subject!", show_alert=True)
            return P_SUBJECT
        ctx.user_data["p_level"] = []
        await q.edit_message_text(
            hdr("🎓", "Academic Level") + "\n\n_Step 4 of 5_\n\n"
            f"Subject: *{', '.join(ctx.user_data['p_subject'])}*\n\n" +
            DIV2 + "\nSelect your child's *level:*",
            reply_markup=ms_kb(ALL_LEVELS, [], "plvl"),
            parse_mode="Markdown"
        )
        return P_LEVEL
    sel = ctx.user_data.get("p_subject", [])
    sel.remove(val) if val in sel else sel.append(val)
    ctx.user_data["p_subject"] = sel
    await q.edit_message_reply_markup(reply_markup=ms_kb(ALL_SUBJECTS, sel, "psubj"))
    return P_SUBJECT

async def p_level(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    val = q.data.split("|", 1)[1]
    if val == "DONE":
        if not ctx.user_data.get("p_level"):
            await q.answer("Pick at least one level!", show_alert=True)
            return P_LEVEL
        ctx.user_data["p_area"] = []
        await q.edit_message_text(
            hdr("📍", "Location") + "\n\n_Step 4 of 5 (cont.)_\n\n"
            f"Level: *{', '.join(ctx.user_data['p_level'])}*\n\n" +
            DIV2 + "\nSelect your *area:*",
            reply_markup=ms_kb(ALL_AREAS, [], "parea"),
            parse_mode="Markdown"
        )
        return P_AREA
    sel = ctx.user_data.get("p_level", [])
    sel.remove(val) if val in sel else sel.append(val)
    ctx.user_data["p_level"] = sel
    await q.edit_message_reply_markup(reply_markup=ms_kb(ALL_LEVELS, sel, "plvl"))
    return P_LEVEL

async def p_area(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    val = q.data.split("|", 1)[1]
    if val == "DONE":
        if not ctx.user_data.get("p_area"):
            await q.answer("Pick at least one area!", show_alert=True)
            return P_AREA
        await q.edit_message_text(
            hdr("💰", "Budget") + "\n\n_Step 5 of 5_\n\n"
            "Enter your *max hourly budget in SGD.*\n\n" +
            DIV2 + "\n✳️ Numbers only (e.g. `35`)",
            parse_mode="Markdown"
        )
        return P_BUDGET
    sel = ctx.user_data.get("p_area", [])
    sel.remove(val) if val in sel else sel.append(val)
    ctx.user_data["p_area"] = sel
    await q.edit_message_reply_markup(reply_markup=ms_kb(ALL_AREAS, sel, "parea"))
    return P_AREA

# ── P_BUDGET FUNCTION (FIXED WITH DEBUG LOGGING) ──────────────────────────────
async def p_budget(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    if not valid_rate(txt):
        await update.message.reply_text("⚠️ *Invalid budget.* Enter a positive number.\n_Example: `35`_", parse_mode="Markdown")
        return P_BUDGET

    budget = clean_rate(txt)
    u = update.effective_user

    # Show loading indicator
    msg = await update.message.reply_text("⏳ Processing your request...")

    # DEBUG: Print what we're trying to insert
    logger.info(f"=== DEBUG: Attempting to insert request for user {u.id} ===")
    logger.info(f"Parent ID: {u.id}")
    logger.info(f"Username: {u.username or ''}")
    logger.info(f"Name: {ctx.user_data['p_name']}")
    logger.info(f"Phone: {ctx.user_data['p_phone']}")
    logger.info(f"Subject: {', '.join(ctx.user_data['p_subject'])}")
    logger.info(f"Level: {', '.join(ctx.user_data['p_level'])}")
    logger.info(f"Area: {', '.join(ctx.user_data['p_area'])}")
    logger.info(f"Budget: {budget}")

    conn = None
    try:
        conn = db.db()
        with conn.cursor() as cur:
            # First, check if the table exists and what columns are available
            cur.execute("""
                SELECT column_name 
                FROM information_schema.columns 
                WHERE table_name = 'requests'
            """)
            columns = [row[0] for row in cur.fetchall()]
            logger.info(f"Available columns in requests table: {columns}")
            
            # Now try the insert
            cur.execute("""
                INSERT INTO requests (parent_id, username, name, phone, subject, level, areas, budget, approved) 
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 1) RETURNING id
            """, (u.id, u.username or "", ctx.user_data["p_name"], ctx.user_data["p_phone"],
                  ", ".join(ctx.user_data["p_subject"]), ", ".join(ctx.user_data["p_level"]),
                  ", ".join(ctx.user_data["p_area"]), budget))
            req_id = cur.fetchone()[0]
            conn.commit()
            logger.info(f"✅ Success! Created request #{req_id}")
            
        await msg.delete()
        
        kb = [[InlineKeyboardButton("🔙 Back to Dashboard", callback_data="back_p")]]
        await update.message.reply_text(
            hdr("✅", "Request Live") + "\n\n"
            "Your request is now *live* and visible to tutors.\n\n" +
            fld("Subject", ", ".join(ctx.user_data["p_subject"])) + "\n" +
            fld("Level", ", ".join(ctx.user_data["p_level"])) + "\n" +
            fld("Area", ", ".join(ctx.user_data["p_area"])) + "\n" +
            fld("Budget", rate_str(budget)) + "\n\n" +
            DIV2 + "\nOur team will contact you on *WhatsApp* once a tutor is matched.\n\n"
            "You can post *another request* anytime from the dashboard.",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
        
        async def background_tasks():
            try:
                await log_to_sheets_async(
                    sheets.log_request, req_id, ctx.user_data["p_name"], ctx.user_data["p_phone"],
                    u.username or "", ", ".join(ctx.user_data["p_subject"]),
                    ", ".join(ctx.user_data["p_level"]), ", ".join(ctx.user_data["p_area"]), budget
                )
                await log_to_sheets_async(sheets.approve_request_sheet, req_id)
            except Exception as e:
                logger.error(f"Sheets logging failed: {e}")
            
            try:
                handle = "@" + u.username if u.username else "No username"
                msg_text = (
                    hdr("🆕", "New Parent Request") + "\n\n" +
                    fld("Name", ctx.user_data["p_name"]) + "\n" +
                    fld("WhatsApp", ctx.user_data["p_phone"]) + "\n" +
                    fld("Telegram", handle) + "\n" +
                    fld("Subject", ", ".join(ctx.user_data["p_subject"])) + "\n" +
                    fld("Level", ", ".join(ctx.user_data["p_level"])) + "\n" +
                    fld("Area", ", ".join(ctx.user_data["p_area"])) + "\n" +
                    fld("Budget", rate_str(budget)) + "\n\n" +
                    DIV2 + f"\n_Request #{req_id} — auto-approved and live._"
                )
                await notify_admins(update.get_bot(), msg_text)
            except Exception as e:
                logger.error(f"Admin notification failed: {e}")
        
        asyncio.create_task(background_tasks())
        
    except Exception as e:
        logger.error(f"🚨 Database insert failed: {e}")
        if conn:
            conn.rollback()
        await msg.edit_text(f"❌ Failed to save request.\n\nError: {str(e)[:200]}\n\nPlease try again or contact support.")
        return P_BUDGET
    finally:
        if conn:
            db.release(conn)
    
    return ConversationHandler.END

# ── MY REQUESTS ────────────────────────────────────────────────────────────────
async def my_reqs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show all requests for parent with option to post more"""
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    
    reqs = db.execute("""
        SELECT id, subject, level, areas, budget, status, created_at 
        FROM requests 
        WHERE parent_id=%s 
        ORDER BY created_at DESC
    """, (uid,), fetch="all")
    
    if not reqs:
        kb = [[InlineKeyboardButton("📝 Post New Request", callback_data="post_req")]]
        await q.edit_message_text(
            hdr("📋", "My Requests") + "\n\nYou haven't posted any requests yet.\n\nTap below to create your first request:",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
        return
    
    lines = [hdr("📋", f"My Requests ({len(reqs)} total)") + "\n"]
    for i, r in enumerate(reqs, 1):
        if r["status"] == "matched":
            icon = "✅ MATCHED"
        elif r["status"] == "open":
            icon = "🟡 OPEN"
        else:
            icon = "🔒 CLOSED"
        
        lines.append(f"*{i}. Request #{r['id']}*\n")
        lines.append(fld("Subject", r["subject"]))
        lines.append(fld("Level", r["level"]))
        lines.append(fld("Area", r["areas"]))
        lines.append(fld("Budget", rate_str(r["budget"])))
        lines.append(fld("Status", icon))
        lines.append(fld("Posted", r["created_at"].strftime('%d %b %Y')))
        lines.append(DIV2)
    
    lines.append("_You can post another request using the button below._")
    
    kb = [
        [InlineKeyboardButton("📝 Post New Request", callback_data="post_req")],
        [InlineKeyboardButton("🔙 Back", callback_data="back_p")],
    ]
    
    await q.edit_message_text(
        "\n\n".join(lines),
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )

# ── BROWSE REQUESTS (tutor) ────────────────────────────────────────────────────
async def browse_reqs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    
    t = db.execute("SELECT approved FROM tutors WHERE user_id=%s", (uid,), fetch="one")
    if not t or not t["approved"]:
        await q.edit_message_text(
            hdr("⏳", "Access Restricted") + "\n\nYour profile is pending admin approval.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="back_t")]]),
            parse_mode="Markdown"
        )
        return

    reqs = db.execute("""
        SELECT id, subject, level, areas, budget
        FROM requests
        WHERE status='open' AND approved=1
          AND id NOT IN (SELECT request_id FROM applications WHERE tutor_id=%s)
        ORDER BY created_at DESC
    """, (uid,), fetch="all")

    if not reqs:
        await q.edit_message_text(
            hdr("📋", "Open Requests") + "\n\nNo new requests at this time.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="back_t")]]),
            parse_mode="Markdown"
        )
        return

    ctx.user_data["rlist"] = [dict(r) for r in reqs]
    ctx.user_data["ridx"] = 0
    await show_req_card(q, ctx)

async def show_req_card(q, ctx):
    reqs = ctx.user_data["rlist"]
    idx = ctx.user_data["ridx"]
    r = reqs[idx]
    
    nav = []
    if idx > 0:
        nav.append(InlineKeyboardButton("◀ Prev", callback_data="req_prev"))
    if idx < len(reqs) - 1:
        nav.append(InlineKeyboardButton("Next ▶", callback_data="req_next"))
    
    kb = []
    if nav:
        kb.append(nav)
    kb.append([InlineKeyboardButton("✅ Apply for this Request", callback_data=f"apply_{r['id']}")])
    kb.append([InlineKeyboardButton("🔙 Back", callback_data="back_t")])
    
    await q.edit_message_text(
        hdr("📋", "Open Request") + f"\n\n_{idx + 1} of {len(reqs)}_\n\n" +
        fld("Ref", f"#{r['id']}") + "\n" +
        fld("Subject", r["subject"]) + "\n" +
        fld("Level", r["level"]) + "\n" +
        fld("Area", r["areas"]) + "\n" +
        fld("Budget", rate_str(r["budget"])) + "\n\n" +
        DIV2 + "\n_Contact details withheld until match is confirmed._",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )

async def req_nav(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ctx.user_data["ridx"] += 1 if q.data == "req_next" else -1
    await show_req_card(q, ctx)

async def apply_req(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    req_id = int(q.data.replace("apply_", ""))
    tutor_id = q.from_user.id

    if db.execute("SELECT 1 FROM applications WHERE tutor_id=%s AND request_id=%s",
                  (tutor_id, req_id), fetch="one"):
        await q.answer("⚠️ You already applied for this.", show_alert=True)
        return

    tutor = db.execute("SELECT * FROM tutors WHERE user_id=%s", (tutor_id,), fetch="one")
    req = db.execute("SELECT * FROM requests WHERE id=%s", (req_id,), fetch="one")
    
    if not tutor or not req:
        await q.answer("Error: Request not found.", show_alert=True)
        return

    score = compute_score(tutor, req)
    db.execute(
        "INSERT INTO applications (tutor_id,request_id,match_score) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
        (tutor_id, req_id, score)
    )

    app_count = db.execute("SELECT COUNT(*) as n FROM applications WHERE request_id=%s", (req_id,), fetch="one")["n"]
    asyncio.create_task(log_to_sheets_async(sheets.update_applicant_count, req_id, app_count))

    t_handle = "@" + tutor["username"] if tutor["username"] else "No username"
    p_handle = "@" + req["username"] if req["username"] else "No username"
    
    msg = (
        hdr("🎯", "New Application") + "\n\n" +
        f"📊 *Match Score: {score}/100*\n\n" +
        DIV2 + "\n📌 *JOB REQUEST*\n" +
        fld("Ref", f"#{req['id']}") + "\n" +
        fld("Subject", req["subject"]) + "\n" +
        fld("Level", req["level"]) + "\n" +
        fld("Area", req["areas"]) + "\n" +
        fld("Budget", rate_str(req["budget"])) + "\n\n" +
        DIV2 + "\n👨‍🏫 *TUTOR*\n" +
        fld("Name", tutor["name"]) + "\n" +
        fld("WhatsApp", tutor["phone"]) + "\n" +
        fld("Telegram", t_handle) + "\n" +
        fld("Subjects", tutor["subjects"]) + "\n" +
        fld("Levels", tutor["levels"]) + "\n" +
        fld("Rate", rate_str(tutor["rate"])) + "\n\n" +
        DIV2 + "\n👨‍👩‍👧 *PARENT*\n" +
        fld("Name", req["name"]) + "\n" +
        fld("WhatsApp", req["phone"]) + "\n" +
        fld("Telegram", p_handle) + "\n\n" +
        DIV2 + f"\n_Total applicants: {app_count} — use /applicants {req['id']} to compare all._"
    )
    
    kb_match = [[InlineKeyboardButton(
        f"✅ Confirm Match — #{req_id} + {tutor['name']}",
        callback_data=f"confirm_match_{req_id}_{tutor_id}"
    )]]
    
    asyncio.create_task(notify_admins(ctx.bot, msg, InlineKeyboardMarkup(kb_match)))

    await q.edit_message_text(
        hdr("✅", "Application Submitted") + "\n\n"
        "Your application has been received.\n\n"
        "Admins will review and contact you if matched.\n\n"
        "Use /myapplications or the 'Applied Postings' button to track this request.",
        parse_mode="Markdown"
    )

# ── CONFIRM MATCH (admin) ──────────────────────────────────────────────────────
async def confirm_match(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    
    if not is_admin(update.effective_user.id):
        await q.answer("⛔️ Admin only.", show_alert=True)
        return

    parts = q.data.replace("confirm_match_", "").split("_")
    req_id = int(parts[0])
    tutor_id = int(parts[1])
    actor = update.effective_user.username or str(update.effective_user.id)

    req = db.execute("SELECT * FROM requests WHERE id=%s", (req_id,), fetch="one")
    tutor = db.execute("SELECT * FROM tutors WHERE user_id=%s", (tutor_id,), fetch="one")
    
    if not req or not tutor:
        await q.answer("Record not found.", show_alert=True)
        return

    if req["status"] == "matched":
        await q.answer("⚠️ Already matched.", show_alert=True)
        return

    match_id = db.execute(
        "INSERT INTO matches (request_id,tutor_id,parent_id,confirmed_by) VALUES (%s,%s,%s,%s) RETURNING id",
        (req_id, tutor_id, req["parent_id"], update.effective_user.id),
        fetch="id"
    )
    db.execute("UPDATE requests SET status='matched', matched_tutor_id=%s WHERE id=%s", (tutor_id, req_id))
    db.execute("UPDATE tutors SET available=0 WHERE user_id=%s", (tutor_id,))

    asyncio.create_task(log_to_sheets_async(
        sheets.log_match, match_id, req_id, tutor["name"], tutor["phone"],
        req["name"], req["phone"], req["subject"], tutor["rate"], actor
    ))
    asyncio.create_task(log_to_sheets_async(sheets.log_revenue, match_id, tutor["name"], PLACEMENT_FEE))

    await q.edit_message_text(
        q.message.text + "\n\n" + DIV2 +
        f"\n✅ *Match confirmed* by @{actor}\n" +
        f"▸ *Match ID:* M{match_id}",
        parse_mode="Markdown"
    )

    # Notify tutor
    async def notify_tutor():
        try:
            await ctx.bot.send_message(
                tutor_id,
                hdr("🎉", "Match Confirmed!") + "\n\n"
                "Congratulations! You have been matched with a parent.\n\n" +
                DIV2 + "\n" +
                fld("Parent name", req["name"]) + "\n" +
                fld("WhatsApp", req["phone"]) + "\n" +
                fld("Subject needed", req["subject"]) + "\n" +
                fld("Level", req["level"]) + "\n" +
                fld("Area", req["areas"]) + "\n" +
                fld("Budget", rate_str(req["budget"])) + "\n\n" +
                DIV2 + "\n"
                f"💰 A placement fee of *${PLACEMENT_FEE}* is due to CognifySG.\n"
                "_Please contact your admin to arrange payment._\n\n"
                "_Contact the parent on WhatsApp to arrange your first lesson. Good luck!_",
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.warning(f"Could not notify tutor {tutor_id}: {e}")
    
    # Notify parent
    async def notify_parent():
        try:
            await ctx.bot.send_message(
                req["parent_id"],
                hdr("🎉", "Tutor Found!") + "\n\n"
                "We have matched you with a tutor!\n\n" +
                DIV2 + "\n" +
                fld("Tutor name", tutor["name"]) + "\n" +
                fld("WhatsApp", tutor["phone"]) + "\n" +
                fld("Subjects", tutor["subjects"]) + "\n" +
                fld("Rate", rate_str(tutor["rate"])) + "\n\n" +
                DIV2 + "\n_Contact your tutor on WhatsApp to arrange the first lesson._",
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.warning(f"Could not notify parent {req['parent_id']}: {e}")
    
    asyncio.create_task(notify_tutor())
    asyncio.create_task(notify_parent())

# ── ADMIN COMMANDS (abbreviated for length, but functional) ────────────────────
async def open_requests(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔️ Admin access required.")
        return

    reqs = db.execute("""
        SELECT r.id, r.subject, r.level, r.areas, r.budget, r.name as parent,
               COUNT(a.id) as applicants
        FROM requests r
        LEFT JOIN applications a ON a.request_id = r.id
        WHERE r.status='open' AND r.approved=1
        GROUP BY r.id ORDER BY applicants DESC, r.created_at ASC
    """, fetch="all")

    if not reqs:
        await update.message.reply_text(hdr("📋", "Open Requests") + "\n\n✅ All requests have been matched!", parse_mode="Markdown")
        return

    lines = [hdr("📋", f"Open Requests — {len(reqs)} active") + "\n"]
    for r in reqs:
        apps = r["applicants"]
        icon = "🔴" if apps == 0 else "🟡" if apps < 3 else "🟢"
        line = (
            f"{icon}  *#{r['id']}* — {r['subject']} | {r['level']} | {rate_str(r['budget'])}\n"
            f"    Parent: {r['parent']}\n"
            f"    Applicants: *{apps}*"
        )
        if apps > 0:
            line += f" — /applicants {r['id']}"
        lines.append(line)

    lines.append(f"\n{DIV2}\n🔴 No applicants  🟡 1-2 applicants  🟢 3+ applicants")
    await update.message.reply_text("\n\n".join(lines), parse_mode="Markdown")

async def view_applicants(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔️ Admin access required.")
        return
    if not ctx.args:
        await update.message.reply_text(hdr("📊", "View Applicants") + "\n\nUsage: `/applicants REQUEST_ID`", parse_mode="Markdown")
        return
    
    try:
        req_id = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("⚠️ Provide a valid request ID.")
        return

    req = db.execute("SELECT * FROM requests WHERE id=%s", (req_id,), fetch="one")
    if not req:
        await update.message.reply_text(f"⚠️ Request #{req_id} not found.")
        return

    apps = db.execute("""
        SELECT t.user_id, t.name, t.phone, t.username, t.subjects, t.levels,
               t.areas, t.rate, t.rating_avg, t.rating_count, a.match_score
        FROM applications a
        JOIN tutors t ON t.user_id = a.tutor_id
        WHERE a.request_id=%s ORDER BY a.match_score DESC
    """, (req_id,), fetch="all")

    if not apps:
        await update.message.reply_text(hdr("📊", f"Applicants for #{req_id}") + "\n\nNo applicants yet.", parse_mode="Markdown")
        return

    msg = (
        hdr("📊", f"Applicants for Request #{req_id}") + "\n\n" +
        fld("Subject", req["subject"]) + "\n" +
        fld("Level", req["level"]) + "\n" +
        fld("Area", req["areas"]) + "\n" +
        fld("Budget", rate_str(req["budget"])) + "\n" +
        fld("Parent", req["name"]) + "\n" +
        fld("Contact", req["phone"]) + "\n\n" +
        DIV + f"\n*{len(apps)} Applicant{'s' if len(apps) != 1 else ''} — ranked by match score*\n" + DIV + "\n\n"
    )

    medals = ["🥇", "🥈", "🥉"]
    kb_rows = []
    for i, a in enumerate(apps, 1):
        medal = medals[i - 1] if i <= 3 else f"{i}."
        handle = f"@{a['username']}" if a["username"] else "No username"
        rating = f"⭐ {a['rating_avg']} ({a['rating_count']})" if a["rating_count"] else "No ratings"
        msg += (
            f"{medal}  *{a['name']}*  —  Score: *{a['match_score']}/100*\n" +
            fld("WhatsApp", a["phone"]) + "\n" +
            fld("Telegram", handle) + "\n" +
            fld("Subjects", a["subjects"]) + "\n" +
            fld("Levels", a["levels"]) + "\n" +
            fld("Rate", rate_str(a["rate"])) + "\n" +
            fld("Rating", rating) + "\n"
        )
        if i < len(apps):
            msg += DIV2 + "\n"
        kb_rows.append([InlineKeyboardButton(
            f"✅ Match #{i} — {a['name']}",
            callback_data=f"confirm_match_{req_id}_{a['user_id']}"
        )])

    await update.message.reply_text(msg, parse_mode="Markdown")
    if kb_rows:
        await update.message.reply_text("Select a tutor to confirm the match:", reply_markup=InlineKeyboardMarkup(kb_rows))

# ── ADMIN APPROVAL ────────────────────────────────────────────────────────────
async def app_tutor(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(update.effective_user.id):
        return
    
    uid = int(q.data.replace("app_t_", ""))
    row = db.execute("SELECT actioned_by FROM tutors WHERE user_id=%s", (uid,), fetch="one")
    if row and row["actioned_by"]:
        await q.answer("⚠️ Already actioned.", show_alert=True)
        return
    
    actor = update.effective_user.username or str(update.effective_user.id)
    db.execute("UPDATE tutors SET approved=1, actioned_by=%s WHERE user_id=%s", (update.effective_user.id, uid))
    asyncio.create_task(log_to_sheets_async(sheets.approve_tutor_sheet, uid))
    
    await q.edit_message_text(q.message.text + f"\n\n{DIV2}\n✅ *Approved* by @{actor}", parse_mode="Markdown")
    
    try:
        await ctx.bot.send_message(uid,
            hdr("✅", "Profile Approved") + "\n\n"
            "Your profile has been *approved* by CognifySG!\n\n"
            "Use /start to browse and apply for requests.",
            parse_mode="Markdown"
        )
    except Exception:
        pass

async def rej_tutor(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(update.effective_user.id):
        return
    
    uid = int(q.data.replace("rej_t_", ""))
    row = db.execute("SELECT actioned_by FROM tutors WHERE user_id=%s", (uid,), fetch="one")
    if row and row["actioned_by"]:
        await q.answer("⚠️ Already actioned.", show_alert=True)
        return
    
    kb = [[InlineKeyboardButton(r, callback_data=f"tr_{uid}|{r}")] for r in REJECT_TUTOR]
    kb.append([InlineKeyboardButton("🔙 Cancel", callback_data=f"trc_{uid}")])
    await q.edit_message_text(
        q.message.text + f"\n\n{DIV2}\n⚠️ *Select rejection reason:*",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )

async def rej_tutor_reason(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(update.effective_user.id):
        return
    
    parts = q.data.replace("tr_", "").split("|", 1)
    uid = int(parts[0])
    reason = parts[1]
    actor = update.effective_user.username or str(update.effective_user.id)
    
    row = db.execute("SELECT actioned_by FROM tutors WHERE user_id=%s", (uid,), fetch="one")
    if row and row["actioned_by"]:
        await q.answer("⚠️ Already actioned.", show_alert=True)
        return
    
    db.execute("DELETE FROM tutors WHERE user_id=%s", (uid,))
    await q.edit_message_text(
        q.message.text.split(f"\n\n{DIV2}")[0] + f"\n\n{DIV2}\n❌ *Rejected* by @{actor}\n" + fld("Reason", reason),
        parse_mode="Markdown"
    )
    
    try:
        await ctx.bot.send_message(uid,
            hdr("❌", "Application Unsuccessful") + "\n\n" +
            fld("Reason", reason) + "\n\n_You may re-apply using /start._",
            parse_mode="Markdown"
        )
    except Exception:
        pass

async def rej_tutor_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = int(q.data.replace("trc_", ""))
    kb = [[
        InlineKeyboardButton("✅ Approve", callback_data=f"app_t_{uid}"),
        InlineKeyboardButton("❌ Reject", callback_data=f"rej_t_{uid}")
    ]]
    await q.edit_message_text(
        q.message.text.split(f"\n\n{DIV2}")[0],
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )

# ── TUTOR PROFILE & AVAILABILITY ───────────────────────────────────────────────
async def view_t_profile(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    t = db.execute("SELECT * FROM tutors WHERE user_id=%s", (q.from_user.id,), fetch="one")
    if not t:
        await q.edit_message_text("Profile not found. Use /start.")
        return
    
    status = "🟢 Available" if t["available"] else "🔴 Unavailable"
    approved = "✅ Approved" if t["approved"] else "⏳ Pending"
    rating = f"{t['rating_avg']} ({t['rating_count']} reviews)" if t["rating_count"] else "No ratings yet"
    
    await q.edit_message_text(
        hdr("👤", "My Tutor Profile") + "\n\n" +
        fld("Name", t["name"]) + "\n" +
        fld("Phone", t["phone"]) + "\n" +
        fld("Subjects", t["subjects"]) + "\n" +
        fld("Levels", t["levels"]) + "\n" +
        fld("Areas", t["areas"]) + "\n" +
        fld("Rate", rate_str(t["rate"])) + "\n" +
        fld("Rating", rating) + "\n" +
        fld("Status", status) + "\n" +
        fld("Account", approved),
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="back_t")]]),
        parse_mode="Markdown"
    )

async def toggle_avail(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    row = db.execute("SELECT available FROM tutors WHERE user_id=%s", (uid,), fetch="one")
    new = 0 if (row and row["available"]) else 1
    db.execute("UPDATE tutors SET available=%s WHERE user_id=%s", (new, uid))
    label = "🟢 You are now *Available.*" if new else "🔴 You are now *Unavailable.*"
    await q.edit_message_text(hdr("🔄", "Availability Updated") + "\n\n" + label, parse_mode="Markdown")
    return await tutor_menu(update, ctx)

# ── DELETE ACCOUNT ─────────────────────────────────────────────────────────────
async def delete_account(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    kb = [[
        InlineKeyboardButton("⚠️ Yes, delete all my data", callback_data="confirm_delete"),
        InlineKeyboardButton("Cancel", callback_data="cancel_delete"),
    ]]
    await update.message.reply_text(
        hdr("🗑️", "Delete Account") + "\n\n"
        "This will permanently delete *all your data* from CognifySG:\n\n"
        "▸ Your profile (tutor or parent)\n"
        "▸ All your requests and applications\n"
        "▸ Your acceptance of terms\n\n" +
        DIV2 + "\n⚠️ *This action cannot be undone.*",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )

async def confirm_delete(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    db.execute("DELETE FROM tutors WHERE user_id=%s", (uid,))
    db.execute("DELETE FROM requests WHERE parent_id=%s", (uid,))
    db.execute("DELETE FROM applications WHERE tutor_id=%s", (uid,))
    db.execute("DELETE FROM terms_accepted WHERE user_id=%s", (uid,))
    db.execute("DELETE FROM blocked WHERE user_id=%s", (uid,))
    await q.edit_message_text(
        hdr("✅", "Account Deleted") + "\n\n"
        "All your data has been permanently removed from CognifySG.\n\n"
        "_As required under PDPA Singapore._",
        parse_mode="Markdown"
    )

async def cancel_delete(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text("Deletion cancelled. Your account is safe.")

# ── ADMIN MANAGEMENT ───────────────────────────────────────────────────────────
async def add_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != SUPER_ADMIN_ID:
        await update.message.reply_text("⛔️ Super Admin only.")
        return
    if not ctx.args:
        await update.message.reply_text("Usage: `/addadmin TELEGRAM_ID`", parse_mode="Markdown")
        return
    try:
        new_id = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("⚠️ Provide a numeric Telegram ID.")
        return
    db.execute("INSERT INTO admins(user_id,name,added_by) VALUES(%s,'Admin',%s) ON CONFLICT DO NOTHING",
               (new_id, update.effective_user.id))
    await update.message.reply_text(hdr("✅", "Admin Added") + f"\n\nUser `{new_id}` now has admin access.", parse_mode="Markdown")
    try:
        await ctx.bot.send_message(new_id,
            hdr("🔑", "Admin Access Granted") + "\n\n"
            "You are now an admin of *CognifySG.*\n\n"
            "Commands:\n`/open` — View open requests\n"
            "`/applicants ID` — Compare applicants\n"
            "`/admin` — Dashboard stats\n"
            "`/listadmins` — View admin team",
            parse_mode="Markdown"
        )
    except Exception:
        pass

async def remove_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != SUPER_ADMIN_ID:
        await update.message.reply_text("⛔️ Super Admin only.")
        return
    if not ctx.args:
        await update.message.reply_text("Usage: `/removeadmin TELEGRAM_ID`", parse_mode="Markdown")
        return
    try:
        rid = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("⚠️ Provide a numeric ID.")
        return
    if rid == SUPER_ADMIN_ID:
        await update.message.reply_text("⛔️ Cannot remove Super Admin.")
        return
    db.execute("DELETE FROM admins WHERE user_id=%s", (rid,))
    await update.message.reply_text(hdr("✅", "Admin Removed") + f"\n\nUser `{rid}` removed.", parse_mode="Markdown")

async def list_admins(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔️ Admin access required.")
        return
    admins = db.execute("SELECT user_id, username, added_at FROM admins ORDER BY added_at", fetch="all")
    lines = []
    for a in admins:
        crown = "👑 " if a["user_id"] == SUPER_ADMIN_ID else "🔑 "
        handle = f"@{a['username']}" if a["username"] else f"`{a['user_id']}`"
        lines.append(crown + handle)
    await update.message.reply_text(
        hdr("👥", "Admin Team") + "\n\n" + "\n".join(lines) + f"\n\n{DIV2}\n_{len(admins)} admins total_",
        parse_mode="Markdown"
    )

async def admin_panel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔️ Admin access required.")
        return
    
    t_active = db.execute("SELECT COUNT(*) as n FROM tutors WHERE approved=1", fetch="one")["n"]
    t_pending = db.execute("SELECT COUNT(*) as n FROM tutors WHERE approved=0", fetch="one")["n"]
    r_open = db.execute("SELECT COUNT(*) as n FROM requests WHERE status='open' AND approved=1", fetch="one")["n"]
    r_pending = db.execute("SELECT COUNT(*) as n FROM requests WHERE approved=0", fetch="one")["n"]
    matched = db.execute("SELECT COUNT(*) as n FROM matches", fetch="one")["n"]
    apps = db.execute("SELECT COUNT(*) as n FROM applications", fetch="one")["n"]
    
    await update.message.reply_text(
        hdr("⚙️", "Admin Panel — CognifySG") + "\n\n"
        "👨‍🏫 *Tutors*\n" +
        fld("Active", t_active) + "\n" +
        fld("Pending", t_pending) + "\n\n"
        "👨‍👩‍👧 *Requests*\n" +
        fld("Open", r_open) + "\n" +
        fld("Pending", r_pending) + "\n\n" +
        fld("Total Matches", matched) + "\n" +
        fld("Total Applications", apps) + "\n\n" +
        DIV2 + "\n"
        "_Commands:_\n"
        "`/open` — Open requests dashboard\n"
        "`/applicants ID` — Compare applicants\n"
        "`/addadmin ID` — Add admin\n"
        "`/removeadmin ID` — Remove admin\n"
        "`/listadmins` — View team\n"
        "`/terms` — View terms",
        parse_mode="Markdown"
    )

async def terms_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        hdr("📋", "Terms of Service") + "\n\n" +
        fld("Terms URL", TERMS_URL) + "\n" +
        fld("Privacy URL", PRIVACY_URL) + "\n\n" +
        DIV2 + "\n"
        "Key policies:\n"
        "▸ No direct solicitation outside the platform\n"
        f"▸ Placement fee of ${PLACEMENT_FEE} per successful match\n"
        "▸ Data collected per PDPA Singapore\n"
        "▸ Users may delete their data at any time via /deleteaccount",
        parse_mode="Markdown"
    )

# ── BACK BUTTONS ──────────────────────────────────────────────────────────────
async def back_t(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    return await tutor_menu(update, ctx)

async def back_p(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    return await parent_menu(update, ctx)

async def back_to_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    kb = [[
        InlineKeyboardButton("👨‍🏫 I am a Tutor", callback_data="role_tutor"),
        InlineKeyboardButton("👨‍👩‍👧 I am a Parent", callback_data="role_parent"),
    ]]
    await q.edit_message_text(
        hdr("🎓", "CognifySG") + "\n\n"
        "Singapore's premier tuition matching platform.\n\n" +
        DIV2 + "\nPlease identify yourself to continue:\n" + DIV2,
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )
    return ROLE_SELECT

# ── USER COMMANDS ─────────────────────────────────────────────────────────────
async def profile_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    tutor = db.execute("SELECT * FROM tutors WHERE user_id=%s", (uid,), fetch="one")
    if tutor:
        status = "🟢 Available" if tutor["available"] else "🔴 Unavailable"
        await update.message.reply_text(
            hdr("👤", "Your Tutor Profile") + "\n\n" +
            fld("Name", tutor["name"]) + "\n" +
            fld("Phone", tutor["phone"]) + "\n" +
            fld("Subjects", tutor["subjects"]) + "\n" +
            fld("Levels", tutor["levels"]) + "\n" +
            fld("Areas", tutor["areas"]) + "\n" +
            fld("Rate", rate_str(tutor["rate"])) + "\n" +
            fld("Status", status) + "\n" +
            fld("Approval", "✅ Approved" if tutor["approved"] else "⏳ Pending"),
            parse_mode="Markdown"
        )
        return
    
    parent = db.execute("SELECT * FROM requests WHERE parent_id=%s LIMIT 1", (uid,), fetch="one")
    if parent:
        await update.message.reply_text(
            hdr("👨‍👩‍👧", "Your Parent Profile") + "\n\n" +
            fld("Name", parent["name"]) + "\n" +
            fld("Phone", parent["phone"]) + "\n" +
            fld("Telegram", f"@{parent['username']}" if parent["username"] else "none"),
            parse_mode="Markdown"
        )
        return
    
    await update.message.reply_text("You are not registered yet. Use /start to begin.")

async def myrequests_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    tutor = db.execute("SELECT * FROM tutors WHERE user_id=%s", (uid,), fetch="one")
    if tutor:
        apps = db.execute("""
            SELECT a.request_id, r.subject, r.level, a.match_score, r.status, a.created_at
            FROM applications a
            JOIN requests r ON r.id = a.request_id
            WHERE a.tutor_id=%s
            ORDER BY a.created_at DESC
        """, (uid,), fetch="all")
        if not apps:
            await update.message.reply_text("You haven't applied for any requests.")
            return
        lines = [hdr("📋", "Your Applications") + "\n"]
        for a in apps:
            status_icon = "✅ Matched" if a["status"] == "matched" else "🟡 Pending"
            lines.append(
                f"📌 *#{a['request_id']}* — {a['subject']} | {a['level']}\n"
                f"   Score: {a['match_score']}/100 | Status: {status_icon}\n"
                f"   Applied: {a['created_at'].strftime('%d %b %Y')}"
            )
        await update.message.reply_text("\n\n".join(lines), parse_mode="Markdown")
        return
    
    reqs = db.execute(
        "SELECT id, subject, level, status, created_at FROM requests WHERE parent_id=%s ORDER BY created_at DESC",
        (uid,), fetch="all"
    )
    if not reqs:
        await update.message.reply_text("You haven't posted any requests.")
        return
    lines = [hdr("📋", "Your Requests") + "\n"]
    for r in reqs:
        icon = "✅" if r["status"] == "matched" else "🟡"
        lines.append(f"{icon}  *#{r['id']}* — {r['subject']} | {r['level']}  ({r['status']})")
    await update.message.reply_text("\n\n".join(lines), parse_mode="Markdown")

async def myapplications_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await myrequests_cmd(update, ctx)

async def cancel_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text(
        "Operation cancelled. Use /start to begin again.",
        reply_markup=ReplyKeyboardRemove()
    )
    return ConversationHandler.END

# ── POST INIT ─────────────────────────────────────────────────────────────────
async def post_init(app: Application):
    """Set bot commands so Telegram shows the menu button automatically."""
    user_commands = [
        BotCommand("start",          "Open main menu"),
        BotCommand("profile",        "View your profile"),
        BotCommand("myrequests",     "View your requests / applications"),
        BotCommand("deleteaccount",  "Delete all your data (PDPA)"),
        BotCommand("cancel",         "Cancel current operation"),
        BotCommand("terms",          "View Terms & Privacy Policy"),
    ]
    await app.bot.set_my_commands(user_commands)
    logger.info("Bot commands registered.")

# ── MAIN ───────────────────────────────────────────────────────────────────────
def main():
    db.init_db()
    db.execute(
        "INSERT INTO admins(user_id,name,added_by) VALUES(%s,'Super Admin',%s) ON CONFLICT DO NOTHING",
        (SUPER_ADMIN_ID, SUPER_ADMIN_ID)
    )

    app = Application.builder().token(TOKEN).concurrent_updates(True).post_init(post_init).build()

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CallbackQueryHandler(start_welcome_callback, pattern="^start_welcome$"),
            CallbackQueryHandler(post_req_start, pattern="^post_req$"),
        ],
        states={
            TERMS: [CallbackQueryHandler(terms_accept, pattern="^terms_accept$")],
            CAPTCHA: [CallbackQueryHandler(captcha_cb, pattern="^cap\\|")],
            ROLE_SELECT: [CallbackQueryHandler(role_select, pattern="^role_")],
            T_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, t_name)],
            T_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, t_phone)],
            T_SUBJECTS: [CallbackQueryHandler(t_subjects, pattern="^tsubj\\|")],
            T_LEVELS: [CallbackQueryHandler(t_levels, pattern="^tlvl\\|")],
            T_AREAS: [CallbackQueryHandler(t_areas, pattern="^tarea\\|")],
            T_RATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, t_rate)],
            P_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, p_name)],
            P_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, p_phone)],
            P_SUBJECT: [CallbackQueryHandler(p_subject, pattern="^psubj\\|")],
            P_LEVEL: [CallbackQueryHandler(p_level, pattern="^plvl\\|")],
            P_AREA: [CallbackQueryHandler(p_area, pattern="^parea\\|")],
            P_BUDGET: [MessageHandler(filters.TEXT & ~filters.COMMAND, p_budget)],
            EDIT_TUTOR_MENU: [CallbackQueryHandler(edit_profile_menu, pattern="^edit_profile$")],
            EDIT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, update_name)],
            EDIT_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, update_phone)],
            EDIT_SUBJECTS: [CallbackQueryHandler(edit_subjects_cb, pattern="^esubj\\|")],
            EDIT_LEVELS: [CallbackQueryHandler(edit_levels_cb, pattern="^elvl\\|")],
            EDIT_AREAS: [CallbackQueryHandler(edit_areas_cb, pattern="^eara\\|")],
            EDIT_RATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, update_rate)],
        },
        fallbacks=[CommandHandler("cancel", cancel_cmd), CommandHandler("start", start)],
        per_message=False,
        allow_reentry=True,
    )

    app.add_handler(conv)

    # User commands
    app.add_handler(CommandHandler("profile", profile_cmd))
    app.add_handler(CommandHandler("myrequests", myrequests_cmd))
    app.add_handler(CommandHandler("myapplications", myapplications_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))

    # Admin commands
    app.add_handler(CommandHandler("open",        open_requests))
    app.add_handler(CommandHandler("applicants",  view_applicants))
    app.add_handler(CommandHandler("admin",       admin_panel))
    app.add_handler(CommandHandler("addadmin",    add_admin))
    app.add_handler(CommandHandler("removeadmin", remove_admin))
    app.add_handler(CommandHandler("listadmins",  list_admins))
    app.add_handler(CommandHandler("deleteaccount", delete_account))
    app.add_handler(CommandHandler("terms",       terms_cmd))

    # Callback handlers
    app.add_handler(CallbackQueryHandler(browse_reqs, pattern="^browse_reqs$"))
    app.add_handler(CallbackQueryHandler(applied_postings, pattern="^applied_postings$"))
    app.add_handler(CallbackQueryHandler(edit_profile_menu, pattern="^edit_profile$"))
    app.add_handler(CallbackQueryHandler(edit_name, pattern="^edit_name$"))
    app.add_handler(CallbackQueryHandler(edit_phone, pattern="^edit_phone$"))
    app.add_handler(CallbackQueryHandler(edit_subjects, pattern="^edit_subjects$"))
    app.add_handler(CallbackQueryHandler(edit_levels, pattern="^edit_levels$"))
    app.add_handler(CallbackQueryHandler(edit_areas, pattern="^edit_areas$"))
    app.add_handler(CallbackQueryHandler(edit_rate, pattern="^edit_rate$"))
    app.add_handler(CallbackQueryHandler(req_nav, pattern="^req_(next|prev)$"))
    app.add_handler(CallbackQueryHandler(apply_req, pattern="^apply_\\d+$"))
    app.add_handler(CallbackQueryHandler(confirm_match, pattern="^confirm_match_"))
    app.add_handler(CallbackQueryHandler(view_t_profile, pattern="^view_t_profile$"))
    app.add_handler(CallbackQueryHandler(toggle_avail, pattern="^toggle_avail$"))
    app.add_handler(CallbackQueryHandler(my_reqs, pattern="^my_reqs$"))
    app.add_handler(CallbackQueryHandler(back_t, pattern="^back_t$"))
    app.add_handler(CallbackQueryHandler(back_p, pattern="^back_p$"))
    app.add_handler(CallbackQueryHandler(back_to_start, pattern="^back_to_start$"))
    app.add_handler(CallbackQueryHandler(app_tutor, pattern="^app_t_\\d+$"))
    app.add_handler(CallbackQueryHandler(rej_tutor, pattern="^rej_t_\\d+$"))
    app.add_handler(CallbackQueryHandler(rej_tutor_reason, pattern="^tr_"))
    app.add_handler(CallbackQueryHandler(rej_tutor_cancel, pattern="^trc_"))
    app.add_handler(CallbackQueryHandler(confirm_delete, pattern="^confirm_delete$"))
    app.add_handler(CallbackQueryHandler(cancel_delete, pattern="^cancel_delete$"))

    # Welcome handler
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, welcome))

    app.add_error_handler(error_handler)

    logger.info("CognifySG v6 is starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    main()

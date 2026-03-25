"""
CognifySG — Production Bot v6
Enhanced UI: edit profile for tutors, applied postings, multiple parent requests,
back buttons, and improved error handling.
"""
import os
import re
import random
import logging
import threading
import traceback
from http.server import HTTPServer, BaseHTTPRequestHandler

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
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

# ── UI CONSTANTS (SHORTENED) ───────────────────────────────────────────────────
DIV  = "────────"   # 8 dashes
DIV2 = "──────"     # 6 dashes

def hdr(icon, title):   return icon + "  *" + title + "*\n" + DIV
def fld(label, value):  return "▸ *" + label + ":* " + str(value)
def rate_str(r):        return "$" + str(r) + "/hr"

def ms_kb(options, selected, prefix):
    rows, row = [], []
    for opt in options:
        tick = "✅ " if opt in selected else "◻️ "
        row.append(InlineKeyboardButton(tick + opt, callback_data=prefix + "|" + opt))
        if len(row) == 3:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("Confirm Selection ✅", callback_data=prefix + "|DONE")])
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
    return InlineKeyboardMarkup(rows)

# ── KEEPALIVE ──────────────────────────────────────────────────────────────────
class _KA(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers()
        self.wfile.write(b"CognifySG v6 running!")
    def log_message(self, *a): pass

threading.Thread(
    target=lambda: HTTPServer(("0.0.0.0", 8080), _KA).serve_forever(),
    daemon=True
).start()

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
    admins = get_admins()
    if not admins:
        logger.warning("No admins registered. Set ADMIN_CHAT_ID env var.")
        return
    for aid in admins:
        try:
            await bot.send_message(aid, text, reply_markup=markup, parse_mode="Markdown")
        except Exception as e:
            logger.warning("Notify admin %s failed: %s", aid, e)

# ── ERROR HANDLER ──────────────────────────────────────────────────────────────
async def error_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    err   = ctx.error
    trace = "".join(traceback.format_exception(type(err), err, err.__traceback__))
    uid   = update.effective_user.id if update and update.effective_user else 0
    handler_name = ctx.update_queue.qsize()

    logger.error("Unhandled exception for user %s: %s", uid, err)

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
            fld("Error",   str(err)[:300]) + "\n\n" +
            "_Check /errorlog in the dashboard for full trace._",
            parse_mode="Markdown"
        )
    except Exception:
        pass

    if update and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️  Something went wrong on our end.\n"
                "Please type /start to continue.\n\n"
                "_Our team has been notified._",
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
 P_NAME, P_PHONE, P_SUBJECT,  P_LEVEL,  P_AREA,  P_BUDGET,
 EDIT_TUTOR_MENU, EDIT_NAME, EDIT_PHONE, EDIT_SUBJECTS, EDIT_LEVELS, EDIT_AREAS, EDIT_RATE) = range(22)

# ── CAPTCHA ────────────────────────────────────────────────────────────────────
def gen_captcha():
    a, b  = random.randint(2, 9), random.randint(2, 9)
    ans   = a + b
    wrong = random.sample([x for x in range(2, 19) if x != ans], 3)
    opts  = wrong + [ans]; random.shuffle(opts)
    return a, b, ans, opts

# ── START ENTRY ────────────────────────────────────────────────────────────────
async def send_terms(user_id, bot):
    """Send the terms message to a user (used by both /start and button)."""
    kb = [[
        InlineKeyboardButton("📄 Read Terms", url=TERMS_URL),
        InlineKeyboardButton("🔒 Privacy Policy", url=PRIVACY_URL),
    ], [
        InlineKeyboardButton("✅  I agree to the Terms & Privacy Policy",
                             callback_data="terms_accept")
    ]]
    try:
        await bot.send_message(
            user_id,
            hdr("📋", "Terms of Service") + "\n\n"
            "Before using *CognifySG*, please read and accept our Terms of Service and Privacy Policy.\n\n" +
            DIV2 + "\n"
            "By tapping *I agree*, you confirm:\n"
            "▸ You are based in Singapore\n"
            "▸ You will not solicit tutors/parents outside this platform\n"
            "▸ We may collect and use your data per our Privacy Policy (PDPA compliant)\n"
            "▸ A placement fee applies upon successful match\n\n" +
            DIV2 + "\n"
            "_You can delete all your data at any time using /deleteaccount_",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error("Failed to send terms to %s: %s", user_id, e)

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Entry point for /start command."""
    uid = update.effective_user.id
    if db.execute("SELECT 1 FROM blocked WHERE user_id=%s", (uid,), fetch="one"):
        await update.message.reply_text(
            hdr("🚫", "Access Denied") + "\n\n"
            "Your account has been restricted.\n"
            "_Contact support if you believe this is an error._",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    if not db.execute("SELECT 1 FROM terms_accepted WHERE user_id=%s", (uid,), fetch="one"):
        await send_terms(uid, update.get_bot())
        return TERMS
    return await show_captcha(update, ctx)

async def start_welcome_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle the inline 'Start' button."""
    q = update.callback_query
    await q.answer()
    await q.message.delete()  # remove the welcome message
    # Now start the real flow
    uid = q.from_user.id
    if db.execute("SELECT 1 FROM blocked WHERE user_id=%s", (uid,), fetch="one"):
        await q.message.reply_text(
            hdr("🚫", "Access Denied") + "\n\n"
            "Your account has been restricted.\n"
            "_Contact support if you believe this is an error._",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    if not db.execute("SELECT 1 FROM terms_accepted WHERE user_id=%s", (uid,), fetch="one"):
        await send_terms(uid, ctx.bot)
        return TERMS
    # If already accepted, just go to captcha
    a, b, ans, opts = gen_captcha()
    ctx.user_data.update({"captcha_ans": ans, "ca": a, "cb": b, "cattempts": 0})
    kb = [[InlineKeyboardButton(str(o), callback_data="cap|" + str(o)) for o in opts]]
    await ctx.bot.send_message(
        uid,
        hdr("🔐", "Security Verification") + "\n\n"
        "Please confirm you are human.\n\n" +
        DIV2 + "\n❓  *What is  " + str(a) + " + " + str(b) + "?*\n" + DIV2,
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )
    return CAPTCHA

# ── WELCOME HANDLER ───────────────────────────────────────────────────────────
async def welcome(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Send a welcome message with Start button for users who haven't started."""
    uid = update.effective_user.id
    if db.execute("SELECT 1 FROM terms_accepted WHERE user_id=%s", (uid,), fetch="one"):
        # User already accepted terms, they are probably in a conversation or just idle
        # We can ignore or show a help message.
        await update.message.reply_text(
            "Welcome back! Use /start to open the main menu."
        )
        return
    # New user: show welcome with start button
    kb = [[InlineKeyboardButton("▶️ Start", callback_data="start_welcome")]]
    await update.message.reply_text(
        hdr("🎓", "Welcome to CognifySG") + "\n\n"
        "Singapore's trusted tuition matching platform.\n\n"
        "Tap *Start* to begin your journey.",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )

# ── TERMS ACCEPT ──────────────────────────────────────────────────────────────
async def terms_accept(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    uid = q.from_user.id
    db.execute("INSERT INTO terms_accepted(user_id) VALUES(%s) ON CONFLICT DO NOTHING", (uid,))
    await q.edit_message_text(
        hdr("✅", "Terms Accepted") + "\n\nThank you. Let's verify you are human.",
        parse_mode="Markdown"
    )
    return await show_captcha_query(q, ctx)

async def show_captcha(update, ctx):
    a, b, ans, opts = gen_captcha()
    ctx.user_data.update({"captcha_ans": ans, "ca": a, "cb": b, "cattempts": 0})
    kb = [[InlineKeyboardButton(str(o), callback_data="cap|" + str(o)) for o in opts]]
    await update.message.reply_text(
        hdr("🔐", "Security Verification") + "\n\n"
        "Please confirm you are human.\n\n" +
        DIV2 + "\n❓  *What is  " + str(a) + " + " + str(b) + "?*\n" + DIV2,
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )
    return CAPTCHA

async def show_captcha_query(q, ctx):
    a, b, ans, opts = gen_captcha()
    ctx.user_data.update({"captcha_ans": ans, "ca": a, "cb": b, "cattempts": 0})
    kb = [[InlineKeyboardButton(str(o), callback_data="cap|" + str(o)) for o in opts]]
    await q.message.reply_text(
        hdr("🔐", "Security Verification") + "\n\n"
        "Please confirm you are human.\n\n" +
        DIV2 + "\n❓  *What is  " + str(a) + " + " + str(b) + "?*\n" + DIV2,
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )
    return CAPTCHA

async def captcha_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    
    # Safely check callback data
    callback_data = q.data
    if not callback_data or not callback_data.startswith("cap|"):
        await q.answer("Invalid selection.", show_alert=True)
        return CAPTCHA
    
    # Extract the chosen number
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
            InlineKeyboardButton("👨‍🏫  I am a Tutor",  callback_data="role_tutor"),
            InlineKeyboardButton("👨‍👩‍👧  I am a Parent", callback_data="role_parent"),
        ]]
        await q.edit_message_text(
            hdr("🎓", "Welcome to CognifySG") + "\n\n"
            "Singapore's premier tuition matching platform.\n\n" +
            DIV2 + "\nPlease identify yourself to continue:\n" + DIV2,
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
        return ROLE_SELECT

    remaining = MAX_CAPTCHA - attempts
    if remaining <= 0:
        db.execute("INSERT INTO blocked(user_id) VALUES(%s) ON CONFLICT DO NOTHING", (uid,))
        await q.edit_message_text(
            hdr("🚫", "Access Denied") + "\n\nMaximum attempts exceeded.",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    a2, b2, ans2, opts2 = gen_captcha()
    ctx.user_data.update({"captcha_ans": ans2, "ca": a2, "cb": b2})
    kb = [[InlineKeyboardButton(str(o), callback_data="cap|" + str(o)) for o in opts2]]
    pl = "s" if remaining > 1 else ""
    await q.edit_message_text(
        hdr("🔐", "Verification Failed") + "\n\n"
        "❌  Incorrect.  ⚠️  *" + str(remaining) + " attempt" + pl + " remaining.*\n\n" +
        DIV2 + "\n❓  *What is  " + str(a2) + " + " + str(b2) + "?*\n" + DIV2,
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )
    return CAPTCHA

# ── ROLE SELECT ────────────────────────────────────────────────────────────────
async def role_select(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if q.data == "role_tutor":
        row = db.execute("SELECT approved FROM tutors WHERE user_id=%s", (q.from_user.id,), fetch="one")
        if row:
            if row["approved"]: return await tutor_menu(update, ctx)
            await q.edit_message_text(
                hdr("⏳", "Approval Pending") + "\n\n" +
                fld("Status", "🟡 Pending Admin Approval") + "\n\n"
                "_You will be notified once approved._",
                parse_mode="Markdown"
            )
            return ConversationHandler.END
        await q.edit_message_text(
            hdr("👨‍🏫", "Tutor Registration") + "\n\n_Step 1 of 5_  —  Enter your *full name:*",
            parse_mode="Markdown"
        )
        return T_NAME
    return await parent_menu(update, ctx)

# ── TUTOR REGISTRATION ─────────────────────────────────────────────────────────
async def t_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    if not valid_name(txt):
        await update.message.reply_text(
            "⚠️  *Invalid name.* Letters only, min 2 characters.", parse_mode="Markdown")
        return T_NAME
    ctx.user_data["t_name"] = txt
    await update.message.reply_text(
        hdr("📱", "WhatsApp Number") + "\n\n_Step 2 of 5_\n\n"
        "Enter your *8-digit SG WhatsApp number.*\n\n" +
        DIV2 + "\n✳️  Starts with 8 or 9\n✳️  No country code\n✳️  Example: `91234567`",
        parse_mode="Markdown"
    )
    return T_PHONE

async def t_phone(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    if not valid_phone(txt):
        await update.message.reply_text(
            "⚠️  *Invalid number.* 8 digits starting with 8 or 9.\n_Example: `91234567`_",
            parse_mode="Markdown")
        return T_PHONE
    if db.execute("SELECT 1 FROM tutors WHERE phone=%s", (txt,), fetch="one"):
        await update.message.reply_text(
            "⚠️  This phone number is already registered.\n_Each number can only be used once._",
            parse_mode="Markdown")
        return T_PHONE
    ctx.user_data["t_phone"]    = txt
    ctx.user_data["t_subjects"] = []
    await update.message.reply_text(
        hdr("📚", "Subjects") + "\n\n_Step 3 of 5_\n\n"
        "Select *all subjects* you teach:",
        reply_markup=ms_kb(ALL_SUBJECTS, [], "tsubj"),
        parse_mode="Markdown"
    )
    return T_SUBJECTS

async def t_subjects(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    val = q.data.split("|", 1)[1]
    if val == "DONE":
        if not ctx.user_data.get("t_subjects"):
            await q.answer("Pick at least one subject!", show_alert=True); return T_SUBJECTS
        ctx.user_data["t_levels"] = []
        await q.edit_message_text(
            hdr("🎓", "Academic Levels") + "\n\n_Step 3 of 5 (cont.)_\n\n"
            "Subjects: *" + ", ".join(ctx.user_data["t_subjects"]) + "*\n\n" +
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
    q = update.callback_query; await q.answer()
    val = q.data.split("|", 1)[1]
    if val == "DONE":
        if not ctx.user_data.get("t_levels"):
            await q.answer("Pick at least one level!", show_alert=True); return T_LEVELS
        ctx.user_data["t_areas"] = []
        await q.edit_message_text(
            hdr("📍", "Travel Areas") + "\n\n_Step 4 of 5_\n\n"
            "Levels: *" + ", ".join(ctx.user_data["t_levels"]) + "*\n\n" +
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
    q = update.callback_query; await q.answer()
    val = q.data.split("|", 1)[1]
    if val == "DONE":
        if not ctx.user_data.get("t_areas"):
            await q.answer("Pick at least one area!", show_alert=True); return T_AREAS
        await q.edit_message_text(
            hdr("💰", "Hourly Rate") + "\n\n_Step 5 of 5_\n\n"
            "Enter your *hourly rate in SGD.*\n\n" +
            DIV2 + "\n✳️  Numbers only (e.g. `35`)\n✳️  Between $15–$500/hr",
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
        await update.message.reply_text(
            "⚠️  *Invalid rate.* Enter a number between 15 and 500.\n_Example: `35`_",
            parse_mode="Markdown")
        return T_RATE
    rate = clean_rate(txt)
    u    = update.effective_user
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

    sheets.log_tutor(u.id, ctx.user_data["t_name"], ctx.user_data["t_phone"],
                     u.username or "", ", ".join(ctx.user_data["t_subjects"]),
                     ", ".join(ctx.user_data["t_levels"]),
                     ", ".join(ctx.user_data["t_areas"]), rate)

    handle = "@" + u.username if u.username else "No username"
    kb = [[
        InlineKeyboardButton("✅  Approve", callback_data="app_t_" + str(u.id)),
        InlineKeyboardButton("❌  Reject",  callback_data="rej_t_" + str(u.id)),
    ]]
    flag = "🤖 *Auto-approved* (rate in range)\n\n" if auto_approve else "⚠️ *Needs manual review* (rate outside range)\n\n"
    msg = (
        hdr("📋", "New Tutor Application") + "\n\n" + flag +
        fld("Name",     ctx.user_data["t_name"])                   + "\n" +
        fld("WhatsApp", ctx.user_data["t_phone"])                  + "\n" +
        fld("Telegram", handle)                                    + "\n" +
        fld("Subjects", ", ".join(ctx.user_data["t_subjects"]))    + "\n" +
        fld("Levels",   ", ".join(ctx.user_data["t_levels"]))      + "\n" +
        fld("Areas",    ", ".join(ctx.user_data["t_areas"]))       + "\n" +
        fld("Rate",     rate_str(rate))                            + "\n\n" +
        DIV2 + "\n_Action required: Approve or reject._"
    )
    await notify_admins(update.get_bot(), msg, InlineKeyboardMarkup(kb) if not auto_approve else None)

    if auto_approve:
        sheets.approve_tutor_sheet(u.id)
        await update.message.reply_text(
            hdr("✅", "Profile Approved") + "\n\n"
            "Your profile has been *automatically approved!*\n\n" +
            fld("Name",     ctx.user_data["t_name"])                + "\n" +
            fld("Subjects", ", ".join(ctx.user_data["t_subjects"])) + "\n" +
            fld("Rate",     rate_str(rate))                         + "\n\n" +
            DIV2 + "\nYou can now browse and apply for parent requests!",
            parse_mode="Markdown"
        )
        return await tutor_menu_msg(update, ctx)
    else:
        await update.message.reply_text(
            hdr("⏳", "Application Submitted") + "\n\n"
            "Your profile is *pending admin approval.*\n"
            "You will be notified once reviewed.",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

# ── TUTOR MENU (with new buttons) ─────────────────────────────────────────────
async def tutor_menu_msg(update, ctx):
    row = db.execute("SELECT available FROM tutors WHERE user_id=%s",
                     (update.effective_user.id,), fetch="one")
    status = "🟢  Available" if (row and row["available"]) else "🔴  Unavailable"
    kb = [
        [InlineKeyboardButton("📋  Browse Requests",      callback_data="browse_reqs")],
        [InlineKeyboardButton("📌  Applied Postings",     callback_data="applied_postings")],
        [InlineKeyboardButton("👤  My Profile",           callback_data="view_t_profile")],
        [InlineKeyboardButton("✏️  Edit Profile",          callback_data="edit_profile")],
        [InlineKeyboardButton("🔄  Toggle Availability",  callback_data="toggle_avail")],
    ]
    text = hdr("🎓", "Tutor Dashboard") + "\n\n" + fld("Status", status) + "\n\n" + DIV2 + "\n_Select an option:_"
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    return ConversationHandler.END

async def tutor_menu(update, ctx):
    q = update.callback_query
    row = db.execute("SELECT available FROM tutors WHERE user_id=%s",
                     (update.effective_user.id,), fetch="one")
    status = "🟢  Available" if (row and row["available"]) else "🔴  Unavailable"
    kb = [
        [InlineKeyboardButton("📋  Browse Requests",      callback_data="browse_reqs")],
        [InlineKeyboardButton("📌  Applied Postings",     callback_data="applied_postings")],
        [InlineKeyboardButton("👤  My Profile",           callback_data="view_t_profile")],
        [InlineKeyboardButton("✏️  Edit Profile",          callback_data="edit_profile")],
        [InlineKeyboardButton("🔄  Toggle Availability",  callback_data="toggle_avail")],
    ]
    text = hdr("🎓", "Tutor Dashboard") + "\n\n" + fld("Status", status) + "\n\n" + DIV2 + "\n_Select an option:_"
    await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    return ConversationHandler.END

# ── APPLIED POSTINGS (for tutors) ─────────────────────────────────────────────
async def applied_postings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id

    # Get all applications with request details
    apps = db.execute("""
        SELECT a.request_id, r.subject, r.level, a.match_score, r.status, a.created_at
        FROM applications a
        JOIN requests r ON r.id = a.request_id
        WHERE a.tutor_id=%s
        ORDER BY a.created_at DESC
    """, (uid,), fetch="all")

    if not apps:
        kb = [[InlineKeyboardButton("🔙  Back", callback_data="back_t")]]
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
            f"   Score: {a['match_score']}/100  |  Status: {status_icon}\n"
            f"   Applied: {a['created_at'].strftime('%d %b %Y')}"
        )
        lines.append(DIV2)

    lines.append("_Tap /start to return to dashboard_")
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
        [InlineKeyboardButton("✏️  Name", callback_data="edit_name")],
        [InlineKeyboardButton("📱  Phone", callback_data="edit_phone")],
        [InlineKeyboardButton("📚  Subjects", callback_data="edit_subjects")],
        [InlineKeyboardButton("🎓  Levels", callback_data="edit_levels")],
        [InlineKeyboardButton("📍  Areas", callback_data="edit_areas")],
        [InlineKeyboardButton("💰  Rate", callback_data="edit_rate")],
        [InlineKeyboardButton("🔙  Back", callback_data="back_t")],
    ]
    await q.edit_message_text(
        hdr("✏️", "Edit Profile") + "\n\nSelect what you want to update:",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )
    return EDIT_TUTOR_MENU

async def edit_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer

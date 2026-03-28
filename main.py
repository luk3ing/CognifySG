"""
CognifySG — Production Bot v7
Changes from v6:
  - Rate removed from tutor profile; set per-application (applied_rate)
  - Parent location: town selector + 6-digit SG postal code
  - New requests broadcast to TUTOR_CHANNEL_ID via deep-link Apply button
  - Deep-link apply flow: /start apply_<id> preserved across captcha
  - Edit-profile-during-application flow with resume
  - Bug fixes: DB connection in p_budget, edit_profile_menu crash,
    my_reqs missing returns, confirm_match race condition,
    valid_rate floor, applied_postings back button
"""

import os
import re
import random
import logging
import threading
import asyncio
from http.server import HTTPServer, BaseHTTPRequestHandler

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardRemove, BotCommand,
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler,
)

import db
import sheets

# ── LOGGING ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── KEEPALIVE ─────────────────────────────────────────────────────────────────
class _KAHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers()
        self.wfile.write(b"CognifySG v7 running!")
    def log_message(self, *a): pass

threading.Thread(
    target=lambda: HTTPServer(
        ("0.0.0.0", int(os.environ.get("PORT", 8080))), _KAHandler
    ).serve_forever(),
    daemon=True,
).start()

# ── CONFIG ─────────────────────────────────────────────────────────────────────
TOKEN            = os.environ.get("TOKEN")
SUPER_ADMIN_ID   = int(os.environ.get("ADMIN_CHAT_ID", "0"))
TUTOR_CHANNEL_ID = int(os.environ.get("TUTOR_CHANNEL_ID", "0"))
BOT_USERNAME     = os.environ.get("BOT_USERNAME", "")   # e.g. "CognifySGBot"
MAX_CAPTCHA      = 3
PLACEMENT_FEE    = int(os.environ.get("PLACEMENT_FEE", "40"))
TERMS_URL        = os.environ.get("TERMS_URL", "https://cognifysg.com/terms")
PRIVACY_URL      = os.environ.get("PRIVACY_URL", "https://cognifysg.com/privacy")

REJECT_TUTOR  = [
    "Qualifications unclear", "Incomplete profile",
    "Duplicate account", "Suspected spam", "Not based in SG",
]
REJECT_PARENT = [
    "Budget too low", "Area not covered",
    "Subject not available", "Duplicate request", "Suspected spam",
]

ALL_SUBJECTS = [
    "Maths", "English", "Science", "Chinese", "Malay", "Tamil",
    "Physics", "Chemistry", "Biology", "History", "Geography", "Literature",
]
ALL_LEVELS = [
    "Primary 1-3", "Primary 4-6", "Lower Sec", "Upper Sec",
    "JC", "IB/IP", "Poly/ITE",
]
ALL_AREAS = ["North", "South", "East", "West", "Central", "Online"]

ALL_TOWNS = [
    "Ang Mo Kio",   "Bedok",         "Bishan",        "Bukit Batok",
    "Bukit Merah",  "Bukit Panjang", "Bukit Timah",   "Choa Chu Kang",
    "Clementi",     "Geylang",       "Hougang",       "Jurong East",
    "Jurong West",  "Kallang",       "Marine Parade", "Novena",
    "Pasir Ris",    "Punggol",       "Queenstown",    "Sembawang",
    "Sengkang",     "Serangoon",     "Tampines",      "Toa Payoh",
    "Woodlands",    "Yishun",        "Online",
]

TOWN_TO_AREA = {
    "Ang Mo Kio": "North",   "Sembawang":  "North", "Woodlands":  "North", "Yishun":   "North",
    "Bedok":      "East",    "Hougang":    "East",  "Pasir Ris":  "East",  "Punggol":  "East",
    "Sengkang":   "East",    "Tampines":   "East",
    "Bukit Batok":"West",    "Bukit Panjang":"West","Choa Chu Kang":"West","Clementi": "West",
    "Jurong East":"West",    "Jurong West":"West",
    "Bishan":     "Central", "Bukit Timah":"Central","Geylang":   "Central","Kallang": "Central",
    "Marine Parade":"Central","Novena":    "Central","Queenstown":"Central","Serangoon":"Central",
    "Toa Payoh":  "Central",
    "Bukit Merah":"South",
    "Online":     "Online",
}

# ── UI HELPERS ─────────────────────────────────────────────────────────────────
DIV  = "────────"
DIV2 = "──────"

def hdr(icon, title):   return icon + "  *" + title + "*\n" + DIV
def fld(label, value):  return "▸ *" + label + ":* " + str(value)
def rate_str(r):        return "$" + str(r) + "/hr"

def ms_kb(options, selected, prefix, show_cancel=True):
    """Multi-select inline keyboard."""
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

def town_kb():
    """Single-select grid for towns, 3 per row."""
    rows, row = [], []
    for t in ALL_TOWNS:
        row.append(InlineKeyboardButton(t, callback_data="ptown|" + t))
        if len(row) == 3:
            rows.append(row); row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)

# ── ASYNC HELPERS ─────────────────────────────────────────────────────────────
async def log_to_sheets_async(func, *args):
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: func(*args))
    except Exception as e:
        logger.error("Sheets async error: %s", e)

# ── ADMIN HELPERS ─────────────────────────────────────────────────────────────
def get_admins():
    rows = db.execute("SELECT user_id FROM admins", fetch="all")
    admins = [r["user_id"] for r in rows] if rows else []
    if SUPER_ADMIN_ID not in admins:
        admins.append(SUPER_ADMIN_ID)
    return admins

def is_admin(uid):
    return bool(db.execute("SELECT 1 FROM admins WHERE user_id=%s", (uid,), fetch="one"))

async def notify_admins(bot, text, markup=None):
    for aid in get_admins():
        try:
            await bot.send_message(aid, text, reply_markup=markup, parse_mode="Markdown")
        except Exception as e:
            logger.warning("Notify admin %s failed: %s", aid, e)

# ── ERROR HANDLER ─────────────────────────────────────────────────────────────
async def error_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    err = ctx.error
    uid = update.effective_user.id if update and update.effective_user else 0
    logger.error("Unhandled error for user %s: %s", uid, err, exc_info=True)
    try:
        db.execute(
            "INSERT INTO error_log (user_id, handler, error) VALUES (%s,%s,%s)",
            (uid, str(ctx.match), str(err)[:2000]),
        )
    except Exception:
        pass
    try:
        await ctx.bot.send_message(
            SUPER_ADMIN_ID,
            hdr("⚠️", "Bot Error") + "\n\n" + fld("User", uid) + "\n" + fld("Error", str(err)[:300]),
            parse_mode="Markdown",
        )
    except Exception:
        pass
    if update and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ Something went wrong. Type /start to continue.\n\n_Team notified._",
                parse_mode="Markdown",
            )
        except Exception:
            pass

# ── VALIDATION ─────────────────────────────────────────────────────────────────
def valid_name(t):
    return bool(re.match(r"^[A-Za-z\s\-'\.]{2,50}$", t.strip()))

def valid_phone(t):
    return bool(re.match(r"^[89]\d{7}$", t.strip().replace(" ", "")))

def valid_rate(t):
    t = t.strip().replace("$", "").replace("/hr", "").replace(" ", "")
    return t.isdigit() and 15 <= int(t) <= 500

def clean_rate(t):
    return int(t.strip().replace("$", "").replace("/hr", "").replace(" ", ""))

def valid_postal(t):
    """6-digit SG postal code. Sector codes 01–82."""
    t = t.strip()
    if not re.match(r"^\d{6}$", t):
        return False
    return 1 <= int(t[:2]) <= 82

# ── MATCHING SCORE ─────────────────────────────────────────────────────────────
def compute_score(tutor, req, applied_rate):
    """Score 0-100. applied_rate is tutor's quoted rate for this job."""
    score = 0
    t_subj  = [s.strip().lower() for s in tutor["subjects"].split(",")]
    t_lvl   = [l.strip().lower() for l in tutor["levels"].split(",")]
    t_areas = [a.strip().lower() for a in tutor["areas"].split(",")]

    r_subj  = [s.strip().lower() for s in req["subject"].split(",")]
    r_lvl   = [l.strip().lower() for l in req["level"].split(",")]
    # Use town-mapped area if available, else legacy areas field
    req_area = TOWN_TO_AREA.get(req.get("town", ""), req.get("areas", "")).lower()

    if any(s in t_subj  for s in r_subj):                              score += 40
    if any(l in t_lvl   for l in r_lvl):                               score += 30
    if req_area in t_areas or "online" in t_areas or req_area == "online":
        score += 20
    if applied_rate <= req["budget"]:                                   score += 10

    score += min(int(float(tutor.get("rating_avg") or 0) * 2), 10)
    return min(score, 100)

# ── STATES ─────────────────────────────────────────────────────────────────────
(
    TERMS,
    CAPTCHA,
    ROLE_SELECT,
    T_NAME, T_PHONE, T_SUBJECTS, T_LEVELS, T_AREAS,
    P_NAME, P_PHONE, P_SUBJECT, P_LEVEL, P_TOWN, P_POSTAL, P_BUDGET,
    EDIT_TUTOR_MENU, EDIT_NAME, EDIT_PHONE,
    EDIT_SUBJECTS, EDIT_LEVELS, EDIT_AREAS, EDIT_RATE,
    APP_EDIT_PROMPT, APP_RATE,
) = range(24)

# ── CAPTCHA HELPERS ────────────────────────────────────────────────────────────
def gen_captcha():
    a, b = random.randint(2, 9), random.randint(2, 9)
    ans  = a + b
    opts = random.sample([x for x in range(2, 19) if x != ans], 3) + [ans]
    random.shuffle(opts)
    return a, b, ans, opts

def _cap_kb(a, b, opts):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(str(o), callback_data="cap|" + str(o)) for o in opts
    ]])

def _cap_text(a, b):
    return (
        hdr("🔐", "Security Verification") + "\n\nPlease confirm you are human.\n\n" +
        DIV2 + "\n❓ *What is " + str(a) + " + " + str(b) + "?*\n" + DIV2
    )

# ── TERMS ──────────────────────────────────────────────────────────────────────
async def send_terms(uid, bot):
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("📄 Terms",          url=TERMS_URL),
        InlineKeyboardButton("🔒 Privacy Policy", url=PRIVACY_URL),
    ], [
        InlineKeyboardButton("✅ I agree", callback_data="terms_accept"),
    ]])
    await bot.send_message(
        uid,
        hdr("📋", "Terms of Service") + "\n\n"
        "Before using *CognifySG*, please read and accept our Terms.\n\n" +
        DIV2 + "\n"
        "By tapping *I agree*, you confirm:\n"
        "▸ You are based in Singapore\n"
        "▸ You will not solicit outside this platform\n"
        "▸ We collect data per PDPA\n"
        "▸ A placement fee applies upon successful match\n\n" +
        DIV2 + "\n_Delete your data any time: /deleteaccount_",
        reply_markup=kb,
        parse_mode="Markdown",
    )

async def terms_accept(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    db.execute(
        "INSERT INTO terms_accepted(user_id) VALUES(%s) ON CONFLICT DO NOTHING", (uid,)
    )
    await q.edit_message_text(
        hdr("✅", "Terms Accepted") + "\n\nThank you. Let's verify you are human.",
        parse_mode="Markdown",
    )
    a, b, ans, opts = gen_captcha()
    ctx.user_data.update({"captcha_ans": ans, "ca": a, "cb": b, "cattempts": 0})
    await q.message.reply_text(_cap_text(a, b), reply_markup=_cap_kb(a, b, opts), parse_mode="Markdown")
    return CAPTCHA

# ── START / DEEP LINK ─────────────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if db.execute("SELECT 1 FROM blocked WHERE user_id=%s", (uid,), fetch="one"):
        await update.message.reply_text(
            hdr("🚫", "Access Denied") + "\n\nYour account has been restricted.",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    # Store deep-link apply intent BEFORE captcha so it survives the flow
    if ctx.args and ctx.args[0].startswith("apply_"):
        try:
            ctx.user_data["pending_apply_id"] = int(ctx.args[0].replace("apply_", ""))
        except ValueError:
            pass

    if not db.execute("SELECT 1 FROM terms_accepted WHERE user_id=%s", (uid,), fetch="one"):
        await send_terms(uid, update.get_bot())
        return TERMS

    a, b, ans, opts = gen_captcha()
    ctx.user_data.update({"captcha_ans": ans, "ca": a, "cb": b, "cattempts": 0})
    await update.message.reply_text(_cap_text(a, b), reply_markup=_cap_kb(a, b, opts), parse_mode="Markdown")
    return CAPTCHA

async def welcome(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if db.execute("SELECT 1 FROM terms_accepted WHERE user_id=%s", (uid,), fetch="one"):
        await update.message.reply_text("Welcome back! Use /start to open the main menu.")
        return
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("▶️ Start", callback_data="start_welcome")
    ]])
    await update.message.reply_text(
        hdr("🎓", "Welcome to CognifySG") + "\n\nSingapore's trusted tuition matching platform.\n\nTap *Start* to begin.",
        reply_markup=kb,
        parse_mode="Markdown",
    )

async def start_welcome_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.message.delete()
    uid = q.from_user.id
    if db.execute("SELECT 1 FROM blocked WHERE user_id=%s", (uid,), fetch="one"):
        await ctx.bot.send_message(uid, hdr("🚫", "Access Denied") + "\n\nAccount restricted.", parse_mode="Markdown")
        return ConversationHandler.END
    if not db.execute("SELECT 1 FROM terms_accepted WHERE user_id=%s", (uid,), fetch="one"):
        await send_terms(uid, ctx.bot)
        return TERMS
    a, b, ans, opts = gen_captcha()
    ctx.user_data.update({"captcha_ans": ans, "ca": a, "cb": b, "cattempts": 0})
    await ctx.bot.send_message(uid, _cap_text(a, b), reply_markup=_cap_kb(a, b, opts), parse_mode="Markdown")
    return CAPTCHA

# ── CAPTCHA CALLBACK ──────────────────────────────────────────────────────────
async def captcha_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    parts = q.data.split("|")
    try:
        chosen = int(parts[1])
    except (IndexError, ValueError):
        return CAPTCHA

    uid = q.from_user.id
    ctx.user_data["cattempts"] = ctx.user_data.get("cattempts", 0) + 1

    if chosen != ctx.user_data.get("captcha_ans"):
        remaining = MAX_CAPTCHA - ctx.user_data["cattempts"]
        if remaining <= 0:
            db.execute("INSERT INTO blocked(user_id) VALUES(%s) ON CONFLICT DO NOTHING", (uid,))
            await q.edit_message_text(
                hdr("🚫", "Access Denied") + "\n\nMaximum attempts exceeded.", parse_mode="Markdown"
            )
            return ConversationHandler.END
        a2, b2, ans2, opts2 = gen_captcha()
        ctx.user_data.update({"captcha_ans": ans2, "ca": a2, "cb": b2})
        pl = "s" if remaining > 1 else ""
        await q.edit_message_text(
            hdr("🔐", "Verification Failed") + "\n\n"
            "❌ Incorrect. ⚠️ *" + str(remaining) + " attempt" + pl + " remaining.*\n\n" +
            DIV2 + "\n❓ *What is " + str(a2) + " + " + str(b2) + "?*\n" + DIV2,
            reply_markup=_cap_kb(a2, b2, opts2),
            parse_mode="Markdown",
        )
        return CAPTCHA

    # ── Correct answer: check for pending deep-link apply ─────────────────────
    pending_id = ctx.user_data.pop("pending_apply_id", None)
    if pending_id:
        tutor = db.execute("SELECT approved FROM tutors WHERE user_id=%s", (uid,), fetch="one")
        if tutor and tutor["approved"]:
            req = db.execute(
                "SELECT id, subject, level, town, postal_code, budget FROM requests "
                "WHERE id=%s AND status='open' AND approved=1",
                (pending_id,), fetch="one",
            )
            if req:
                if db.execute(
                    "SELECT 1 FROM applications WHERE tutor_id=%s AND request_id=%s",
                    (uid, pending_id), fetch="one",
                ):
                    await q.edit_message_text("⚠️ You have already applied for this request.")
                    return ConversationHandler.END
                ctx.user_data["apply_req_id"] = pending_id
                location_txt = req["town"] or ""
                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("✏️ Update Profile First", callback_data="app_doedit")],
                    [InlineKeyboardButton("▶ Apply Now",            callback_data="app_noedit")],
                ])
                await q.edit_message_text(
                    hdr("🎯", "Apply for Request") + "\n\n" +
                    fld("Request", "#" + str(req["id"])) + "\n" +
                    fld("Subject", req["subject"]) + "\n" +
                    fld("Level",   req["level"]) + "\n" +
                    fld("Location", location_txt) + "\n" +
                    fld("Budget",  rate_str(req["budget"])) + "\n\n" +
                    DIV2 + "\n_Would you like to update your profile before applying?_",
                    reply_markup=kb,
                    parse_mode="Markdown",
                )
                return APP_EDIT_PROMPT

    # ── Normal flow: role select ───────────────────────────────────────────────
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("👨‍🏫 I am a Tutor",  callback_data="role_tutor"),
        InlineKeyboardButton("👨‍👩‍👧 I am a Parent", callback_data="role_parent"),
    ]])
    await q.edit_message_text(
        hdr("🎓", "Welcome to CognifySG") + "\n\nPlease identify yourself:",
        reply_markup=kb,
        parse_mode="Markdown",
    )
    return ROLE_SELECT

# ── ROLE SELECT ───────────────────────────────────────────────────────────────
async def role_select(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    if q.data == "role_tutor":
        row = db.execute("SELECT approved FROM tutors WHERE user_id=%s", (uid,), fetch="one")
        if row:
            if row["approved"]:
                return await _show_tutor_menu(q)
            await q.edit_message_text(
                hdr("⏳", "Approval Pending") + "\n\nYour profile is pending admin approval.",
                parse_mode="Markdown",
            )
            return ConversationHandler.END
        # New tutor — start registration
        await q.edit_message_text(
            hdr("👨‍🏫", "Tutor Registration") + "\n\n_Step 1 of 4_ — Enter your *full name:*",
            parse_mode="Markdown",
        )
        return T_NAME
    # Parent
    return await _show_parent_menu_q(q, uid)

# ── TUTOR REGISTRATION ────────────────────────────────────────────────────────
async def t_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    if not valid_name(txt):
        await update.message.reply_text("⚠️ *Invalid name.* Letters only, min 2 characters.", parse_mode="Markdown")
        return T_NAME
    ctx.user_data["t_name"] = txt
    await update.message.reply_text(
        hdr("📱", "WhatsApp Number") + "\n\n_Step 2 of 4_\n\n"
        "Enter your *8-digit SG WhatsApp number.*\n\n" +
        DIV2 + "\n✳️ Starts with 8 or 9\n✳️ Example: `91234567`",
        parse_mode="Markdown",
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
        hdr("📚", "Subjects") + "\n\n_Step 3 of 4_\n\nSelect *all subjects* you teach:",
        reply_markup=ms_kb(ALL_SUBJECTS, [], "tsubj"),
        parse_mode="Markdown",
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
            hdr("🎓", "Academic Levels") + "\n\n_Step 3 of 4 (cont.)_\n\n"
            "Subjects: *" + ", ".join(ctx.user_data["t_subjects"]) + "*\n\n" +
            DIV2 + "\nSelect *levels* you teach:",
            reply_markup=ms_kb(ALL_LEVELS, [], "tlvl"),
            parse_mode="Markdown",
        )
        return T_LEVELS
    sel = ctx.user_data.get("t_subjects", [])
    if val in sel:
        sel.remove(val)
    else:
        sel.append(val)
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
            hdr("📍", "Travel Areas") + "\n\n_Step 4 of 4_\n\n"
            "Levels: *" + ", ".join(ctx.user_data["t_levels"]) + "*\n\n" +
            DIV2 + "\nSelect *areas* you travel to:",
            reply_markup=ms_kb(ALL_AREAS, [], "tarea"),
            parse_mode="Markdown",
        )
        return T_AREAS
    sel = ctx.user_data.get("t_levels", [])
    if val in sel:
        sel.remove(val)
    else:
        sel.append(val)
    ctx.user_data["t_levels"] = sel
    await q.edit_message_reply_markup(reply_markup=ms_kb(ALL_LEVELS, sel, "tlvl"))
    return T_LEVELS

async def t_areas(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Last step of tutor registration — submit on DONE."""
    q = update.callback_query
    await q.answer()
    val = q.data.split("|", 1)[1]

    if val == "DONE":
        if not ctx.user_data.get("t_areas"):
            await q.answer("Pick at least one area!", show_alert=True)
            return T_AREAS
        # ── Submit registration ────────────────────────────────────────────────
        u = q.from_user
        db.execute(
            "INSERT INTO tutors "
            "(user_id, username, name, phone, subjects, levels, areas, rate, approved) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "name=EXCLUDED.name, phone=EXCLUDED.phone, subjects=EXCLUDED.subjects, "
            "levels=EXCLUDED.levels, areas=EXCLUDED.areas, rate=EXCLUDED.rate, "
            "approved=EXCLUDED.approved",
            (
                u.id, u.username or "",
                ctx.user_data["t_name"], ctx.user_data["t_phone"],
                ", ".join(ctx.user_data["t_subjects"]),
                ", ".join(ctx.user_data["t_levels"]),
                ", ".join(ctx.user_data["t_areas"]),
                0,   # rate set per-application; 0 placeholder
                0,   # all tutors need admin approval
            ),
        )
        asyncio.create_task(log_to_sheets_async(
            sheets.log_tutor,
            u.id, ctx.user_data["t_name"], ctx.user_data["t_phone"],
            u.username or "",
            ", ".join(ctx.user_data["t_subjects"]),
            ", ".join(ctx.user_data["t_levels"]),
            ", ".join(ctx.user_data["t_areas"]),
            0,
        ))

        handle = "@" + u.username if u.username else "No username"
        admin_msg = (
            hdr("📋", "New Tutor Application") + "\n\n" +
            fld("Name",     ctx.user_data["t_name"]) + "\n" +
            fld("WhatsApp", ctx.user_data["t_phone"]) + "\n" +
            fld("Telegram", handle) + "\n" +
            fld("Subjects", ", ".join(ctx.user_data["t_subjects"])) + "\n" +
            fld("Levels",   ", ".join(ctx.user_data["t_levels"])) + "\n" +
            fld("Areas",    ", ".join(ctx.user_data["t_areas"])) + "\n\n" +
            DIV2 + "\n_Please review and approve or reject._"
        )
        kb_admin = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Approve", callback_data="app_t_" + str(u.id)),
            InlineKeyboardButton("❌ Reject",  callback_data="rej_t_" + str(u.id)),
        ]])
        asyncio.create_task(notify_admins(q.get_bot(), admin_msg, kb_admin))

        await q.edit_message_text(
            hdr("⏳", "Application Submitted") + "\n\n"
            "Your profile is *pending admin approval.*\n"
            "You'll be notified once reviewed.\n\n" +
            fld("Name",     ctx.user_data["t_name"]) + "\n" +
            fld("Subjects", ", ".join(ctx.user_data["t_subjects"])) + "\n" +
            fld("Levels",   ", ".join(ctx.user_data["t_levels"])) + "\n" +
            fld("Areas",    ", ".join(ctx.user_data["t_areas"])),
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    sel = ctx.user_data.get("t_areas", [])
    if val in sel:
        sel.remove(val)
    else:
        sel.append(val)
    ctx.user_data["t_areas"] = sel
    await q.edit_message_reply_markup(reply_markup=ms_kb(ALL_AREAS, sel, "tarea"))
    return T_AREAS

# ── TUTOR MENU ────────────────────────────────────────────────────────────────
async def _show_tutor_menu(q_or_msg, is_message=False):
    """Show tutor dashboard. Accepts a CallbackQuery or Message."""
    uid = q_or_msg.from_user.id if not is_message else q_or_msg.from_user.id
    row = db.execute("SELECT available FROM tutors WHERE user_id=%s", (uid,), fetch="one")
    status = "🟢 Available" if (row and row["available"]) else "🔴 Unavailable"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Browse Requests",      callback_data="browse_reqs")],
        [InlineKeyboardButton("📌 Applied Postings",    callback_data="applied_postings")],
        [InlineKeyboardButton("👤 My Profile",          callback_data="view_t_profile")],
        [InlineKeyboardButton("✏️ Edit Profile",        callback_data="edit_profile")],
        [InlineKeyboardButton("🔄 Toggle Availability", callback_data="toggle_avail")],
    ])
    text = hdr("🎓", "Tutor Dashboard") + "\n\n" + fld("Status", status) + "\n\n" + DIV2 + "\n_Select an option:_"
    if is_message:
        await q_or_msg.reply_text(text, reply_markup=kb, parse_mode="Markdown")
    else:
        await q_or_msg.edit_message_text(text, reply_markup=kb, parse_mode="Markdown")
    return ConversationHandler.END

async def tutor_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    return await _show_tutor_menu(update.callback_query)

async def tutor_menu_msg(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    return await _show_tutor_menu(update.message, is_message=True)

# ── TUTOR: APPLIED POSTINGS ───────────────────────────────────────────────────
async def applied_postings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    apps = db.execute("""
        SELECT a.request_id, r.subject, r.level, a.match_score,
               a.applied_rate, r.status, a.created_at
        FROM applications a
        JOIN requests r ON r.id = a.request_id
        WHERE a.tutor_id=%s
        ORDER BY a.created_at DESC
    """, (uid,), fetch="all")

    kb_back = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="back_t")]])
    if not apps:
        await q.edit_message_text(
            hdr("📌", "Your Applied Postings") + "\n\nYou have not applied to any requests yet.",
            reply_markup=kb_back,
            parse_mode="Markdown",
        )
        return

    lines = [hdr("📌", "Your Applied Postings") + "\n"]
    for a in apps:
        icon = "✅ Matched" if a["status"] == "matched" else "🟡 Pending"
        rate_info = ("  |  Rate: " + rate_str(a["applied_rate"])) if a["applied_rate"] else ""
        lines.append(
            "*#" + str(a["request_id"]) + "* — " + a["subject"] + " | " + a["level"] + "\n" +
            "   Score: " + str(a["match_score"]) + "/100" + rate_info + "\n" +
            "   Status: " + icon + "\n" +
            "   Applied: " + a["created_at"].strftime("%d %b %Y")
        )
        lines.append(DIV2)

    await q.edit_message_text(
        "\n\n".join(lines),
        reply_markup=kb_back,
        parse_mode="Markdown",
    )

# ── TUTOR: BROWSE REQUESTS ────────────────────────────────────────────────────
async def browse_reqs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    t = db.execute("SELECT approved FROM tutors WHERE user_id=%s", (uid,), fetch="one")
    if not t or not t["approved"]:
        await q.edit_message_text(
            hdr("⏳", "Access Restricted") + "\n\nYour profile is pending admin approval.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="back_t")]]),
            parse_mode="Markdown",
        )
        return

    reqs = db.execute("""
        SELECT id, subject, level, town, areas, budget
        FROM requests
        WHERE status='open' AND approved=1
          AND id NOT IN (SELECT request_id FROM applications WHERE tutor_id=%s)
        ORDER BY created_at DESC
    """, (uid,), fetch="all")

    if not reqs:
        await q.edit_message_text(
            hdr("📋", "Open Requests") + "\n\nNo new requests at this time.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="back_t")]]),
            parse_mode="Markdown",
        )
        return

    ctx.user_data["rlist"] = [dict(r) for r in reqs]
    ctx.user_data["ridx"]  = 0
    await _show_req_card(q, ctx)

async def _show_req_card(q, ctx):
    reqs = ctx.user_data["rlist"]
    idx  = ctx.user_data["ridx"]
    r    = reqs[idx]
    location = r["town"] if r.get("town") else r.get("areas", "")
    nav = []
    if idx > 0:
        nav.append(InlineKeyboardButton("◀ Prev", callback_data="req_prev"))
    if idx < len(reqs) - 1:
        nav.append(InlineKeyboardButton("Next ▶", callback_data="req_next"))
    kb = []
    if nav:
        kb.append(nav)
    kb.append([InlineKeyboardButton("✅ Apply for this Request", callback_data="apply_" + str(r["id"]))])
    kb.append([InlineKeyboardButton("🔙 Back", callback_data="back_t")])
    await q.edit_message_text(
        hdr("📋", "Open Request") + "\n\n_" + str(idx + 1) + " of " + str(len(reqs)) + "_\n\n" +
        fld("Ref",      "#" + str(r["id"])) + "\n" +
        fld("Subject",  r["subject"]) + "\n" +
        fld("Level",    r["level"]) + "\n" +
        fld("Location", location) + "\n" +
        fld("Budget",   rate_str(r["budget"])) + "\n\n" +
        DIV2 + "\n_Contact details withheld until match is confirmed._",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown",
    )

async def req_nav(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ctx.user_data["ridx"] += 1 if q.data == "req_next" else -1
    await _show_req_card(q, ctx)

# ── APPLY FLOW (entry point) ──────────────────────────────────────────────────
async def apply_req(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Entry point: tutor taps Apply button from browse cards."""
    q = update.callback_query
    await q.answer()
    uid    = q.from_user.id
    req_id = int(q.data.replace("apply_", ""))

    if db.execute(
        "SELECT 1 FROM applications WHERE tutor_id=%s AND request_id=%s", (uid, req_id), fetch="one"
    ):
        await q.answer("⚠️ You already applied for this.", show_alert=True)
        return ConversationHandler.END

    req = db.execute(
        "SELECT id, subject, level, town, postal_code, budget FROM requests "
        "WHERE id=%s AND status='open' AND approved=1",
        (req_id,), fetch="one",
    )
    if not req:
        await q.answer("This request is no longer available.", show_alert=True)
        return ConversationHandler.END

    ctx.user_data["apply_req_id"] = req_id
    location = req["town"] or ""
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Update Profile First", callback_data="app_doedit")],
        [InlineKeyboardButton("▶ Apply Now",            callback_data="app_noedit")],
    ])
    await q.edit_message_text(
        hdr("🎯", "Apply for Request") + "\n\n" +
        fld("Request", "#" + str(req["id"])) + "\n" +
        fld("Subject", req["subject"]) + "\n" +
        fld("Level",   req["level"]) + "\n" +
        fld("Location", location) + "\n" +
        fld("Budget",  rate_str(req["budget"])) + "\n\n" +
        DIV2 + "\n_Would you like to update your profile before applying?_",
        reply_markup=kb,
        parse_mode="Markdown",
    )
    return APP_EDIT_PROMPT

async def app_noedit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Tutor skips profile edit — go straight to rate input."""
    q = update.callback_query
    await q.answer()
    req_id = ctx.user_data.get("apply_req_id", "?")
    await q.edit_message_text(
        hdr("💰", "Your Rate for This Job") + "\n\n"
        "Request #" + str(req_id) + "\n\n" +
        DIV2 + "\nEnter the *hourly rate* you will charge for this request:\n\n"
        "✳️ Numbers only (e.g. `35`)\n✳️ Between $15–$500/hr",
        parse_mode="Markdown",
    )
    return APP_RATE

async def app_doedit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Tutor wants to edit profile before applying."""
    q = update.callback_query
    await q.answer()
    ctx.user_data["in_app_edit"] = True
    uid   = q.from_user.id
    tutor = db.execute("SELECT * FROM tutors WHERE user_id=%s", (uid,), fetch="one")
    ctx.user_data["edit_tutor"] = tutor
    await q.edit_message_text(
        _edit_menu_text(ctx),
        reply_markup=_edit_menu_kb(ctx),
        parse_mode="Markdown",
    )
    return EDIT_TUTOR_MENU

async def app_continue_rate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Tutor finished editing, continue to rate input."""
    q = update.callback_query
    await q.answer()
    req_id = ctx.user_data.get("apply_req_id", "?")
    await q.edit_message_text(
        hdr("💰", "Your Rate for This Job") + "\n\n"
        "Request #" + str(req_id) + "\n\n" +
        DIV2 + "\nEnter the *hourly rate* you will charge for this request:\n\n"
        "✳️ Numbers only (e.g. `35`)\n✳️ Between $15–$500/hr",
        parse_mode="Markdown",
    )
    return APP_RATE

async def app_rate_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle tutor's rate input for this specific application."""
    txt = update.message.text.strip()
    if not valid_rate(txt):
        await update.message.reply_text(
            "⚠️ *Invalid rate.* Enter a number between $15 and $500.\n_Example: `40`_",
            parse_mode="Markdown",
        )
        return APP_RATE

    applied_rate = clean_rate(txt)
    tutor_id     = update.effective_user.id
    req_id       = ctx.user_data.get("apply_req_id")

    if not req_id:
        await update.message.reply_text("⚠️ Session expired. Please use /start.")
        return ConversationHandler.END

    # Double-check not already applied
    if db.execute(
        "SELECT 1 FROM applications WHERE tutor_id=%s AND request_id=%s", (tutor_id, req_id), fetch="one"
    ):
        await update.message.reply_text("⚠️ You have already applied for this request.")
        ctx.user_data.pop("apply_req_id", None)
        ctx.user_data.pop("in_app_edit", None)
        return ConversationHandler.END

    tutor = db.execute("SELECT * FROM tutors WHERE user_id=%s", (tutor_id,), fetch="one")
    req   = db.execute("SELECT * FROM requests WHERE id=%s", (req_id,), fetch="one")
    if not tutor or not req:
        await update.message.reply_text("⚠️ Request not found.")
        return ConversationHandler.END

    score = compute_score(tutor, req, applied_rate)
    db.execute(
        "INSERT INTO applications (tutor_id, request_id, match_score, applied_rate) "
        "VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
        (tutor_id, req_id, score, applied_rate),
    )

    app_count = db.execute(
        "SELECT COUNT(*) as n FROM applications WHERE request_id=%s", (req_id,), fetch="one"
    )["n"]
    asyncio.create_task(log_to_sheets_async(sheets.update_applicant_count, req_id, app_count))

    t_handle = "@" + tutor["username"] if tutor["username"] else "No username"
    p_handle = "@" + req["username"]   if req["username"]   else "No username"
    location = (req.get("town") or "") + (" " + req.get("postal_code", "") if req.get("postal_code") else "")

    admin_msg = (
        hdr("🎯", "New Application") + "\n\n"
        "📊 *Match Score: " + str(score) + "/100*\n\n" +
        DIV2 + "\n📌 *JOB REQUEST*\n" +
        fld("Ref",      "#" + str(req["id"])) + "\n" +
        fld("Subject",  req["subject"]) + "\n" +
        fld("Level",    req["level"]) + "\n" +
        fld("Location", location) + "\n" +
        fld("Budget",   rate_str(req["budget"])) + "\n\n" +
        DIV2 + "\n👨‍🏫 *TUTOR*\n" +
        fld("Name",     tutor["name"]) + "\n" +
        fld("WhatsApp", tutor["phone"]) + "\n" +
        fld("Telegram", t_handle) + "\n" +
        fld("Subjects", tutor["subjects"]) + "\n" +
        fld("Levels",   tutor["levels"]) + "\n" +
        fld("Rate",     rate_str(applied_rate) + " _(for this job)_") + "\n\n" +
        DIV2 + "\n👨‍👩‍👧 *PARENT*\n" +
        fld("Name",     req["name"]) + "\n" +
        fld("WhatsApp", req["phone"]) + "\n" +
        fld("Telegram", p_handle) + "\n\n" +
        DIV2 + "\n_Total applicants: " + str(app_count) + " — use /applicants " + str(req["id"]) + " to compare._"
    )
    kb_match = InlineKeyboardMarkup([[InlineKeyboardButton(
        "✅ Confirm Match — #" + str(req_id) + " + " + tutor["name"],
        callback_data="confirm_match_" + str(req_id) + "_" + str(tutor_id),
    )]])
    asyncio.create_task(notify_admins(update.get_bot(), admin_msg, kb_match))

    ctx.user_data.pop("apply_req_id", None)
    ctx.user_data.pop("in_app_edit", None)

    await update.message.reply_text(
        hdr("✅", "Application Submitted") + "\n\n"
        "Your application has been received.\n\n" +
        fld("Request",  "#" + str(req_id)) + "\n" +
        fld("Rate",     rate_str(applied_rate)) + "\n" +
        fld("Score",    str(score) + "/100") + "\n\n" +
        DIV2 + "\nAdmins will contact you if matched.\n"
        "Track your applications via *Applied Postings* in the main menu.",
        parse_mode="Markdown",
    )
    return ConversationHandler.END

# ── EDIT PROFILE FLOW ─────────────────────────────────────────────────────────
def _edit_menu_text(ctx):
    in_app = ctx.user_data.get("in_app_edit", False)
    req_id = ctx.user_data.get("apply_req_id")
    prefix = ("Editing profile for Request #" + str(req_id) + "\n\n") if in_app and req_id else ""
    return hdr("✏️", "Edit Profile") + "\n\n" + prefix + "Select what you want to update:"

def _edit_menu_kb(ctx):
    in_app = ctx.user_data.get("in_app_edit", False)
    rows = [
        [InlineKeyboardButton("✏️ Name",      callback_data="edit_name")],
        [InlineKeyboardButton("📱 Phone",     callback_data="edit_phone")],
        [InlineKeyboardButton("📚 Subjects",  callback_data="edit_subjects")],
        [InlineKeyboardButton("🎓 Levels",    callback_data="edit_levels")],
        [InlineKeyboardButton("📍 Areas",     callback_data="edit_areas")],
        [InlineKeyboardButton("💰 Rate",      callback_data="edit_rate")],
    ]
    if in_app:
        rows.insert(0, [InlineKeyboardButton("✅ Continue Application", callback_data="app_continue_rate")])
        rows.append([InlineKeyboardButton("❌ Cancel Application", callback_data="back_t")])
    else:
        rows.append([InlineKeyboardButton("🔙 Back", callback_data="back_t")])
    return InlineKeyboardMarkup(rows)

async def edit_profile_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show edit menu — handles both callback_query and message update origins."""
    uid   = update.effective_user.id
    tutor = db.execute("SELECT * FROM tutors WHERE user_id=%s", (uid,), fetch="one")
    if not tutor:
        txt = "Profile not found. Use /start."
        if update.callback_query:
            await update.callback_query.edit_message_text(txt)
        else:
            await update.message.reply_text(txt)
        return ConversationHandler.END
    ctx.user_data["edit_tutor"] = tutor
    text = _edit_menu_text(ctx)
    kb   = _edit_menu_kb(ctx)
    q    = update.callback_query
    if q:
        await q.answer()
        await q.edit_message_text(text, reply_markup=kb, parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=kb, parse_mode="Markdown")
    return EDIT_TUTOR_MENU

async def edit_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    await q.edit_message_text(hdr("✏️", "Edit Name") + "\n\nEnter your *new full name:*", parse_mode="Markdown")
    return EDIT_NAME

async def edit_phone(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    await q.edit_message_text(
        hdr("✏️", "Edit WhatsApp Number") + "\n\nEnter your *8-digit SG number:*\n\n✳️ Starts with 8 or 9",
        parse_mode="Markdown",
    )
    return EDIT_PHONE

async def edit_subjects(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    tutor   = ctx.user_data.get("edit_tutor") or db.execute("SELECT * FROM tutors WHERE user_id=%s", (q.from_user.id,), fetch="one")
    current = [s.strip() for s in tutor["subjects"].split(",")] if tutor and tutor["subjects"] else []
    ctx.user_data["edit_subjects"] = current
    await q.edit_message_text(
        hdr("✏️", "Edit Subjects") + "\n\nSelect *all subjects* you teach:",
        reply_markup=ms_kb(ALL_SUBJECTS, current, "esubj", show_cancel=False),
        parse_mode="Markdown",
    )
    return EDIT_SUBJECTS

async def edit_levels(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    tutor   = ctx.user_data.get("edit_tutor") or db.execute("SELECT * FROM tutors WHERE user_id=%s", (q.from_user.id,), fetch="one")
    current = [l.strip() for l in tutor["levels"].split(",")] if tutor and tutor["levels"] else []
    ctx.user_data["edit_levels"] = current
    await q.edit_message_text(
        hdr("✏️", "Edit Levels") + "\n\nSelect *levels* you teach:",
        reply_markup=ms_kb(ALL_LEVELS, current, "elvl", show_cancel=False),
        parse_mode="Markdown",
    )
    return EDIT_LEVELS

async def edit_areas(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    tutor   = ctx.user_data.get("edit_tutor") or db.execute("SELECT * FROM tutors WHERE user_id=%s", (q.from_user.id,), fetch="one")
    current = [a.strip() for a in tutor["areas"].split(",")] if tutor and tutor["areas"] else []
    ctx.user_data["edit_areas"] = current
    await q.edit_message_text(
        hdr("✏️", "Edit Areas") + "\n\nSelect *areas* you travel to:",
        reply_markup=ms_kb(ALL_AREAS, current, "eara", show_cancel=False),
        parse_mode="Markdown",
    )
    return EDIT_AREAS

async def edit_rate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    await q.edit_message_text(
        hdr("✏️", "Edit Default Rate") + "\n\n"
        "This is a reference rate. Your actual rate is set per application.\n\n"
        "Enter *hourly rate in SGD:*\n\n✳️ Numbers only (e.g. `35`)\n✳️ Between $15–$500/hr",
        parse_mode="Markdown",
    )
    return EDIT_RATE

async def edit_subjects_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    val = q.data.split("|", 1)[1]
    sel = ctx.user_data.get("edit_subjects", [])
    if val == "DONE":
        if not sel:
            await q.answer("Pick at least one subject!", show_alert=True)
            return EDIT_SUBJECTS
        db.execute("UPDATE tutors SET subjects=%s WHERE user_id=%s", (", ".join(sel), q.from_user.id))
        ctx.user_data["edit_tutor"] = db.execute("SELECT * FROM tutors WHERE user_id=%s", (q.from_user.id,), fetch="one")
        await q.edit_message_text(hdr("✅", "Subjects Updated") + "\n\nSubjects saved.", parse_mode="Markdown")
        return await edit_profile_menu(update, ctx)
    if val in sel:
        sel.remove(val)
    else:
        sel.append(val)
    ctx.user_data["edit_subjects"] = sel
    await q.edit_message_reply_markup(reply_markup=ms_kb(ALL_SUBJECTS, sel, "esubj", show_cancel=False))
    return EDIT_SUBJECTS

async def edit_levels_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    val = q.data.split("|", 1)[1]
    sel = ctx.user_data.get("edit_levels", [])
    if val == "DONE":
        if not sel:
            await q.answer("Pick at least one level!", show_alert=True)
            return EDIT_LEVELS
        db.execute("UPDATE tutors SET levels=%s WHERE user_id=%s", (", ".join(sel), q.from_user.id))
        ctx.user_data["edit_tutor"] = db.execute("SELECT * FROM tutors WHERE user_id=%s", (q.from_user.id,), fetch="one")
        await q.edit_message_text(hdr("✅", "Levels Updated") + "\n\nLevels saved.", parse_mode="Markdown")
        return await edit_profile_menu(update, ctx)
    if val in sel:
        sel.remove(val)
    else:
        sel.append(val)
    ctx.user_data["edit_levels"] = sel
    await q.edit_message_reply_markup(reply_markup=ms_kb(ALL_LEVELS, sel, "elvl", show_cancel=False))
    return EDIT_LEVELS

async def edit_areas_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    val = q.data.split("|", 1)[1]
    sel = ctx.user_data.get("edit_areas", [])
    if val == "DONE":
        if not sel:
            await q.answer("Pick at least one area!", show_alert=True)
            return EDIT_AREAS
        db.execute("UPDATE tutors SET areas=%s WHERE user_id=%s", (", ".join(sel), q.from_user.id))
        ctx.user_data["edit_tutor"] = db.execute("SELECT * FROM tutors WHERE user_id=%s", (q.from_user.id,), fetch="one")
        await q.edit_message_text(hdr("✅", "Areas Updated") + "\n\nAreas saved.", parse_mode="Markdown")
        return await edit_profile_menu(update, ctx)
    if val in sel:
        sel.remove(val)
    else:
        sel.append(val)
    ctx.user_data["edit_areas"] = sel
    await q.edit_message_reply_markup(reply_markup=ms_kb(ALL_AREAS, sel, "eara", show_cancel=False))
    return EDIT_AREAS

async def update_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    if not valid_name(txt):
        await update.message.reply_text("⚠️ *Invalid name.* Letters only, min 2 characters.", parse_mode="Markdown")
        return EDIT_NAME
    db.execute("UPDATE tutors SET name=%s WHERE user_id=%s", (txt, update.effective_user.id))
    await update.message.reply_text(hdr("✅", "Name Updated") + "\n\nName saved.", parse_mode="Markdown")
    return await edit_profile_menu(update, ctx)

async def update_phone(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    if not valid_phone(txt):
        await update.message.reply_text("⚠️ *Invalid number.* 8 digits starting with 8 or 9.", parse_mode="Markdown")
        return EDIT_PHONE
    uid = update.effective_user.id
    if db.execute("SELECT user_id FROM tutors WHERE phone=%s AND user_id!=%s", (txt, uid), fetch="one"):
        await update.message.reply_text("⚠️ This number is already registered to another account.", parse_mode="Markdown")
        return EDIT_PHONE
    db.execute("UPDATE tutors SET phone=%s WHERE user_id=%s", (txt, uid))
    await update.message.reply_text(hdr("✅", "Phone Updated") + "\n\nWhatsApp number saved.", parse_mode="Markdown")
    return await edit_profile_menu(update, ctx)

async def update_rate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    if not valid_rate(txt):
        await update.message.reply_text("⚠️ *Invalid rate.* Enter a number between $15 and $500.", parse_mode="Markdown")
        return EDIT_RATE
    rate = clean_rate(txt)
    db.execute("UPDATE tutors SET rate=%s WHERE user_id=%s", (rate, update.effective_user.id))
    await update.message.reply_text(hdr("✅", "Rate Updated") + "\n\nReference rate saved.", parse_mode="Markdown")
    return await edit_profile_menu(update, ctx)

# ── PARENT MENU ───────────────────────────────────────────────────────────────
async def _show_parent_menu(send_fn, uid, edit=False):
    parent    = db.execute("SELECT name FROM requests WHERE parent_id=%s LIMIT 1", (uid,), fetch="one")
    name      = parent["name"] if parent else "Parent"
    req_count = db.execute("SELECT COUNT(*) as n FROM requests WHERE parent_id=%s", (uid,), fetch="one")["n"]
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Post New Request", callback_data="post_req")],
        [InlineKeyboardButton("📋 View My Requests", callback_data="my_reqs")],
        [InlineKeyboardButton("🔙 Back to Main",     callback_data="back_to_start")],
    ])
    text = (
        hdr("👨‍👩‍👧", "Welcome, " + name + "!") + "\n\n"
        "📊 You have posted *" + str(req_count) + "* request(s).\n"
        "✅ You can post *unlimited* requests.\n\n" +
        DIV2 + "\n_What would you like to do?_"
    )
    if edit:
        await send_fn(text, reply_markup=kb, parse_mode="Markdown")
    else:
        await send_fn(text, reply_markup=kb, parse_mode="Markdown")

async def _show_parent_menu_q(q, uid):
    await _show_parent_menu(q.edit_message_text, uid, edit=True)
    return ConversationHandler.END

async def parent_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    return await _show_parent_menu_q(q, q.from_user.id)

# ── PARENT: POST REQUEST ──────────────────────────────────────────────────────
async def post_req_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text(
        hdr("📝", "New Tutor Request") + "\n\n_Step 1 of 6_ — Enter your *full name:*",
        parse_mode="Markdown",
    )
    return P_NAME

async def p_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    if not valid_name(txt):
        await update.message.reply_text("⚠️ *Invalid name.* Letters only, min 2 characters.", parse_mode="Markdown")
        return P_NAME
    ctx.user_data["p_name"] = txt
    await update.message.reply_text(
        hdr("📱", "WhatsApp Number") + "\n\n_Step 2 of 6_\n\n"
        "Enter your *8-digit SG WhatsApp number.*\n\n" +
        DIV2 + "\n✳️ Starts with 8 or 9\n✳️ No country code",
        parse_mode="Markdown",
    )
    return P_PHONE

async def p_phone(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    if not valid_phone(txt):
        await update.message.reply_text("⚠️ *Invalid number.* 8 digits starting with 8 or 9.", parse_mode="Markdown")
        return P_PHONE
    ctx.user_data["p_phone"]   = txt
    ctx.user_data["p_subject"] = []
    await update.message.reply_text(
        hdr("📚", "Subject Required") + "\n\n_Step 3 of 6_\n\nSelect subject(s):",
        reply_markup=ms_kb(ALL_SUBJECTS, [], "psubj"),
        parse_mode="Markdown",
    )
    return P_SUBJECT

async def p_subject(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    val = q.data.split("|", 1)[1]
    if val == "DONE":
        if not ctx.user_data.get("p_subject"):
            await q.answer("Pick at least one subject!", show_alert=True)
            return P_SUBJECT
        ctx.user_data["p_level"] = []
        await q.edit_message_text(
            hdr("🎓", "Academic Level") + "\n\n_Step 4 of 6_\n\n"
            "Subject: *" + ", ".join(ctx.user_data["p_subject"]) + "*\n\n" +
            DIV2 + "\nSelect your child's *level:*",
            reply_markup=ms_kb(ALL_LEVELS, [], "plvl"),
            parse_mode="Markdown",
        )
        return P_LEVEL
    sel = ctx.user_data.get("p_subject", [])
    if val in sel:
        sel.remove(val)
    else:
        sel.append(val)
    ctx.user_data["p_subject"] = sel
    await q.edit_message_reply_markup(reply_markup=ms_kb(ALL_SUBJECTS, sel, "psubj"))
    return P_SUBJECT

async def p_level(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    val = q.data.split("|", 1)[1]
    if val == "DONE":
        if not ctx.user_data.get("p_level"):
            await q.answer("Pick at least one level!", show_alert=True)
            return P_LEVEL
        await q.edit_message_text(
            hdr("📍", "Your Town") + "\n\n_Step 5 of 6_\n\n"
            "Level: *" + ", ".join(ctx.user_data["p_level"]) + "*\n\n" +
            DIV2 + "\nSelect your *town / area:*",
            reply_markup=town_kb(),
            parse_mode="Markdown",
        )
        return P_TOWN
    sel = ctx.user_data.get("p_level", [])
    if val in sel:
        sel.remove(val)
    else:
        sel.append(val)
    ctx.user_data["p_level"] = sel
    await q.edit_message_reply_markup(reply_markup=ms_kb(ALL_LEVELS, sel, "plvl"))
    return P_LEVEL

async def p_town(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    town = q.data.replace("ptown|", "")
    ctx.user_data["p_town"] = town
    await q.edit_message_text(
        hdr("📮", "Postal Code") + "\n\n_Step 5 of 6 (cont.)_\n\n"
        "Town: *" + town + "*\n\n" +
        DIV2 + "\nEnter your *6-digit Singapore postal code:*\n\n"
        "✳️ Example: `640311`\n✳️ Must be a valid SG postal code",
        parse_mode="Markdown",
    )
    return P_POSTAL

async def p_postal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    if not valid_postal(txt):
        await update.message.reply_text(
            "⚠️ *Invalid postal code.* Enter a 6-digit Singapore postal code.\n"
            "_Example: `640311`_",
            parse_mode="Markdown",
        )
        return P_POSTAL
    ctx.user_data["p_postal"] = txt
    await update.message.reply_text(
        hdr("💰", "Budget") + "\n\n_Step 6 of 6_\n\n"
        "Enter your *maximum hourly budget in SGD.*\n\n" +
        DIV2 + "\n✳️ Numbers only (e.g. `35`)",
        parse_mode="Markdown",
    )
    return P_BUDGET

async def p_budget(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    if not valid_rate(txt):
        await update.message.reply_text(
            "⚠️ *Invalid budget.* Enter a number between $15 and $500.\n_Example: `35`_",
            parse_mode="Markdown",
        )
        return P_BUDGET

    budget = clean_rate(txt)
    u      = update.effective_user
    town   = ctx.user_data.get("p_town", "")
    postal = ctx.user_data.get("p_postal", "")
    area   = TOWN_TO_AREA.get(town, "")   # map town → North/South/East/West/Central

    # Use db.execute (pooled) — never raw psycopg2 in handlers
    req_id = db.execute(
        "INSERT INTO requests "
        "(parent_id, username, name, phone, subject, level, areas, town, postal_code, budget, approved) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,1) RETURNING id",
        (
            u.id, u.username or "",
            ctx.user_data["p_name"], ctx.user_data["p_phone"],
            ", ".join(ctx.user_data["p_subject"]),
            ", ".join(ctx.user_data["p_level"]),
            area, town, postal, budget,
        ),
        fetch="id",
    )

    # ── Channel broadcast ──────────────────────────────────────────────────────
    async def broadcast_and_notify():
        try:
            await log_to_sheets_async(
                sheets.log_request,
                req_id, ctx.user_data["p_name"], ctx.user_data["p_phone"],
                u.username or "",
                ", ".join(ctx.user_data["p_subject"]),
                ", ".join(ctx.user_data["p_level"]),
                town, budget,
            )
            await log_to_sheets_async(sheets.approve_request_sheet, req_id)
        except Exception as e:
            logger.error("Sheets error: %s", e)

        if TUTOR_CHANNEL_ID and BOT_USERNAME:
            masked_postal = postal[:2] + "xxxx" if len(postal) == 6 else postal
            channel_text = (
                hdr("📋", "New Tuition Request") + "\n\n" +
                fld("Subject", ", ".join(ctx.user_data["p_subject"])) + "\n" +
                fld("Level",   ", ".join(ctx.user_data["p_level"])) + "\n" +
                fld("Town",    town) + "\n" +
                fld("Area",    masked_postal) + "\n" +
                fld("Budget",  rate_str(budget)) + "\n\n" +
                DIV2 + "\n_Tap below to apply. Contact details shared only after match is confirmed._"
            )
            apply_url = "https://t.me/" + BOT_USERNAME + "?start=apply_" + str(req_id)
            try:
                await ctx.bot.send_message(
                    TUTOR_CHANNEL_ID,
                    channel_text,
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("Apply Now ✅", url=apply_url)
                    ]]),
                    parse_mode="Markdown",
                )
            except Exception as e:
                logger.error("Channel broadcast failed: %s", e)

        handle   = "@" + u.username if u.username else "No username"
        admin_msg = (
            hdr("🆕", "New Parent Request") + "\n\n" +
            fld("Name",     ctx.user_data["p_name"]) + "\n" +
            fld("WhatsApp", ctx.user_data["p_phone"]) + "\n" +
            fld("Telegram", handle) + "\n" +
            fld("Subject",  ", ".join(ctx.user_data["p_subject"])) + "\n" +
            fld("Level",    ", ".join(ctx.user_data["p_level"])) + "\n" +
            fld("Town",     town) + "\n" +
            fld("Postal",   postal) + "\n" +
            fld("Budget",   rate_str(budget)) + "\n\n" +
            DIV2 + "\n_Request #" + str(req_id) + " — auto-approved and live._"
        )
        await notify_admins(ctx.bot, admin_msg)

    asyncio.create_task(broadcast_and_notify())

    kb_back = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Dashboard", callback_data="back_p")]])
    await update.message.reply_text(
        hdr("✅", "Request Live") + "\n\n"
        "Your request is now *live* and visible to tutors.\n\n" +
        fld("Subject", ", ".join(ctx.user_data["p_subject"])) + "\n" +
        fld("Level",   ", ".join(ctx.user_data["p_level"])) + "\n" +
        fld("Town",    town) + "\n" +
        fld("Budget",  rate_str(budget)) + "\n\n" +
        DIV2 + "\nOur team will contact you on *WhatsApp* once a tutor is matched.\n\n"
        "You can post *another request* anytime from the dashboard.",
        reply_markup=kb_back,
        parse_mode="Markdown",
    )
    return ConversationHandler.END

# ── PARENT: MY REQUESTS ───────────────────────────────────────────────────────
async def my_reqs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid  = q.from_user.id
    reqs = db.execute("""
        SELECT id, subject, level, town, areas, budget, status, created_at
        FROM requests WHERE parent_id=%s ORDER BY created_at DESC
    """, (uid,), fetch="all")

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Post New Request", callback_data="post_req")],
        [InlineKeyboardButton("🔙 Back",             callback_data="back_p")],
    ])
    if not reqs:
        await q.edit_message_text(
            hdr("📋", "My Requests") + "\n\nYou haven't posted any requests yet.",
            reply_markup=kb,
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    lines = [hdr("📋", "My Requests (" + str(len(reqs)) + " total)") + "\n"]
    for i, r in enumerate(reqs, 1):
        icon = "✅ MATCHED" if r["status"] == "matched" else ("🟡 OPEN" if r["status"] == "open" else "🔒 CLOSED")
        location = r["town"] if r.get("town") else r.get("areas", "")
        lines.append(
            "*" + str(i) + ". Request #" + str(r["id"]) + "*\n" +
            fld("Subject", r["subject"]) + "\n" +
            fld("Level",   r["level"]) + "\n" +
            fld("Town",    location) + "\n" +
            fld("Budget",  rate_str(r["budget"])) + "\n" +
            fld("Status",  icon) + "\n" +
            fld("Posted",  r["created_at"].strftime("%d %b %Y"))
        )
        lines.append(DIV2)
    lines.append("_Post another request using the button below._")

    await q.edit_message_text(
        "\n\n".join(lines),
        reply_markup=kb,
        parse_mode="Markdown",
    )
    return ConversationHandler.END

# ── TUTOR: PROFILE & AVAILABILITY ─────────────────────────────────────────────
async def view_t_profile(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    t = db.execute("SELECT * FROM tutors WHERE user_id=%s", (q.from_user.id,), fetch="one")
    if not t:
        await q.edit_message_text("Profile not found. Use /start.")
        return
    status   = "🟢 Available" if t["available"] else "🔴 Unavailable"
    approved = "✅ Approved" if t["approved"] else "⏳ Pending"
    rating   = (str(t["rating_avg"]) + " (" + str(t["rating_count"]) + " reviews)") if t["rating_count"] else "No ratings yet"
    await q.edit_message_text(
        hdr("👤", "My Tutor Profile") + "\n\n" +
        fld("Name",     t["name"]) + "\n" +
        fld("Phone",    t["phone"]) + "\n" +
        fld("Subjects", t["subjects"]) + "\n" +
        fld("Levels",   t["levels"]) + "\n" +
        fld("Areas",    t["areas"]) + "\n" +
        fld("Rate",     "Set per application") + "\n" +
        fld("Rating",   rating) + "\n" +
        fld("Status",   status) + "\n" +
        fld("Account",  approved),
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="back_t")]]),
        parse_mode="Markdown",
    )

async def toggle_avail(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    uid = q.from_user.id
    row = db.execute("SELECT available FROM tutors WHERE user_id=%s", (uid,), fetch="one")
    new = 0 if (row and row["available"]) else 1
    db.execute("UPDATE tutors SET available=%s WHERE user_id=%s", (new, uid))
    label = "🟢 You are now *Available.*" if new else "🔴 You are now *Unavailable.*"
    await q.edit_message_text(hdr("🔄", "Availability Updated") + "\n\n" + label, parse_mode="Markdown")
    return await tutor_menu(update, ctx)

# ── ADMIN APPROVAL / REJECTION ────────────────────────────────────────────────
async def app_tutor(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if not is_admin(update.effective_user.id):
        return
    uid = int(q.data.replace("app_t_", ""))
    row = db.execute("SELECT actioned_by FROM tutors WHERE user_id=%s", (uid,), fetch="one")
    if row and row["actioned_by"]:
        await q.answer("⚠️ Already actioned.", show_alert=True); return
    actor = update.effective_user.username or str(update.effective_user.id)
    db.execute("UPDATE tutors SET approved=1, actioned_by=%s WHERE user_id=%s", (update.effective_user.id, uid))
    asyncio.create_task(log_to_sheets_async(sheets.approve_tutor_sheet, uid))
    await q.edit_message_text(
        q.message.text + "\n\n" + DIV2 + "\n✅ *Approved* by @" + actor,
        parse_mode="Markdown",
    )
    try:
        await ctx.bot.send_message(
            uid,
            hdr("✅", "Profile Approved") + "\n\n"
            "Your profile has been *approved* by CognifySG!\n\n"
            "Browse open requests from the channel or use /start → Tutor.",
            parse_mode="Markdown",
        )
    except Exception:
        pass

async def rej_tutor(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if not is_admin(update.effective_user.id):
        return
    uid = int(q.data.replace("rej_t_", ""))
    row = db.execute("SELECT actioned_by FROM tutors WHERE user_id=%s", (uid,), fetch="one")
    if row and row["actioned_by"]:
        await q.answer("⚠️ Already actioned.", show_alert=True); return
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(r, callback_data="tr_" + str(uid) + "|" + r)] for r in REJECT_TUTOR] +
                              [[InlineKeyboardButton("🔙 Cancel", callback_data="trc_" + str(uid))]])
    await q.edit_message_text(
        q.message.text + "\n\n" + DIV2 + "\n⚠️ *Select rejection reason:*",
        reply_markup=kb,
        parse_mode="Markdown",
    )

async def rej_tutor_reason(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if not is_admin(update.effective_user.id):
        return
    parts  = q.data.replace("tr_", "").split("|", 1)
    uid    = int(parts[0])
    reason = parts[1]
    actor  = update.effective_user.username or str(update.effective_user.id)
    row    = db.execute("SELECT actioned_by FROM tutors WHERE user_id=%s", (uid,), fetch="one")
    if row and row["actioned_by"]:
        await q.answer("⚠️ Already actioned.", show_alert=True); return
    db.execute("DELETE FROM tutors WHERE user_id=%s", (uid,))
    await q.edit_message_text(
        q.message.text.split("\n\n" + DIV2)[0] + "\n\n" + DIV2 +
        "\n❌ *Rejected* by @" + actor + "\n" + fld("Reason", reason),
        parse_mode="Markdown",
    )
    try:
        await ctx.bot.send_message(
            uid,
            hdr("❌", "Application Unsuccessful") + "\n\n" +
            fld("Reason", reason) + "\n\n_You may re-apply using /start._",
            parse_mode="Markdown",
        )
    except Exception:
        pass

async def rej_tutor_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    uid = int(q.data.replace("trc_", ""))
    kb  = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data="app_t_" + str(uid)),
        InlineKeyboardButton("❌ Reject",  callback_data="rej_t_" + str(uid)),
    ]])
    await q.edit_message_text(
        q.message.text.split("\n\n" + DIV2)[0],
        reply_markup=kb,
        parse_mode="Markdown",
    )

# ── ADMIN: CONFIRM MATCH ──────────────────────────────────────────────────────
async def confirm_match(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if not is_admin(update.effective_user.id):
        await q.answer("⛔️ Admin only.", show_alert=True); return

    parts    = q.data.replace("confirm_match_", "").split("_")
    req_id   = int(parts[0])
    tutor_id = int(parts[1])
    actor    = update.effective_user.username or str(update.effective_user.id)

    req   = db.execute("SELECT * FROM requests WHERE id=%s", (req_id,), fetch="one")
    tutor = db.execute("SELECT * FROM tutors WHERE user_id=%s", (tutor_id,), fetch="one")
    if not req or not tutor:
        await q.answer("Record not found.", show_alert=True); return
    if req["status"] == "matched":
        await q.answer("⚠️ Already matched.", show_alert=True); return

    # Lock: first admin to action wins
    existing = db.execute(
        "SELECT actioned_by FROM matches WHERE request_id=%s AND tutor_id=%s",
        (req_id, tutor_id), fetch="one",
    )
    if existing and existing["actioned_by"]:
        await q.answer("⚠️ Already confirmed by another admin.", show_alert=True); return

    # Get applied_rate for this application
    app_row = db.execute(
        "SELECT applied_rate FROM applications WHERE tutor_id=%s AND request_id=%s",
        (tutor_id, req_id), fetch="one",
    )
    applied_rate = app_row["applied_rate"] if app_row else 0

    match_id = db.execute(
        "INSERT INTO matches (request_id, tutor_id, parent_id, confirmed_by, actioned_by) "
        "VALUES (%s,%s,%s,%s,%s) RETURNING id",
        (req_id, tutor_id, req["parent_id"], update.effective_user.id, update.effective_user.id),
        fetch="id",
    )
    db.execute("UPDATE requests SET status='matched', matched_tutor_id=%s WHERE id=%s", (tutor_id, req_id))
    db.execute("UPDATE tutors SET available=0 WHERE user_id=%s", (tutor_id,))

    asyncio.create_task(log_to_sheets_async(
        sheets.log_match,
        match_id, req_id, tutor["name"], tutor["phone"],
        req["name"], req["phone"], req["subject"], applied_rate, actor,
    ))
    asyncio.create_task(log_to_sheets_async(sheets.log_revenue, match_id, tutor["name"], PLACEMENT_FEE))

    location = (req.get("town") or "") + (", " + req.get("postal_code", "") if req.get("postal_code") else "")
    await q.edit_message_text(
        q.message.text + "\n\n" + DIV2 +
        "\n✅ *Match confirmed* by @" + actor + "\n▸ *Match ID:* M" + str(match_id),
        parse_mode="Markdown",
    )

    async def notify_tutor():
        try:
            await ctx.bot.send_message(
                tutor_id,
                hdr("🎉", "Match Confirmed!") + "\n\n"
                "Congratulations! You have been matched.\n\n" +
                DIV2 + "\n" +
                fld("Parent name",    req["name"]) + "\n" +
                fld("WhatsApp",       req["phone"]) + "\n" +
                fld("Subject needed", req["subject"]) + "\n" +
                fld("Level",          req["level"]) + "\n" +
                fld("Location",       location) + "\n" +
                fld("Budget",         rate_str(req["budget"])) + "\n" +
                fld("Your rate",      rate_str(applied_rate)) + "\n\n" +
                DIV2 + "\n"
                "💰 A placement fee of *$" + str(PLACEMENT_FEE) + "* is due to CognifySG.\n"
                "_Contact the parent on WhatsApp to arrange the first lesson!_",
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.warning("Could not notify tutor %s: %s", tutor_id, e)

    async def notify_parent():
        try:
            await ctx.bot.send_message(
                req["parent_id"],
                hdr("🎉", "Tutor Found!") + "\n\n"
                "We have matched you with a tutor!\n\n" +
                DIV2 + "\n" +
                fld("Tutor name", tutor["name"]) + "\n" +
                fld("WhatsApp",   tutor["phone"]) + "\n" +
                fld("Subjects",   tutor["subjects"]) + "\n" +
                fld("Levels",     tutor["levels"]) + "\n" +
                fld("Rate",       rate_str(applied_rate) + " _(agreed for this job)_") + "\n\n" +
                DIV2 + "\n_Contact your tutor on WhatsApp to arrange the first lesson._",
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.warning("Could not notify parent %s: %s", req["parent_id"], e)

    asyncio.create_task(notify_tutor())
    asyncio.create_task(notify_parent())

# ── ADMIN COMMANDS ────────────────────────────────────────────────────────────
async def open_requests(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔️ Admin access required.")
        return
    reqs = db.execute("""
        SELECT r.id, r.subject, r.level, r.town, r.areas, r.budget, r.name as parent,
               COUNT(a.id) as applicants
        FROM requests r
        LEFT JOIN applications a ON a.request_id = r.id
        WHERE r.status='open' AND r.approved=1
        GROUP BY r.id ORDER BY applicants DESC, r.created_at ASC
    """, fetch="all")
    if not reqs:
        await update.message.reply_text(
            hdr("📋", "Open Requests") + "\n\n✅ All requests matched!", parse_mode="Markdown"
        )
        return
    lines = [hdr("📋", "Open Requests — " + str(len(reqs)) + " active") + "\n"]
    for r in reqs:
        apps     = r["applicants"]
        icon     = "🔴" if apps == 0 else ("🟡" if apps < 3 else "🟢")
        location = r["town"] if r.get("town") else r.get("areas", "")
        line     = (
            icon + "  *#" + str(r["id"]) + "* — " + r["subject"] + " | " + r["level"] +
            " | " + location + " | " + rate_str(r["budget"]) + "\n"
            "    Parent: " + r["parent"] + "\n"
            "    Applicants: *" + str(apps) + "*"
        )
        if apps > 0:
            line += " — /applicants " + str(r["id"])
        lines.append(line)
    lines.append("\n" + DIV2 + "\n🔴 None  🟡 1-2  🟢 3+")
    await update.message.reply_text("\n\n".join(lines), parse_mode="Markdown")

async def view_applicants(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔️ Admin access required.")
        return
    if not ctx.args:
        await update.message.reply_text(
            hdr("📊", "View Applicants") + "\n\nUsage: `/applicants REQUEST_ID`", parse_mode="Markdown"
        )
        return
    try:
        req_id = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("⚠️ Provide a valid request ID.")
        return
    req = db.execute("SELECT * FROM requests WHERE id=%s", (req_id,), fetch="one")
    if not req:
        await update.message.reply_text("⚠️ Request #" + str(req_id) + " not found.")
        return
    apps = db.execute("""
        SELECT t.user_id, t.name, t.phone, t.username,
               t.subjects, t.levels, t.areas,
               a.applied_rate, t.rating_avg, t.rating_count, a.match_score
        FROM applications a
        JOIN tutors t ON t.user_id = a.tutor_id
        WHERE a.request_id=%s ORDER BY a.match_score DESC
    """, (req_id,), fetch="all")
    if not apps:
        await update.message.reply_text(
            hdr("📊", "Applicants for #" + str(req_id)) + "\n\nNo applicants yet.", parse_mode="Markdown"
        )
        return

    location = (req.get("town") or "") + (", " + req.get("postal_code", "") if req.get("postal_code") else "")
    msg  = (
        hdr("📊", "Applicants — Request #" + str(req_id)) + "\n\n" +
        fld("Subject",  req["subject"]) + "\n" +
        fld("Level",    req["level"]) + "\n" +
        fld("Location", location) + "\n" +
        fld("Budget",   rate_str(req["budget"])) + "\n" +
        fld("Parent",   req["name"]) + "\n" +
        fld("Contact",  req["phone"]) + "\n\n" +
        DIV + "\n*" + str(len(apps)) + " Applicant(s) — ranked by score*\n" + DIV + "\n\n"
    )
    medals  = ["🥇", "🥈", "🥉"]
    kb_rows = []
    for i, a in enumerate(apps, 1):
        medal  = medals[i - 1] if i <= 3 else str(i) + "."
        handle = "@" + a["username"] if a["username"] else "No username"
        rating = ("⭐ " + str(a["rating_avg"]) + " (" + str(a["rating_count"]) + ")") if a["rating_count"] else "No ratings"
        ar     = a["applied_rate"] or 0
        msg   += (
            medal + "  *" + a["name"] + "*  —  Score: *" + str(a["match_score"]) + "/100*\n" +
            fld("WhatsApp", a["phone"]) + "\n" +
            fld("Telegram", handle) + "\n" +
            fld("Subjects", a["subjects"]) + "\n" +
            fld("Levels",   a["levels"]) + "\n" +
            fld("Rate",     rate_str(ar) + " _(for this job)_") + "\n" +
            fld("Rating",   rating) + "\n"
        )
        if i < len(apps):
            msg += DIV2 + "\n"
        kb_rows.append([InlineKeyboardButton(
            "✅ Match #" + str(i) + " — " + a["name"],
            callback_data="confirm_match_" + str(req_id) + "_" + str(a["user_id"]),
        )])

    await update.message.reply_text(msg, parse_mode="Markdown")
    if kb_rows:
        await update.message.reply_text(
            "Select a tutor to confirm the match:",
            reply_markup=InlineKeyboardMarkup(kb_rows),
        )

async def admin_panel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔️ Admin access required.")
        return
    t_active  = db.execute("SELECT COUNT(*) as n FROM tutors WHERE approved=1", fetch="one")["n"]
    t_pending = db.execute("SELECT COUNT(*) as n FROM tutors WHERE approved=0", fetch="one")["n"]
    r_open    = db.execute("SELECT COUNT(*) as n FROM requests WHERE status='open' AND approved=1", fetch="one")["n"]
    r_pending = db.execute("SELECT COUNT(*) as n FROM requests WHERE approved=0", fetch="one")["n"]
    matched   = db.execute("SELECT COUNT(*) as n FROM matches", fetch="one")["n"]
    total_apps = db.execute("SELECT COUNT(*) as n FROM applications", fetch="one")["n"]
    await update.message.reply_text(
        hdr("⚙️", "Admin Panel — CognifySG") + "\n\n"
        "👨‍🏫 *Tutors*\n" +
        fld("Active",  t_active) + "\n" +
        fld("Pending", t_pending) + "\n\n"
        "👨‍👩‍👧 *Requests*\n" +
        fld("Open",    r_open) + "\n" +
        fld("Pending", r_pending) + "\n\n" +
        fld("Total Matches",      matched) + "\n" +
        fld("Total Applications", total_apps) + "\n\n" +
        DIV2 + "\n"
        "_Commands:_\n"
        "`/open` — Open requests dashboard\n"
        "`/applicants ID` — Compare applicants\n"
        "`/addadmin ID` — Add admin\n"
        "`/removeadmin ID` — Remove admin\n"
        "`/listadmins` — View team\n"
        "`/terms` — Terms summary",
        parse_mode="Markdown",
    )

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
    db.execute(
        "INSERT INTO admins(user_id, name, added_by) VALUES(%s,'Admin',%s) ON CONFLICT DO NOTHING",
        (new_id, update.effective_user.id),
    )
    await update.message.reply_text(
        hdr("✅", "Admin Added") + "\n\nUser `" + str(new_id) + "` now has admin access.",
        parse_mode="Markdown",
    )
    try:
        await ctx.bot.send_message(
            new_id,
            hdr("🔑", "Admin Access Granted") + "\n\n"
            "You are now an admin of *CognifySG.*\n\n"
            "`/open` `/applicants ID` `/admin` `/listadmins`",
            parse_mode="Markdown",
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
    await update.message.reply_text(
        hdr("✅", "Admin Removed") + "\n\nUser `" + str(rid) + "` removed.",
        parse_mode="Markdown",
    )

async def list_admins(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔️ Admin access required.")
        return
    admins = db.execute("SELECT user_id, username, added_at FROM admins ORDER BY added_at", fetch="all")
    lines  = []
    for a in admins:
        crown  = "👑 " if a["user_id"] == SUPER_ADMIN_ID else "🔑 "
        handle = "@" + a["username"] if a["username"] else "`" + str(a["user_id"]) + "`"
        lines.append(crown + handle)
    await update.message.reply_text(
        hdr("👥", "Admin Team") + "\n\n" + "\n".join(lines) +
        "\n\n" + DIV2 + "\n_" + str(len(admins)) + " admins total_",
        parse_mode="Markdown",
    )

async def terms_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        hdr("📋", "Terms of Service") + "\n\n" +
        fld("Terms",   TERMS_URL) + "\n" +
        fld("Privacy", PRIVACY_URL) + "\n\n" +
        DIV2 + "\n"
        "▸ No direct solicitation outside the platform\n"
        "▸ Placement fee: $" + str(PLACEMENT_FEE) + " per successful match\n"
        "▸ Data per PDPA Singapore\n"
        "▸ Delete your data: /deleteaccount",
        parse_mode="Markdown",
    )

# ── USER COMMANDS ─────────────────────────────────────────────────────────────
async def profile_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid   = update.effective_user.id
    tutor = db.execute("SELECT * FROM tutors WHERE user_id=%s", (uid,), fetch="one")
    if tutor:
        status = "🟢 Available" if tutor["available"] else "🔴 Unavailable"
        await update.message.reply_text(
            hdr("👤", "Your Tutor Profile") + "\n\n" +
            fld("Name",     tutor["name"]) + "\n" +
            fld("Phone",    tutor["phone"]) + "\n" +
            fld("Subjects", tutor["subjects"]) + "\n" +
            fld("Levels",   tutor["levels"]) + "\n" +
            fld("Areas",    tutor["areas"]) + "\n" +
            fld("Rate",     "Set per application") + "\n" +
            fld("Status",   status) + "\n" +
            fld("Approval", "✅ Approved" if tutor["approved"] else "⏳ Pending"),
            parse_mode="Markdown",
        )
        return
    parent = db.execute("SELECT * FROM requests WHERE parent_id=%s LIMIT 1", (uid,), fetch="one")
    if parent:
        await update.message.reply_text(
            hdr("👨‍👩‍👧", "Your Parent Profile") + "\n\n" +
            fld("Name",     parent["name"]) + "\n" +
            fld("Phone",    parent["phone"]) + "\n" +
            fld("Telegram", "@" + parent["username"] if parent["username"] else "none"),
            parse_mode="Markdown",
        )
        return
    await update.message.reply_text("You are not registered yet. Use /start to begin.")

async def myrequests_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid   = update.effective_user.id
    tutor = db.execute("SELECT * FROM tutors WHERE user_id=%s", (uid,), fetch="one")
    if tutor:
        apps = db.execute("""
            SELECT a.request_id, r.subject, r.level, a.match_score,
                   a.applied_rate, r.status, a.created_at
            FROM applications a JOIN requests r ON r.id = a.request_id
            WHERE a.tutor_id=%s ORDER BY a.created_at DESC
        """, (uid,), fetch="all")
        if not apps:
            await update.message.reply_text("You haven't applied for any requests.")
            return
        lines = [hdr("📋", "Your Applications") + "\n"]
        for a in apps:
            icon = "✅ Matched" if a["status"] == "matched" else "🟡 Pending"
            ar   = a["applied_rate"] or 0
            lines.append(
                "*#" + str(a["request_id"]) + "* — " + a["subject"] + " | " + a["level"] + "\n" +
                "   Score: " + str(a["match_score"]) + "/100" +
                ("  |  Rate: " + rate_str(ar) if ar else "") +
                "  |  " + icon
            )
        await update.message.reply_text("\n\n".join(lines), parse_mode="Markdown")
        return
    reqs = db.execute(
        "SELECT id, subject, level, status, created_at FROM requests WHERE parent_id=%s ORDER BY created_at DESC",
        (uid,), fetch="all",
    )
    if not reqs:
        await update.message.reply_text("You haven't posted any requests.")
        return
    lines = [hdr("📋", "Your Requests") + "\n"]
    for r in reqs:
        icon = "✅" if r["status"] == "matched" else "🟡"
        lines.append(icon + "  *#" + str(r["id"]) + "* — " + r["subject"] + " | " + r["level"] + " (" + r["status"] + ")")
    await update.message.reply_text("\n\n".join(lines), parse_mode="Markdown")

async def myapplications_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await myrequests_cmd(update, ctx)

async def cancel_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text("Cancelled. Use /start to begin again.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

# ── DELETE ACCOUNT ────────────────────────────────────────────────────────────
async def delete_account(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("⚠️ Yes, delete all my data", callback_data="confirm_delete"),
        InlineKeyboardButton("Cancel", callback_data="cancel_delete"),
    ]])
    await update.message.reply_text(
        hdr("🗑️", "Delete Account") + "\n\n"
        "This will permanently delete *all your data*:\n\n"
        "▸ Your tutor / parent profile\n"
        "▸ All requests and applications\n"
        "▸ Terms acceptance record\n\n" +
        DIV2 + "\n⚠️ *This cannot be undone.*",
        reply_markup=kb,
        parse_mode="Markdown",
    )

async def confirm_delete(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    uid = q.from_user.id
    for sql in [
        "DELETE FROM tutors       WHERE user_id=%s",
        "DELETE FROM requests     WHERE parent_id=%s",
        "DELETE FROM applications WHERE tutor_id=%s",
        "DELETE FROM terms_accepted WHERE user_id=%s",
        "DELETE FROM blocked      WHERE user_id=%s",
    ]:
        db.execute(sql, (uid,))
    await q.edit_message_text(
        hdr("✅", "Account Deleted") + "\n\n"
        "All your data has been permanently removed from CognifySG.\n\n"
        "_As required under PDPA Singapore._",
        parse_mode="Markdown",
    )

async def cancel_delete(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    await q.edit_message_text("Deletion cancelled. Your account is safe.")

# ── BACK BUTTONS ──────────────────────────────────────────────────────────────
async def back_t(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Clear any in-progress application flags
    ctx.user_data.pop("in_app_edit", None)
    ctx.user_data.pop("apply_req_id", None)
    return await tutor_menu(update, ctx)

async def back_p(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    return await parent_menu(update, ctx)

async def back_to_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("👨‍🏫 I am a Tutor",  callback_data="role_tutor"),
        InlineKeyboardButton("👨‍👩‍👧 I am a Parent", callback_data="role_parent"),
    ]])
    await q.edit_message_text(
        hdr("🎓", "CognifySG") + "\n\nSingapore's premier tuition matching platform.\n\n" +
        DIV2 + "\nPlease identify yourself to continue:\n" + DIV2,
        reply_markup=kb,
        parse_mode="Markdown",
    )
    return ROLE_SELECT

# ── POST INIT ─────────────────────────────────────────────────────────────────
async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start",         "Open main menu"),
        BotCommand("profile",       "View your profile"),
        BotCommand("myrequests",    "View your requests / applications"),
        BotCommand("deleteaccount", "Delete all your data (PDPA)"),
        BotCommand("cancel",        "Cancel current operation"),
        BotCommand("terms",         "View Terms & Privacy Policy"),
    ])
    logger.info("Bot commands registered.")

# ── MAIN ───────────────────────────────────────────────────────────────────────
def main():
    db.init_db()
    db.execute(
        "INSERT INTO admins(user_id, name, added_by) VALUES(%s,'Super Admin',%s) ON CONFLICT DO NOTHING",
        (SUPER_ADMIN_ID, SUPER_ADMIN_ID),
    )

    app = (
        Application.builder()
        .token(TOKEN)
        .concurrent_updates(True)
        .post_init(post_init)
        .build()
    )

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start",              start),
            CallbackQueryHandler(start_welcome_callback, pattern="^start_welcome$"),
            CallbackQueryHandler(post_req_start,         pattern="^post_req$"),
            CallbackQueryHandler(edit_profile_menu,      pattern="^edit_profile$"),   # fix: proper entry
            CallbackQueryHandler(apply_req,              pattern="^apply_\\d+$"),     # apply from browse
        ],
        states={
            TERMS:    [CallbackQueryHandler(terms_accept, pattern="^terms_accept$")],
            CAPTCHA:  [CallbackQueryHandler(captcha_cb,   pattern="^cap\\|")],
            ROLE_SELECT: [CallbackQueryHandler(role_select, pattern="^role_")],

            T_NAME:     [MessageHandler(filters.TEXT & ~filters.COMMAND, t_name)],
            T_PHONE:    [MessageHandler(filters.TEXT & ~filters.COMMAND, t_phone)],
            T_SUBJECTS: [CallbackQueryHandler(t_subjects, pattern="^tsubj\\|")],
            T_LEVELS:   [CallbackQueryHandler(t_levels,   pattern="^tlvl\\|")],
            T_AREAS:    [CallbackQueryHandler(t_areas,    pattern="^tarea\\|")],

            P_NAME:    [MessageHandler(filters.TEXT & ~filters.COMMAND, p_name)],
            P_PHONE:   [MessageHandler(filters.TEXT & ~filters.COMMAND, p_phone)],
            P_SUBJECT: [CallbackQueryHandler(p_subject, pattern="^psubj\\|")],
            P_LEVEL:   [CallbackQueryHandler(p_level,   pattern="^plvl\\|")],
            P_TOWN:    [CallbackQueryHandler(p_town,    pattern="^ptown\\|")],
            P_POSTAL:  [MessageHandler(filters.TEXT & ~filters.COMMAND, p_postal)],
            P_BUDGET:  [MessageHandler(filters.TEXT & ~filters.COMMAND, p_budget)],

            EDIT_TUTOR_MENU: [
                CallbackQueryHandler(edit_profile_menu,  pattern="^edit_profile$"),
                CallbackQueryHandler(edit_name,          pattern="^edit_name$"),
                CallbackQueryHandler(edit_phone,         pattern="^edit_phone$"),
                CallbackQueryHandler(edit_subjects,      pattern="^edit_subjects$"),
                CallbackQueryHandler(edit_levels,        pattern="^edit_levels$"),
                CallbackQueryHandler(edit_areas,         pattern="^edit_areas$"),
                CallbackQueryHandler(edit_rate,          pattern="^edit_rate$"),
                CallbackQueryHandler(app_continue_rate,  pattern="^app_continue_rate$"),
                CallbackQueryHandler(back_t,             pattern="^back_t$"),
            ],
            EDIT_NAME:     [MessageHandler(filters.TEXT & ~filters.COMMAND, update_name)],
            EDIT_PHONE:    [MessageHandler(filters.TEXT & ~filters.COMMAND, update_phone)],
            EDIT_SUBJECTS: [CallbackQueryHandler(edit_subjects_cb, pattern="^esubj\\|")],
            EDIT_LEVELS:   [CallbackQueryHandler(edit_levels_cb,   pattern="^elvl\\|")],
            EDIT_AREAS:    [CallbackQueryHandler(edit_areas_cb,    pattern="^eara\\|")],
            EDIT_RATE:     [MessageHandler(filters.TEXT & ~filters.COMMAND, update_rate)],

            APP_EDIT_PROMPT: [
                CallbackQueryHandler(app_noedit,  pattern="^app_noedit$"),
                CallbackQueryHandler(app_doedit,  pattern="^app_doedit$"),
            ],
            APP_RATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, app_rate_input)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_cmd),
            CommandHandler("start",  start),
        ],
        per_message=False,
        allow_reentry=True,
    )

    app.add_handler(conv)

    # ── User commands ──────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("profile",        profile_cmd))
    app.add_handler(CommandHandler("myrequests",     myrequests_cmd))
    app.add_handler(CommandHandler("myapplications", myapplications_cmd))
    app.add_handler(CommandHandler("cancel",         cancel_cmd))
    app.add_handler(CommandHandler("deleteaccount",  delete_account))
    app.add_handler(CommandHandler("terms",          terms_cmd))

    # ── Admin commands ─────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("open",        open_requests))
    app.add_handler(CommandHandler("applicants",  view_applicants))
    app.add_handler(CommandHandler("admin",       admin_panel))
    app.add_handler(CommandHandler("addadmin",    add_admin))
    app.add_handler(CommandHandler("removeadmin", remove_admin))
    app.add_handler(CommandHandler("listadmins",  list_admins))

    # ── Standalone callback handlers ───────────────────────────────────────────
    app.add_handler(CallbackQueryHandler(browse_reqs,      pattern="^browse_reqs$"))
    app.add_handler(CallbackQueryHandler(applied_postings, pattern="^applied_postings$"))
    app.add_handler(CallbackQueryHandler(req_nav,          pattern="^req_(next|prev)$"))
    app.add_handler(CallbackQueryHandler(confirm_match,    pattern="^confirm_match_"))
    app.add_handler(CallbackQueryHandler(view_t_profile,   pattern="^view_t_profile$"))
    app.add_handler(CallbackQueryHandler(toggle_avail,     pattern="^toggle_avail$"))
    app.add_handler(CallbackQueryHandler(my_reqs,          pattern="^my_reqs$"))
    app.add_handler(CallbackQueryHandler(back_t,           pattern="^back_t$"))
    app.add_handler(CallbackQueryHandler(back_p,           pattern="^back_p$"))
    app.add_handler(CallbackQueryHandler(back_to_start,    pattern="^back_to_start$"))
    app.add_handler(CallbackQueryHandler(app_tutor,        pattern="^app_t_\\d+$"))
    app.add_handler(CallbackQueryHandler(rej_tutor,        pattern="^rej_t_\\d+$"))
    app.add_handler(CallbackQueryHandler(rej_tutor_reason, pattern="^tr_"))
    app.add_handler(CallbackQueryHandler(rej_tutor_cancel, pattern="^trc_"))
    app.add_handler(CallbackQueryHandler(confirm_delete,   pattern="^confirm_delete$"))
    app.add_handler(CallbackQueryHandler(cancel_delete,    pattern="^cancel_delete$"))

    # ── Catch-all for non-registered users ────────────────────────────────────
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, welcome))

    app.add_error_handler(error_handler)

    logger.info("CognifySG v7 starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()

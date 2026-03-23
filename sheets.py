"""
CognifySG — Google Sheets Sync
Auto-writes all events to Google Sheets
Setup: set GOOGLE_CREDENTIALS env var with service account JSON (base64 encoded)
"""

import os
import json
import base64
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

_client  = None
_sheet   = None
SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "")

def _get_client():
    global _client
    if _client:
        return _client
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        creds_b64 = os.environ.get("GOOGLE_CREDENTIALS", "")
        if not creds_b64:
            logger.warning("GOOGLE_CREDENTIALS not set — Sheets sync disabled.")
            return None
        creds_json = json.loads(base64.b64decode(creds_b64).decode())
        scopes = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive",
        ]
        creds   = Credentials.from_service_account_info(creds_json, scopes=scopes)
        _client = gspread.authorize(creds)
        return _client
    except Exception as e:
        logger.error("Sheets init error: %s", e)
        return None

def _get_sheet():
    global _sheet
    if _sheet:
        return _sheet
    client = _get_client()
    if not client or not SHEET_ID:
        return None
    try:
        _sheet = client.open_by_key(SHEET_ID)
        _ensure_tabs(_sheet)
        return _sheet
    except Exception as e:
        logger.error("Sheets open error: %s", e)
        return None

def _ensure_tabs(sheet):
    existing = [w.title for w in sheet.worksheets()]
    tabs = {
        "Tutors":   ["ID","Name","WhatsApp","Telegram","Subjects","Levels","Areas","Rate","Status","Approved","Registered"],
        "Requests": ["ID","Parent","WhatsApp","Telegram","Subject","Level","Area","Budget","Status","Applicants","Posted"],
        "Matches":  ["Match ID","Request ID","Tutor","Tutor WA","Parent","Parent WA","Subject","Rate","Confirmed By","Date"],
        "Revenue":  ["Match ID","Tutor","Fee","Payment Status","Date","Running Total"],
        "Ratings":  ["Match ID","Tutor","Tutor Rating","Parent Rating","Date"],
    }
    running = 0
    for tab, headers in tabs.items():
        if tab not in existing:
            ws = sheet.add_worksheet(title=tab, rows=1000, cols=len(headers))
            ws.append_row(headers, value_input_option="RAW")

def _append(tab, row):
    try:
        sheet = _get_sheet()
        if not sheet:
            return
        ws = sheet.worksheet(tab)
        ws.append_row(row, value_input_option="USER_ENTERED")
    except Exception as e:
        logger.error("Sheets append error (%s): %s", tab, e)

def _update_cell(tab, search_col, search_val, update_col, update_val):
    try:
        sheet = _get_sheet()
        if not sheet:
            return
        ws   = sheet.worksheet(tab)
        cell = ws.find(str(search_val), in_column=search_col)
        if cell:
            ws.update_cell(cell.row, update_col, update_val)
    except Exception as e:
        logger.error("Sheets update error (%s): %s", tab, e)

def log_tutor(tutor_id, name, phone, username, subjects, levels, areas, rate):
    now = datetime.now().strftime("%d %b %Y")
    _append("Tutors", [
        "T" + str(tutor_id)[-4:], name, phone,
        "@" + username if username else "—",
        subjects, levels, areas, "$" + str(rate) + "/hr",
        "Pending", "No", now
    ])

def approve_tutor_sheet(tutor_id):
    _update_cell("Tutors", 1, "T" + str(tutor_id)[-4:], 10, "Yes")
    _update_cell("Tutors", 1, "T" + str(tutor_id)[-4:], 9,  "Active")

def log_request(req_id, name, phone, username, subject, level, areas, budget):
    now = datetime.now().strftime("%d %b %Y")
    _append("Requests", [
        "#" + str(req_id), name, phone,
        "@" + username if username else "—",
        subject, level, areas, "$" + str(budget) + "/hr",
        "Pending", 0, now
    ])

def approve_request_sheet(req_id):
    _update_cell("Requests", 1, "#" + str(req_id), 9, "Open")

def update_applicant_count(req_id, count):
    _update_cell("Requests", 1, "#" + str(req_id), 10, count)

def log_match(match_id, req_id, tutor_name, tutor_phone,
              parent_name, parent_phone, subject, rate, confirmed_by):
    now = datetime.now().strftime("%d %b %Y")
    _append("Matches", [
        "M" + str(match_id), "#" + str(req_id),
        tutor_name, tutor_phone,
        parent_name, parent_phone,
        subject, "$" + str(rate) + "/hr",
        "@" + confirmed_by if confirmed_by else "admin",
        now
    ])
    _update_cell("Requests", 1, "#" + str(req_id), 9, "Matched")

def log_revenue(match_id, tutor_name, fee):
    now = datetime.now().strftime("%d %b %Y")
    _append("Revenue", [
        "M" + str(match_id), tutor_name,
        "$" + str(fee), "Pending", now, "=SUM(C2:C" + str(match_id + 1) + ")"
    ])

def log_rating(match_id, tutor_name, tutor_rating, parent_rating):
    now = datetime.now().strftime("%d %b %Y")
    _append("Ratings", [
        "M" + str(match_id), tutor_name,
        tutor_rating or "—", parent_rating or "—", now
    ])

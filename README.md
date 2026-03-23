# CognifySG v6 — Production Tuition Agency Bot

## Files
- `main.py` — Telegram bot (deploy to Railway)
- `db.py` — PostgreSQL database layer
- `sheets.py` — Google Sheets auto-sync
- `dashboard.py` — Streamlit admin web UI
- `requirements.txt` — Python dependencies
- `Dockerfile` — Railway deployment

## Railway Setup (Bot)

1. Push all files to your GitHub repo
2. Railway → New Project → Deploy from GitHub
3. Add Variables (see .env.example):
   - `TOKEN` — BotFather token
   - `ADMIN_CHAT_ID` — Your Telegram ID
   - `DATABASE_URL` — From Railway PostgreSQL plugin

### Add PostgreSQL on Railway
1. Railway → your project → + New → Database → PostgreSQL
2. Click the database → Variables tab → copy DATABASE_URL
3. Paste into your bot service Variables as DATABASE_URL

## Streamlit Dashboard (Admin Web UI)

Run locally:
  pip install -r requirements.txt
  streamlit run dashboard.py

Or deploy to Streamlit Cloud (free):
1. Push dashboard.py to GitHub
2. Go to share.streamlit.io
3. Connect your repo
4. Set DATABASE_URL in Streamlit secrets

## Google Sheets Setup (Optional)

1. Go to console.cloud.google.com
2. Create a project → Enable "Google Sheets API" and "Google Drive API"
3. Create Service Account → download JSON key
4. Base64 encode: base64 -i service_account.json (Mac/Linux)
5. Add as GOOGLE_CREDENTIALS in Railway Variables
6. Create a Google Sheet → Share with service account email (editor access)
7. Copy Sheet ID from URL → add as GOOGLE_SHEET_ID

## Admin Commands

| Command | Description |
|---|---|
| `/open` | Live dashboard of all open requests with applicant counts |
| `/applicants ID` | Compare all applicants for a request, ranked by match score |
| `/admin` | Full stats dashboard |
| `/addadmin ID` | Grant admin access to a team member |
| `/removeadmin ID` | Revoke admin access |
| `/listadmins` | View full admin team |
| `/terms` | View current T&Cs and privacy policy |

## Matching Score

Each applicant is automatically scored 0–100:
- Subject match: 40 points
- Level match: 30 points
- Area match: 20 points
- Rate within budget: 10 points
- Rating bonus: up to 10 points

Applicants are always shown ranked by score in `/applicants`.

## PDPA Compliance

- Terms acceptance required before registration
- `/deleteaccount` permanently removes all user data
- Phone number uniqueness enforced
- No data shared between tutors/parents without admin confirmation

## Revenue Tracking

Set PLACEMENT_FEE in Railway Variables (default: $40).
Revenue tab in Streamlit dashboard shows all matches, fee status, and running total.
Mark fees as paid directly from the dashboard.

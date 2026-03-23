"""
CognifySG — Admin Web Dashboard
Run: streamlit run dashboard.py
"""

import os
import streamlit as st
import psycopg2
import psycopg2.extras
import pandas as pd
from datetime import datetime

DATABASE_URL = os.environ.get("DATABASE_URL", "")

st.set_page_config(
    page_title="CognifySG Admin",
    page_icon="🎓",
    layout="wide"
)

@st.cache_resource
def get_conn():
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)

def query(sql, params=()):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    except Exception as e:
        conn.rollback()
        st.error("DB error: " + str(e))
        return []

def execute(sql, params=()):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            conn.commit()
    except Exception as e:
        conn.rollback()
        st.error("DB error: " + str(e))

# ── SIDEBAR ────────────────────────────────────────────────────────────────────
st.sidebar.image("https://via.placeholder.com/200x60?text=CognifySG", use_column_width=True)
page = st.sidebar.radio("Navigate", [
    "📊 Dashboard",
    "👨‍🏫 Tutors",
    "👨‍👩‍👧 Requests",
    "🎯 Applications",
    "✅ Matches",
    "💰 Revenue",
    "⭐ Ratings",
    "🚫 Blocked",
    "⚠️ Error Log",
])

# ── DASHBOARD ──────────────────────────────────────────────────────────────────
if page == "📊 Dashboard":
    st.title("CognifySG — Admin Dashboard")
    st.caption("Live overview of your tuition matching platform")

    c1, c2, c3, c4, c5 = st.columns(5)
    tutors_active  = query("SELECT COUNT(*) as n FROM tutors  WHERE approved=1")[0]["n"]
    tutors_pending = query("SELECT COUNT(*) as n FROM tutors  WHERE approved=0")[0]["n"]
    reqs_open      = query("SELECT COUNT(*) as n FROM requests WHERE status='open' AND approved=1")[0]["n"]
    reqs_pending   = query("SELECT COUNT(*) as n FROM requests WHERE approved=0")[0]["n"]
    matches_total  = query("SELECT COUNT(*) as n FROM matches")[0]["n"]

    c1.metric("Active Tutors",    tutors_active)
    c2.metric("Pending Tutors",   tutors_pending)
    c3.metric("Open Requests",    reqs_open)
    c4.metric("Pending Requests", reqs_pending)
    c5.metric("Total Matches",    matches_total)

    st.divider()

    col1, col2 = st.columns(2)

    with col1:
        st.subheader("Open Requests with Applicants")
        open_reqs = query("""
            SELECT r.id, r.name, r.subject, r.level, r.areas, r.budget,
                   COUNT(a.id) as applicants
            FROM requests r
            LEFT JOIN applications a ON a.request_id = r.id
            WHERE r.status='open' AND r.approved=1
            GROUP BY r.id ORDER BY applicants DESC, r.created_at ASC
        """)
        if open_reqs:
            df = pd.DataFrame(open_reqs)
            df["budget"] = df["budget"].apply(lambda x: "$" + str(x) + "/hr")
            st.dataframe(df, use_container_width=True, hide_index=True)
        else:
            st.info("No open requests.")

    with col2:
        st.subheader("Recent Matches")
        recent = query("""
            SELECT m.id, t.name as tutor, r.name as parent,
                   req.subject, req.budget, m.created_at::date as date
            FROM matches m
            JOIN tutors t ON t.user_id = m.tutor_id
            JOIN requests req ON req.id = m.request_id
            JOIN requests r ON r.id = m.request_id
            ORDER BY m.created_at DESC LIMIT 10
        """)
        if recent:
            st.dataframe(pd.DataFrame(recent), use_container_width=True, hide_index=True)
        else:
            st.info("No matches yet.")

# ── TUTORS ─────────────────────────────────────────────────────────────────────
elif page == "👨‍🏫 Tutors":
    st.title("Tutor Management")

    tab1, tab2 = st.tabs(["All Tutors", "Pending Approval"])

    with tab1:
        search = st.text_input("Search by name or subject")
        if search:
            tutors = query(
                "SELECT * FROM tutors WHERE name ILIKE %s OR subjects ILIKE %s ORDER BY created_at DESC",
                ("%" + search + "%", "%" + search + "%")
            )
        else:
            tutors = query("SELECT * FROM tutors ORDER BY created_at DESC")

        if tutors:
            df = pd.DataFrame(tutors)
            df["rate"] = df["rate"].apply(lambda x: "$" + str(x) + "/hr")
            df["approved"] = df["approved"].apply(lambda x: "✅ Yes" if x else "⏳ Pending")
            df["available"] = df["available"].apply(lambda x: "🟢" if x else "🔴")
            st.dataframe(
                df[["user_id","name","phone","subjects","levels","areas","rate","available","approved","rating_avg","created_at"]],
                use_container_width=True, hide_index=True
            )
        else:
            st.info("No tutors found.")

    with tab2:
        pending = query("SELECT * FROM tutors WHERE approved=0 ORDER BY created_at ASC")
        if pending:
            for t in pending:
                with st.expander("👨‍🏫 " + t["name"] + " — " + t["subjects"]):
                    col1, col2 = st.columns(2)
                    col1.write("**WhatsApp:** " + t["phone"])
                    col1.write("**Subjects:** " + t["subjects"])
                    col1.write("**Levels:** " + t["levels"])
                    col2.write("**Areas:** " + t["areas"])
                    col2.write("**Rate:** $" + str(t["rate"]) + "/hr")
                    col2.write("**Telegram:** @" + (t["username"] or "none"))
                    a, b, _ = st.columns([1, 1, 4])
                    if a.button("✅ Approve", key="app_" + str(t["user_id"])):
                        execute("UPDATE tutors SET approved=1 WHERE user_id=%s", (t["user_id"],))
                        st.success("Approved!")
                        st.rerun()
                    if b.button("❌ Reject", key="rej_" + str(t["user_id"])):
                        execute("DELETE FROM tutors WHERE user_id=%s", (t["user_id"],))
                        st.warning("Rejected and removed.")
                        st.rerun()
        else:
            st.success("No pending tutors.")

# ── REQUESTS ───────────────────────────────────────────────────────────────────
elif page == "👨‍👩‍👧 Requests":
    st.title("Parent Requests")

    tab1, tab2 = st.tabs(["Open Requests", "Pending Approval"])

    with tab1:
        reqs = query("""
            SELECT r.id, r.name, r.phone, r.subject, r.level, r.areas,
                   r.budget, r.status, COUNT(a.id) as applicants, r.created_at::date as posted
            FROM requests r
            LEFT JOIN applications a ON a.request_id = r.id
            WHERE r.approved=1
            GROUP BY r.id ORDER BY r.created_at DESC
        """)
        if reqs:
            df = pd.DataFrame(reqs)
            df["budget"] = df["budget"].apply(lambda x: "$" + str(x) + "/hr")
            st.dataframe(df, use_container_width=True, hide_index=True)
        else:
            st.info("No approved requests.")

    with tab2:
        pending = query("SELECT * FROM requests WHERE approved=0 ORDER BY created_at ASC")
        if pending:
            for r in pending:
                with st.expander("#" + str(r["id"]) + " — " + r["subject"] + " | " + r["name"]):
                    col1, col2 = st.columns(2)
                    col1.write("**WhatsApp:** " + r["phone"])
                    col1.write("**Subject:** " + r["subject"])
                    col1.write("**Level:** " + r["level"])
                    col2.write("**Area:** " + r["areas"])
                    col2.write("**Budget:** $" + str(r["budget"]) + "/hr")
                    a, b, _ = st.columns([1, 1, 4])
                    if a.button("✅ Approve", key="areq_" + str(r["id"])):
                        execute("UPDATE requests SET approved=1 WHERE id=%s", (r["id"],))
                        st.success("Approved!")
                        st.rerun()
                    if b.button("❌ Reject", key="rreq_" + str(r["id"])):
                        execute("DELETE FROM requests WHERE id=%s", (r["id"],))
                        st.warning("Rejected.")
                        st.rerun()
        else:
            st.success("No pending requests.")

# ── APPLICATIONS ───────────────────────────────────────────────────────────────
elif page == "🎯 Applications":
    st.title("Applications — Match View")
    st.caption("Select a request to see all applicants ranked by match score")

    open_reqs = query("""
        SELECT r.id, r.subject, r.level, r.areas, r.budget, r.name as parent,
               COUNT(a.id) as applicants
        FROM requests r
        JOIN applications a ON a.request_id = r.id
        WHERE r.status='open' AND r.approved=1
        GROUP BY r.id ORDER BY applicants DESC
    """)

    if not open_reqs:
        st.info("No open requests with applicants.")
    else:
        options = {
            "#" + str(r["id"]) + " — " + r["subject"] + " | " + r["parent"]: r["id"]
            for r in open_reqs
        }
        selected = st.selectbox("Select a request", list(options.keys()))
        req_id   = options[selected]

        req = query("SELECT * FROM requests WHERE id=%s", (req_id,))[0]

        st.info(
            "**Request #" + str(req["id"]) + "**  |  " +
            req["subject"] + " | " + req["level"] + " | " + req["areas"] +
            " | $" + str(req["budget"]) + "/hr  |  Parent: " + req["name"] +
            " (" + req["phone"] + ")"
        )

        applicants = query("""
            SELECT t.user_id, t.name, t.phone, t.username, t.subjects,
                   t.levels, t.areas, t.rate, t.rating_avg, t.rating_count,
                   a.match_score, a.created_at
            FROM applications a
            JOIN tutors t ON t.user_id = a.tutor_id
            WHERE a.request_id=%s
            ORDER BY a.match_score DESC, a.created_at ASC
        """, (req_id,))

        if not applicants:
            st.warning("No applicants yet.")
        else:
            st.success(str(len(applicants)) + " applicant(s) — ranked by match score")
            for i, a in enumerate(applicants, 1):
                medal = ["🥇","🥈","🥉"][i-1] if i <= 3 else str(i) + "."
                with st.expander(
                    medal + "  " + a["name"] + " — $" + str(a["rate"]) +
                    "/hr  |  Score: " + str(a["match_score"]) + "/100" +
                    ("  |  ⭐ " + str(a["rating_avg"]) if a["rating_count"] > 0 else "")
                ):
                    c1, c2, c3 = st.columns(3)
                    c1.write("**WhatsApp:** " + a["phone"])
                    c1.write("**Telegram:** @" + (a["username"] or "none"))
                    c2.write("**Subjects:** " + a["subjects"])
                    c2.write("**Levels:** " + a["levels"])
                    c3.write("**Areas:** " + a["areas"])
                    c3.write("**Rating:** " + (str(a["rating_avg"]) + " (" + str(a["rating_count"]) + " reviews)") if a["rating_count"] else "No ratings yet")
                    if st.button(
                        "✅ Confirm Match with " + a["name"],
                        key="match_" + str(req_id) + "_" + str(a["user_id"])
                    ):
                        execute("""
                            INSERT INTO matches (request_id, tutor_id, parent_id, confirmed_by)
                            VALUES (%s, %s, %s, %s)
                            ON CONFLICT DO NOTHING
                        """, (req_id, a["user_id"], req["parent_id"], 0))
                        execute(
                            "UPDATE requests SET status='matched', matched_tutor_id=%s WHERE id=%s",
                            (a["user_id"], req_id)
                        )
                        st.success("Match confirmed! Both parties should now be contacted on WhatsApp.")
                        st.rerun()

# ── MATCHES ────────────────────────────────────────────────────────────────────
elif page == "✅ Matches":
    st.title("Match History")
    matches = query("""
        SELECT m.id, req.id as req_id, t.name as tutor, t.phone as tutor_wa,
               req.name as parent, req.phone as parent_wa,
               req.subject, t.rate, m.fee_status,
               m.created_at::date as matched_on
        FROM matches m
        JOIN tutors t ON t.user_id = m.tutor_id
        JOIN requests req ON req.id = m.request_id
        ORDER BY m.created_at DESC
    """)
    if matches:
        df = pd.DataFrame(matches)
        df["rate"] = df["rate"].apply(lambda x: "$" + str(x) + "/hr")
        st.dataframe(df, use_container_width=True, hide_index=True)
        st.caption(str(len(matches)) + " total matches")
    else:
        st.info("No matches yet.")

# ── REVENUE ────────────────────────────────────────────────────────────────────
elif page == "💰 Revenue":
    st.title("Revenue Tracker")

    PLACEMENT_FEE = st.sidebar.number_input("Placement fee ($)", value=40, min_value=0)

    matches = query("""
        SELECT m.id, t.name as tutor, m.fee_status, m.created_at::date as date
        FROM matches m JOIN tutors t ON t.user_id = m.tutor_id
        ORDER BY m.created_at DESC
    """)

    total_matches = len(matches)
    paid          = sum(1 for m in matches if m["fee_status"] == "paid")
    pending       = total_matches - paid

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Matches",    total_matches)
    c2.metric("Fees Collected",   "$" + str(paid * PLACEMENT_FEE))
    c3.metric("Fees Pending",     "$" + str(pending * PLACEMENT_FEE))
    c4.metric("Projected Total",  "$" + str(total_matches * PLACEMENT_FEE))

    st.divider()
    if matches:
        df = pd.DataFrame(matches)
        df["fee"] = "$" + str(PLACEMENT_FEE)
        st.dataframe(df, use_container_width=True, hide_index=True)

        for m in [x for x in matches if x["fee_status"] == "pending"]:
            if st.button("Mark paid — Match #" + str(m["id"]), key="pay_" + str(m["id"])):
                execute("UPDATE matches SET fee_status='paid' WHERE id=%s", (m["id"],))
                st.rerun()

# ── RATINGS ────────────────────────────────────────────────────────────────────
elif page == "⭐ Ratings":
    st.title("Tutor Ratings Leaderboard")
    tutors = query("""
        SELECT name, subjects, rating_avg, rating_count, areas
        FROM tutors WHERE rating_count > 0
        ORDER BY rating_avg DESC, rating_count DESC
    """)
    if tutors:
        df = pd.DataFrame(tutors)
        df["rating_avg"] = df["rating_avg"].apply(lambda x: "⭐ " + str(x))
        st.dataframe(df, use_container_width=True, hide_index=True)
    else:
        st.info("No ratings collected yet.")

# ── BLOCKED ────────────────────────────────────────────────────────────────────
elif page == "🚫 Blocked":
    st.title("Blocked Users")
    blocked = query("SELECT user_id, blocked_at FROM blocked ORDER BY blocked_at DESC")
    if blocked:
        df = pd.DataFrame(blocked)
        st.dataframe(df, use_container_width=True, hide_index=True)
        uid = st.number_input("User ID to unblock", min_value=0, step=1)
        if st.button("Unblock"):
            execute("DELETE FROM blocked WHERE user_id=%s", (uid,))
            st.success("Unblocked.")
            st.rerun()
    else:
        st.info("No blocked users.")

# ── ERROR LOG ──────────────────────────────────────────────────────────────────
elif page == "⚠️ Error Log":
    st.title("Error Log")
    errors = query("SELECT * FROM error_log ORDER BY created_at DESC LIMIT 100")
    if errors:
        st.dataframe(pd.DataFrame(errors), use_container_width=True, hide_index=True)
    else:
        st.success("No errors logged.")
    if st.button("Clear error log"):
        execute("DELETE FROM error_log")
        st.rerun()

"""
CognifySG — Database Layer
PostgreSQL with connection pooling
"""

import os
import psycopg2
import psycopg2.pool
import psycopg2.extras
import logging

logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL", "")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set! Check Railway variables.")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
_pool = None

def get_pool():
    global _pool
    if _pool is None:
        _pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=2,
            maxconn=20,
            dsn=DATABASE_URL,
            cursor_factory=psycopg2.extras.RealDictCursor
        )
    return _pool

def db():
    return get_pool().getconn()

def release(conn):
    get_pool().putconn(conn)

def execute(sql, params=(), fetch="none"):
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            conn.commit()
            if fetch == "one":
                return cur.fetchone()
            elif fetch == "all":
                return cur.fetchall()
            elif fetch == "id":
                return cur.fetchone()[0]
    except Exception as e:
        conn.rollback()
        logger.error("DB error: %s | SQL: %s | Params: %s", e, sql, params)
        raise
    finally:
        release(conn)

def init_db():
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_roles (
                    user_id    BIGINT PRIMARY KEY,
                    role       TEXT NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );
                CREATE TABLE IF NOT EXISTS admins (
                    user_id    BIGINT PRIMARY KEY,
                    username   TEXT DEFAULT '',
                    name       TEXT DEFAULT 'Admin',
                    added_by   BIGINT,
                    added_at   TIMESTAMPTZ DEFAULT NOW()
                );
                CREATE TABLE IF NOT EXISTS blocked (
                    user_id    BIGINT PRIMARY KEY,
                    blocked_at TIMESTAMPTZ DEFAULT NOW()
                );
                CREATE TABLE IF NOT EXISTS captcha_attempts (
                    user_id  BIGINT PRIMARY KEY,
                    attempts INTEGER DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS terms_accepted (
                    user_id     BIGINT PRIMARY KEY,
                    accepted_at TIMESTAMPTZ DEFAULT NOW()
                );
                CREATE TABLE IF NOT EXISTS tutors (
                    user_id     BIGINT PRIMARY KEY,
                    username    TEXT DEFAULT '',
                    name        TEXT,
                    phone       TEXT,
                    subjects    TEXT,
                    levels      TEXT,
                    areas       TEXT,
                    rate        INTEGER,
                    available   INTEGER DEFAULT 1,
                    approved    INTEGER DEFAULT 0,
                    actioned_by BIGINT DEFAULT NULL,
                    rating_avg  NUMERIC(3,2) DEFAULT 0,
                    rating_count INTEGER DEFAULT 0,
                    created_at  TIMESTAMPTZ DEFAULT NOW()
                );
                CREATE TABLE IF NOT EXISTS requests (
                    id          SERIAL PRIMARY KEY,
                    parent_id   BIGINT,
                    username    TEXT DEFAULT '',
                    name        TEXT,
                    phone       TEXT,
                    subject     TEXT,
                    level       TEXT,
                    areas       TEXT,
                    budget      INTEGER,
                    status      TEXT DEFAULT 'open',
                    approved    INTEGER DEFAULT 0,
                    actioned_by BIGINT DEFAULT NULL,
                    matched_tutor_id BIGINT DEFAULT NULL,
                    created_at  TIMESTAMPTZ DEFAULT NOW()
                );
                CREATE TABLE IF NOT EXISTS applications (
                    id          SERIAL PRIMARY KEY,
                    tutor_id    BIGINT,
                    request_id  INTEGER,
                    match_score INTEGER DEFAULT 0,
                    created_at  TIMESTAMPTZ DEFAULT NOW(),
                    UNIQUE(tutor_id, request_id)
                );
                CREATE TABLE IF NOT EXISTS matches (
                    id          SERIAL PRIMARY KEY,
                    request_id  INTEGER,
                    tutor_id    BIGINT,
                    parent_id   BIGINT,
                    confirmed_by BIGINT,
                    fee_status  TEXT DEFAULT 'pending',
                    tutor_rating INTEGER DEFAULT NULL,
                    parent_rating INTEGER DEFAULT NULL,
                    created_at  TIMESTAMPTZ DEFAULT NOW()
                );
                CREATE TABLE IF NOT EXISTS error_log (
                    id          SERIAL PRIMARY KEY,
                    user_id     BIGINT,
                    handler     TEXT,
                    error       TEXT,
                    created_at  TIMESTAMPTZ DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_tutors_approved  ON tutors(approved, available);
                CREATE INDEX IF NOT EXISTS idx_requests_status  ON requests(status, approved);
                CREATE INDEX IF NOT EXISTS idx_requests_parent  ON requests(parent_id);
                CREATE INDEX IF NOT EXISTS idx_apps_request     ON applications(request_id);
                CREATE INDEX IF NOT EXISTS idx_apps_tutor       ON applications(tutor_id);
                CREATE INDEX IF NOT EXISTS idx_matches_request  ON matches(request_id);
            """)
            conn.commit()
            logger.info("Database initialised successfully.")
    except Exception as e:
        conn.rollback()
        logger.error("DB init error: %s", e)
        raise
    finally:
        release(conn)

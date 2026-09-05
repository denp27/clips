"""
Простой слой доступа к SQLite без внешних ORM-зависимостей.
Все функции синхронные (sqlite3 из стандартной библиотеки), но т.к.
FastAPI обработчики async, вызовы обёрнуты через run_in_threadpool
там, где это нужно (см. app.py).
"""
import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).parent / "daniclips.db"


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_id INTEGER UNIQUE NOT NULL,
            username TEXT,
            first_name TEXT,
            balance REAL NOT NULL DEFAULT 0,
            referrer_id INTEGER,
            created_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS offers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL,      -- streamers | youtubers | brands
            title TEXT NOT NULL,
            channel TEXT NOT NULL,       -- канал/аккаунт офера (@username или ссылка)
            price REAL NOT NULL,         -- цена за 1000 просмотров, руб
            min_views INTEGER NOT NULL DEFAULT 0,  -- от скольки просмотров идёт выплата
            image_url TEXT,              -- обложка оффера, 1920x1080
            budget_total REAL NOT NULL DEFAULT 0,  -- общий бюджет оффера (для полоски прогресса)
            details TEXT,                -- подробное описание задания ("Детали задания")
            description TEXT,
            hashtag_code TEXT,           -- код, который надо вставлять в хэштеги TikTok
            active INTEGER NOT NULL DEFAULT 1,
            created_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id),
            offer_id INTEGER NOT NULL REFERENCES offers(id),
            video_url TEXT NOT NULL,
            tiktok_account TEXT,
            views INTEGER NOT NULL DEFAULT 0,
            earned REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'pending', -- pending | accepted | rejected
            created_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS withdrawals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id),
            amount REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', -- pending | approved | rejected
            created_at INTEGER NOT NULL,
            processed_at INTEGER
        );
        """
    )
    conn.commit()

    # Мягкая миграция: если офферы уже были созданы старой версией схемы —
    # добираем недостающие колонки, не трогая существующие данные.
    existing_cols = {row["name"] for row in cur.execute("PRAGMA table_info(offers)").fetchall()}
    migrations = {
        "min_views": "ALTER TABLE offers ADD COLUMN min_views INTEGER NOT NULL DEFAULT 0",
        "image_url": "ALTER TABLE offers ADD COLUMN image_url TEXT",
        "budget_total": "ALTER TABLE offers ADD COLUMN budget_total REAL NOT NULL DEFAULT 0",
        "details": "ALTER TABLE offers ADD COLUMN details TEXT",
    }
    for col, stmt in migrations.items():
        if col not in existing_cols:
            cur.execute(stmt)
    conn.commit()
    conn.close()


OFFER_SELECT = """
    SELECT o.*,
           COALESCE((SELECT SUM(s.earned) FROM submissions s
                     WHERE s.offer_id = o.id AND s.status = 'accepted'), 0) AS budget_paid
    FROM offers o
"""


def get_or_create_user(tg_id: int, username: str | None, first_name: str | None) -> sqlite3.Row:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,))
    row = cur.fetchone()
    if row is None:
        cur.execute(
            "INSERT INTO users (tg_id, username, first_name, balance, created_at) VALUES (?, ?, ?, 0, ?)",
            (tg_id, username, first_name, int(time.time())),
        )
        conn.commit()
        cur.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,))
        row = cur.fetchone()
    else:
        cur.execute(
            "UPDATE users SET username = ?, first_name = ? WHERE tg_id = ?",
            (username, first_name, tg_id),
        )
        conn.commit()
    conn.close()
    return row


def get_user_by_tg_id(tg_id: int):
    conn = get_conn()
    row = conn.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,)).fetchone()
    conn.close()
    return row


def list_offers(category: str | None = None, active_only: bool = True):
    conn = get_conn()
    q = OFFER_SELECT
    conds, params = [], []
    if active_only:
        conds.append("o.active = 1")
    if category:
        conds.append("o.category = ?")
        params.append(category)
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY o.created_at DESC"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return rows


def get_offer(offer_id: int):
    conn = get_conn()
    row = conn.execute(OFFER_SELECT + " WHERE o.id = ?", (offer_id,)).fetchone()
    conn.close()
    return row


def create_offer(
    category,
    title,
    channel,
    price,
    description,
    hashtag_code,
    min_views=0,
    image_url=None,
    budget_total=0,
    details=None,
):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO offers
           (category, title, channel, price, description, hashtag_code,
            min_views, image_url, budget_total, details, active, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
        (
            category, title, channel, price, description, hashtag_code,
            min_views, image_url, budget_total, details, int(time.time()),
        ),
    )
    conn.commit()
    offer_id = cur.lastrowid
    conn.close()
    return offer_id


def toggle_offer(offer_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE offers SET active = 1 - active WHERE id = ?", (offer_id,))
    conn.commit()
    conn.close()


def delete_offer(offer_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM offers WHERE id = ?", (offer_id,))
    conn.commit()
    conn.close()


def create_submission(user_id, offer_id, video_url, tiktok_account):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO submissions (user_id, offer_id, video_url, tiktok_account, status, created_at)
           VALUES (?, ?, ?, ?, 'pending', ?)""",
        (user_id, offer_id, video_url, tiktok_account, int(time.time())),
    )
    conn.commit()
    sub_id = cur.lastrowid
    conn.close()
    return sub_id


def list_submissions_for_user(user_id: int):
    conn = get_conn()
    rows = conn.execute(
        """SELECT s.*, o.title AS offer_title, o.channel AS offer_channel
           FROM submissions s JOIN offers o ON o.id = s.offer_id
           WHERE s.user_id = ? ORDER BY s.created_at DESC""",
        (user_id,),
    ).fetchall()
    conn.close()
    return rows


def list_all_submissions():
    conn = get_conn()
    rows = conn.execute(
        """SELECT s.*, o.title AS offer_title, u.tg_id AS user_tg_id, u.username AS username
           FROM submissions s
           JOIN offers o ON o.id = s.offer_id
           JOIN users u ON u.id = s.user_id
           ORDER BY s.created_at DESC"""
    ).fetchall()
    conn.close()
    return rows


def update_submission_stats(sub_id: int, views: int, status: str, earned: float):
    conn = get_conn()
    conn.execute(
        "UPDATE submissions SET views = ?, status = ?, earned = ? WHERE id = ?",
        (views, status, earned, sub_id),
    )
    conn.commit()
    conn.close()


def create_withdrawal(user_id: int, amount: float):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO withdrawals (user_id, amount, status, created_at) VALUES (?, ?, 'pending', ?)",
        (user_id, amount, int(time.time())),
    )
    conn.commit()
    wd_id = cur.lastrowid
    conn.close()
    return wd_id


def list_withdrawals(status: str | None = None):
    conn = get_conn()
    q = """SELECT w.*, u.tg_id AS user_tg_id, u.username AS username
           FROM withdrawals w JOIN users u ON u.id = w.user_id"""
    params = []
    if status:
        q += " WHERE w.status = ?"
        params.append(status)
    q += " ORDER BY w.created_at DESC"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return rows


def get_withdrawal(wd_id: int):
    conn = get_conn()
    row = conn.execute(
        """SELECT w.*, u.tg_id AS user_tg_id, u.username AS username, u.id AS uid
           FROM withdrawals w JOIN users u ON u.id = w.user_id WHERE w.id = ?""",
        (wd_id,),
    ).fetchone()
    conn.close()
    return row


def set_withdrawal_status(wd_id: int, status: str):
    conn = get_conn()
    conn.execute(
        "UPDATE withdrawals SET status = ?, processed_at = ? WHERE id = ?",
        (status, int(time.time()), wd_id),
    )
    conn.commit()
    conn.close()


def adjust_balance(user_id: int, delta: float):
    conn = get_conn()
    conn.execute("UPDATE users SET balance = balance + ? WHERE id = ?", (delta, user_id))
    conn.commit()
    conn.close()

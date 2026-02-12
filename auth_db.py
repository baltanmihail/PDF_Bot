"""
Пользователи и задания: SQLite + сессия по cookie.
Логин = часть email до @, пароль — свой.
"""

import os
import sqlite3
import re
from pathlib import Path
from typing import Optional, List, Tuple, Any
from contextlib import contextmanager

from passlib.hash import bcrypt
from itsdangerous import URLSafeTimedSerializer, BadSignature

# SECRET_KEY из env для подписи сессии
SECRET_KEY = os.environ.get("SECRET_KEY", "change-me-in-production")
DB_PATH = Path(os.environ.get("DB_PATH", "data/auth.db"))
DATA_DIR = Path(os.environ.get("DATA_DIR", "data/outputs"))  # постоянное хранилище PDF

serializer = URLSafeTimedSerializer(SECRET_KEY, salt="session")


def get_password_hash(password: str) -> str:
    return bcrypt.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.verify(plain, hashed)


def create_session(user_id: int) -> str:
    return serializer.dumps(user_id)


def read_session(token: str) -> Optional[int]:
    try:
        return serializer.loads(token, max_age=60 * 60 * 24 * 7)  # 7 дней
    except BadSignature:
        return None


def _norm_username(email_part: str) -> str:
    """Логин: только буквы, цифры, точка (часть до @)."""
    s = (email_part or "").strip().lower()
    return re.sub(r"[^a-z0-9.]", "", s)[:64]


@contextmanager
def _db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with _db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                job_id TEXT UNIQUE NOT NULL,
                filename TEXT NOT NULL,
                file_path TEXT,
                total_pages INTEGER,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_user ON jobs(user_id)")


def create_user(username: str, password: str) -> Tuple[Optional[int], str]:
    """Возвращает (user_id, error). error пустой при успехе."""
    name = _norm_username(username)
    if not name:
        return None, "Логин не задан"
    if len(password) < 4:
        return None, "Пароль не менее 4 символов"
    with _db() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO users (username, password_hash) VALUES (?, ?)",
                (name, get_password_hash(password)),
            )
            return cur.lastrowid, ""
        except sqlite3.IntegrityError:
            return None, "Такой логин уже занят"


def get_user_by_username(username: str) -> Optional[dict]:
    name = _norm_username(username)
    with _db() as conn:
        row = conn.execute("SELECT id, username, password_hash FROM users WHERE username = ?", (name,)).fetchone()
        return dict(row) if row else None


def auth_user(username: str, password: str) -> Optional[int]:
    u = get_user_by_username(username)
    if not u or not verify_password(password, u["password_hash"]):
        return None
    return u["id"]


def save_job(user_id: int, job_id: str, filename: str, file_path: Optional[str], total_pages: Optional[int]):
    with _db() as conn:
        conn.execute(
            "INSERT INTO jobs (user_id, job_id, filename, file_path, total_pages) VALUES (?, ?, ?, ?, ?)",
            (user_id, job_id, filename, file_path, total_pages),
        )


def get_job(job_id: str) -> Optional[dict]:
    with _db() as conn:
        row = conn.execute("SELECT user_id, job_id, filename, file_path FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        return dict(row) if row else None


def get_user_jobs(user_id: int) -> List[dict]:
    with _db() as conn:
        rows = conn.execute(
            "SELECT job_id, filename, total_pages, created_at FROM jobs WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_user_storage_dir(user_id: int) -> Path:
    d = DATA_DIR / str(user_id)
    d.mkdir(parents=True, exist_ok=True)
    return d

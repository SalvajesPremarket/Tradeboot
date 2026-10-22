"""Account authentication and persistent editor permissions for Tradeboot."""
import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone

SESSION_DAYS = 7
PBKDF2_ITERATIONS = 600_000


def _connect():
    from strategy_store import _connect as connect
    return connect()


def _now():
    return datetime.now(timezone.utc)


def _password_hash(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return "pbkdf2_sha256$" + str(PBKDF2_ITERATIONS) + "$" + salt.hex() + "$" + digest.hex()


def _verify_password(password, encoded):
    try:
        algorithm, iterations, salt_hex, digest_hex = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations)
        ).hex()
        return hmac.compare_digest(actual, digest_hex)
    except (ValueError, TypeError, AttributeError):
        return False


def _ensure_schema(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tradeboot_users (
            username TEXT PRIMARY KEY,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('admin', 'user')),
            editor_enabled BOOLEAN NOT NULL DEFAULT FALSE,
            must_change_password BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tradeboot_sessions (
            token_hash TEXT PRIMARY KEY,
            username TEXT NOT NULL REFERENCES tradeboot_users(username) ON DELETE CASCADE,
            expires_at TIMESTAMPTZ NOT NULL,
            created_at TIMESTAMPTZ NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS tradeboot_sessions_expiry_idx ON tradeboot_sessions(expires_at)")


def ensure_bootstrap_admin():
    username = os.environ.get("TRADEBOOT_ADMIN_USERNAME", "").strip()
    password = os.environ.get("TRADEBOOT_ADMIN_PASSWORD", "")
    if not username or not password:
        return False
    if len(username) > 80 or len(password) < 12:
        raise ValueError("admin_bootstrap_configuration_invalid")
    now = _now()
    with _connect() as conn:
        _ensure_schema(conn)
        row = conn.execute("SELECT username FROM tradeboot_users WHERE username = %s", (username,)).fetchone()
        if row:
            return True
        conn.execute(
            """INSERT INTO tradeboot_users
               (username, password_hash, role, editor_enabled, must_change_password, created_at, updated_at)
               VALUES (%s, %s, 'admin', TRUE, FALSE, %s, %s)""",
            (username, _password_hash(password), now, now),
        )
    return True


def _username(value):
    if not isinstance(value, str):
        raise ValueError("username_required")
    value = value.strip()
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._@-"
    if not value or len(value) > 80 or any(ch not in allowed for ch in value):
        raise ValueError("username_invalid")
    return value


def _password(value):
    if not isinstance(value, str) or len(value) < 12 or len(value) > 1024:
        raise ValueError("password_must_be_at_least_12_characters")
    return value


def login(username, password):
    username = _username(username)
    if not isinstance(password, str) or not password:
        return None
    ensure_bootstrap_admin()
    now = _now()
    with _connect() as conn:
        _ensure_schema(conn)
        row = conn.execute(
            "SELECT password_hash FROM tradeboot_users WHERE username = %s", (username,)
        ).fetchone()
        if not row or not _verify_password(password, row[0]):
            return None
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        conn.execute("DELETE FROM tradeboot_sessions WHERE expires_at <= %s", (now,))
        conn.execute(
            "INSERT INTO tradeboot_sessions (token_hash, username, expires_at, created_at) VALUES (%s, %s, %s, %s)",
            (token_hash, username, now + timedelta(days=SESSION_DAYS), now),
        )
    return token


def current_user(token):
    if not token or not isinstance(token, str) or len(token) > 256:
        return None
    ensure_bootstrap_admin()
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    now = _now()
    with _connect() as conn:
        _ensure_schema(conn)
        row = conn.execute(
            """SELECT u.username, u.role, u.editor_enabled, u.must_change_password
               FROM tradeboot_sessions s JOIN tradeboot_users u ON u.username = s.username
               WHERE s.token_hash = %s AND s.expires_at > %s""",
            (token_hash, now),
        ).fetchone()
    if not row:
        return None
    return {"username": row[0], "role": row[1], "editor_enabled": bool(row[2]),
            "must_change_password": bool(row[3])}


def logout(token):
    if not token or not isinstance(token, str):
        return
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    with _connect() as conn:
        _ensure_schema(conn)
        conn.execute("DELETE FROM tradeboot_sessions WHERE token_hash = %s", (token_hash,))


def create_user(username, password, editor_enabled=False):
    username = _username(username)
    password = _password(password)
    if not isinstance(editor_enabled, bool):
        raise ValueError("editor_enabled_must_be_boolean")
    now = _now()
    with _connect() as conn:
        _ensure_schema(conn)
        conn.execute(
            """INSERT INTO tradeboot_users
               (username, password_hash, role, editor_enabled, must_change_password, created_at, updated_at)
               VALUES (%s, %s, 'user', %s, TRUE, %s, %s)""",
            (username, _password_hash(password), editor_enabled, now, now),
        )
    return {"username": username, "role": "user", "editor_enabled": editor_enabled,
            "must_change_password": True}


def list_users():
    with _connect() as conn:
        _ensure_schema(conn)
        rows = conn.execute(
            "SELECT username, role, editor_enabled, must_change_password, created_at FROM tradeboot_users ORDER BY created_at, username"
        ).fetchall()
    return [{"username": r[0], "role": r[1], "editor_enabled": bool(r[2]),
             "must_change_password": bool(r[3]), "created_at": r[4].isoformat()} for r in rows]


def set_editor_access(username, enabled):
    username = _username(username)
    if not isinstance(enabled, bool):
        raise ValueError("editor_enabled_must_be_boolean")
    with _connect() as conn:
        _ensure_schema(conn)
        result = conn.execute(
            "UPDATE tradeboot_users SET editor_enabled = %s, updated_at = %s WHERE username = %s AND role = 'user'",
            (enabled, _now(), username),
        )
        if result.rowcount != 1:
            raise ValueError("user_not_found")
    return {"username": username, "editor_enabled": enabled}


def change_password(username, old_password, new_password):
    username = _username(username)
    new_password = _password(new_password)
    with _connect() as conn:
        _ensure_schema(conn)
        row = conn.execute("SELECT password_hash FROM tradeboot_users WHERE username = %s", (username,)).fetchone()
        if not row or not isinstance(old_password, str) or not _verify_password(old_password, row[0]):
            return False
        conn.execute(
            "UPDATE tradeboot_users SET password_hash = %s, must_change_password = FALSE, updated_at = %s WHERE username = %s",
            (_password_hash(new_password), _now(), username),
        )
        conn.execute("DELETE FROM tradeboot_sessions WHERE username = %s", (username,))
    return True

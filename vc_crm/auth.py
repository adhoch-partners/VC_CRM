"""Email + password authentication with server-side sessions.

Passwords are hashed with scrypt (stdlib). The browser holds a random session
token in an HttpOnly cookie; the database stores only its SHA-256, so sessions
can be revoked (logout, deactivation, password change) and a database leak
cannot be replayed as a login.
"""

import base64
import hashlib
import hmac
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone

ROLES = ["admin", "editor", "viewer"]
ROLE_RANK = {"viewer": 1, "editor": 2, "admin": 3}
MIN_PASSWORD = 10
COOKIE = "vccrm_session"

_N, _R, _P = 2 ** 14, 8, 1


def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    b64 = lambda b: base64.b64encode(b).decode()
    return f"scrypt${_N}${_R}${_P}${b64(salt)}${b64(digest)}"


def verify_password(password, stored):
    try:
        algo, n, r, p, salt, digest = stored.split("$")
        if algo != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt),
                                n=int(n), r=int(r), p=int(p), dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


# Burn the same scrypt cost for unknown emails so response time does not
# reveal which addresses have accounts.
_DUMMY_HASH = hash_password(secrets.token_hex(8))


def check_password_policy(password):
    if not isinstance(password, str) or len(password) < MIN_PASSWORD:
        return f"Password must be at least {MIN_PASSWORD} characters."
    return None


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def utcnow():
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def create_session(conn, user_id, days):
    token = secrets.token_urlsafe(32)
    with conn.transaction():
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (iso(utcnow()),))
        conn.execute("INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
                     (token_hash(token), user_id, iso(utcnow() + timedelta(days=days))))
    return token


def session_user(conn, token):
    if not token:
        return None
    row = conn.execute(
        "SELECT u.id, u.email, u.name, u.role, u.active FROM sessions s "
        "JOIN users u ON u.id = s.user_id WHERE s.token_hash = ? AND s.expires_at > ?",
        (token_hash(token), iso(utcnow()))).fetchone()
    if not row or not row["active"]:
        return None
    return dict(row)


class LoginThrottle:
    """In-memory limit on failed logins per email and per client IP.

    Good for a single app instance (the deploy config runs one). With several
    instances each keeps its own counts, which loosens the limit proportionally.
    """

    def __init__(self, max_failures=10, window_seconds=900):
        self.max = max_failures
        self.window = window_seconds
        self.failures = {}
        self.lock = threading.Lock()

    def _recent(self, key, now):
        hits = [t for t in self.failures.get(key, []) if now - t < self.window]
        self.failures[key] = hits
        return hits

    def blocked(self, *keys):
        now = time.monotonic()
        with self.lock:
            return any(len(self._recent(k, now)) >= self.max for k in keys)

    def fail(self, *keys):
        now = time.monotonic()
        with self.lock:
            # Bound memory if someone sprays many distinct emails or IPs.
            if len(self.failures) > 10_000:
                for k in list(self.failures):
                    if not self._recent(k, now):
                        del self.failures[k]
            for k in keys:
                self._recent(k, now).append(now)

    def clear(self, key):
        with self.lock:
            self.failures.pop(key, None)

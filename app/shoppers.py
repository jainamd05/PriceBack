"""Shopper accounts: passwordless sign-in with a 6-digit code sent to the shopper's own email.

* Codes are random, expire after 10 minutes, allow 5 guesses, and are stored only as salted hashes.
* Requests are rate-limited per address and globally (the free email tier allows ~300 messages a day).
* A session is a random 32-byte token in an HttpOnly cookie; only its sha256 is stored.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
import time

CODE_TTL = 10 * 60
MAX_ATTEMPTS = 5
RESEND_GAP = 30
MAX_PER_HOUR = 5
MAX_PER_DAY_GLOBAL = 250
SESSION_TTL = 30 * 86400
COOKIE = "pb_session"

_EMAIL = re.compile(r"^[^@\s,;<>\"']{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,24}$")


class AuthError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def normalize(email: str) -> str:
    e = (email or "").strip().lower()
    if len(e) > 254 or not _EMAIL.match(e):
        raise AuthError("Enter a valid email address.", 422)
    return e


def _hash(salt: str, code: str) -> str:
    return hashlib.sha256(f"{salt}:{code}".encode()).hexdigest()


def start(conn: sqlite3.Connection, email: str, now: int | None = None) -> str:
    """Create a sign-in code for `email` and return it (the caller emails it)."""
    now = int(now if now is not None else time.time())
    email = normalize(email)
    last = conn.execute("SELECT created_at FROM login_codes WHERE email=? ORDER BY id DESC LIMIT 1", (email,)).fetchone()
    if last and now - last["created_at"] < RESEND_GAP:
        raise AuthError(f"A code was just sent. Wait {RESEND_GAP - (now - last['created_at'])} seconds before asking again.", 429)
    if conn.execute("SELECT COUNT(*) FROM login_codes WHERE email=? AND created_at>?", (email, now - 3600)).fetchone()[0] >= MAX_PER_HOUR:
        raise AuthError("Too many codes for this address. Try again in an hour.", 429)
    if conn.execute("SELECT COUNT(*) FROM login_codes WHERE created_at>?", (now - 86400,)).fetchone()[0] >= MAX_PER_DAY_GLOBAL:
        raise AuthError("The demo has hit today's email limit. Please try again tomorrow.", 429)
    code = f"{secrets.randbelow(10**6):06d}"
    salt = secrets.token_hex(8)
    conn.execute("UPDATE login_codes SET used=1 WHERE email=? AND used=0", (email,))  # only the newest code works
    conn.execute("INSERT INTO login_codes (email, code_hash, salt, created_at, expires_at) VALUES (?,?,?,?,?)",
                 (email, _hash(salt, code), salt, now, now + CODE_TTL))
    return code


def verify(conn: sqlite3.Connection, email: str, code: str, now: int | None = None) -> str:
    """Check the code; on success return a new session token (the cookie value)."""
    now = int(now if now is not None else time.time())
    email = normalize(email)
    code = re.sub(r"\D", "", code or "")
    row = conn.execute("SELECT * FROM login_codes WHERE email=? AND used=0 ORDER BY id DESC LIMIT 1", (email,)).fetchone()
    if not row or row["expires_at"] < now or row["attempts"] >= MAX_ATTEMPTS:
        raise AuthError("That code has expired. Ask for a new one.", 400)
    if len(code) != 6 or not hmac.compare_digest(_hash(row["salt"], code), row["code_hash"]):
        conn.execute("UPDATE login_codes SET attempts=attempts+1 WHERE id=?", (row["id"],))
        left = MAX_ATTEMPTS - row["attempts"] - 1
        raise AuthError("That code is not right." + (f" {left} tries left." if left > 0 else " Ask for a new one."), 400)
    conn.execute("UPDATE login_codes SET used=1 WHERE id=?", (row["id"],))
    token = secrets.token_urlsafe(32)
    conn.execute("INSERT INTO sessions (token_hash, email, created_at, expires_at) VALUES (?,?,?,?)",
                 (hashlib.sha256(token.encode()).hexdigest(), email, now, now + SESSION_TTL))
    return token


def session_email(conn: sqlite3.Connection, token: str | None, now: int | None = None) -> str | None:
    if not token:
        return None
    now = int(now if now is not None else time.time())
    row = conn.execute("SELECT email, expires_at FROM sessions WHERE token_hash=?",
                       (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
    return row["email"] if row and row["expires_at"] > now else None


def logout(conn: sqlite3.Connection, token: str | None) -> None:
    if token:
        conn.execute("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))

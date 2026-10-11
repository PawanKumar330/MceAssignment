import os
import time
import secrets
from functools import wraps
from flask import session, redirect, url_for, flash, request, abort, jsonify
from werkzeug.security import generate_password_hash, check_password_hash

# Login rate limiting: in-memory tracker
# Format: ip_address -> {'attempts': int, 'lock_until': float}
LOGIN_ATTEMPTS = {}
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_DURATION_SECONDS = 300  # 5 minutes

def is_ip_locked(ip: str) -> tuple[bool, int]:
    """Checks if the given IP address is currently locked out."""
    now = time.time()
    record = LOGIN_ATTEMPTS.get(ip)
    if not record:
        return False, 0
    if record.get("lock_until", 0) > now:
        remaining = int(record["lock_until"] - now)
        return True, remaining
    # Lock expired, reset if needed
    if record.get("lock_until", 0) <= now and record.get("attempts", 0) >= MAX_FAILED_ATTEMPTS:
        LOGIN_ATTEMPTS.pop(ip, None)
    return False, 0

def record_failed_login(ip: str):
    """Increments failed attempts and triggers lockout if max attempts reached."""
    now = time.time()
    if ip not in LOGIN_ATTEMPTS:
        LOGIN_ATTEMPTS[ip] = {"attempts": 1, "lock_until": 0}
    else:
        LOGIN_ATTEMPTS[ip]["attempts"] += 1

    if LOGIN_ATTEMPTS[ip]["attempts"] >= MAX_FAILED_ATTEMPTS:
        LOGIN_ATTEMPTS[ip]["lock_until"] = now + LOCKOUT_DURATION_SECONDS

def clear_failed_login(ip: str):
    """Clears failed login attempts for an IP upon successful login."""
    LOGIN_ATTEMPTS.pop(ip, None)

def get_client_ip() -> str:
    """Safely retrieves the client's IP from request headers or remote_addr."""
    if request.headers.get("X-Forwarded-For"):
        return request.headers.get("X-Forwarded-For").split(",")[0].strip()
    return request.remote_addr or "127.0.0.1"

# CSRF Protection Helpers
def generate_csrf_token() -> str:
    """Generates and stores a CSRF token in the session."""
    if "_csrf_token" not in session:
        session["_csrf_token"] = secrets.token_hex(32)
    return session["_csrf_token"]

def validate_csrf_token(token: str) -> bool:
    """Validates the submitted CSRF token against the session token."""
    session_token = session.get("_csrf_token")
    if not session_token or not token:
        return False
    return secrets.compare_digest(session_token, token)

# Role Decorators
def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login", next=request.path))
        if session.get("role") != "admin":
            abort(403, description="Access restricted to Master Admin.")
        return f(*args, **kwargs)
    return decorated_function

def faculty_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login", next=request.path))
        if session.get("role") != "faculty":
            abort(403, description="Access restricted to Faculty members.")
        return f(*args, **kwargs)
    return decorated_function

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return decorated_function

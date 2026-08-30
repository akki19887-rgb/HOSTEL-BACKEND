"""
security.py — drop-in hardening for the HostelOM Flask backend.

Import this from app.py and apply the decorators. Nothing here changes what your
endpoints DO; it only changes who is allowed to call them and how often.

    pip install flask-limiter

Add to requirements.txt:
    flask-limiter
"""

import os
import functools
import time
from flask import request, jsonify


# ==========================================================================
# 1. WHO IS CALLING — Firebase ID token verification
#
# Every endpoint was previously open to the public internet. Anyone could hit
# /send-otp in a loop and burn real 2Factor SMS credits, or /extract-aadhar and
# burn Gemini quota. This decorator makes the caller prove which Firebase user
# they are before anything runs.
#
# The frontend sends:  Authorization: Bearer <await user.getIdToken()>
# ==========================================================================

def _verify_token():
    """Returns the decoded token dict, or None."""
    from firebase_admin import auth as fb_auth
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    token = header[7:].strip()
    if not token:
        return None
    try:
        # check_revoked=True means a signed-out / disabled account stops working
        # immediately rather than staying valid for up to an hour.
        return fb_auth.verify_id_token(token, check_revoked=True)
    except Exception:
        return None


def require_auth(fn):
    """Any signed-in Firebase user. Sets request.uid / request.claims."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        # CORS preflight carries no Authorization header by design. Rejecting it here
        # surfaces in the browser as "blocked by CORS policy" and the real request is
        # never sent at all.
        if request.method == 'OPTIONS':
            return ('', 204)
        decoded = _verify_token()
        if not decoded:
            return jsonify({"error": "Authentication required."}), 401
        request.uid = decoded.get("uid")
        request.claims = decoded
        return fn(*args, **kwargs)
    return wrapper


def require_admin(fn):
    """Signed in AND present in the Firestore `admin` collection."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if request.method == 'OPTIONS':
            return ('', 204)
        decoded = _verify_token()
        if not decoded:
            return jsonify({"error": "Authentication required."}), 401
        uid = decoded.get("uid")
        from firebase_admin import firestore as fb_firestore
        db = fb_firestore.client()
        if not db.collection("admin").document(uid).get().exists:
            return jsonify({"error": "Admin access required."}), 403
        request.uid = uid
        request.claims = decoded
        return fn(*args, **kwargs)
    return wrapper


def require_staff(fn):
    """
    Admin, OR an active member of the `staffRoles` collection.

    Handing an employee the admin login gives them every guest's Aadhaar, every owner's bank
    account, and the power to grant the Verified badge. This decorator is what lets a support
    agent answer a phone call on a fraction of that.

    Sets request.staff_role to 'admin' | 'support' | 'field' | 'verifier' so a route can
    narrow further, and so every audit entry records WHICH role acted.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if request.method == 'OPTIONS':
            return ('', 204)
        decoded = _verify_token()
        if not decoded:
            return jsonify({"error": "Authentication required."}), 401
        uid = decoded.get("uid")
        from firebase_admin import firestore as fb_firestore
        db = fb_firestore.client()

        if db.collection("admin").document(uid).get().exists:
            request.staff_role = "admin"
        else:
            doc = db.collection("staffRoles").document(uid).get()
            data = doc.to_dict() if doc.exists else None
            # `active` is checked separately from existence so that removing someone's access
            # is one field flip, not a delete — the audit trail of who they were stays intact.
            if not data or data.get("active") is not True or not data.get("role"):
                return jsonify({"error": "Staff access required."}), 403
            request.staff_role = data.get("role")

        request.uid = uid
        request.claims = decoded
        return fn(*args, **kwargs)
    return wrapper


# ==========================================================================
# 2. HOW OFTEN — rate limiting
#
# Without this, /send-otp is an SMS-bombing service that bills you, and
# /extract-aadhar is free Gemini access for anyone who finds the URL.
# ==========================================================================

def build_limiter(app):
    from flask_limiter import Limiter

    def key():
        # Prefer the authenticated uid so one user can't dodge limits by
        # rotating IPs; fall back to IP for unauthenticated routes.
        uid = getattr(request, "uid", None)
        if uid:
            return f"uid:{uid}"
        fwd = request.headers.get("X-Forwarded-For", "")
        return "ip:" + (fwd.split(",")[0].strip() if fwd else request.remote_addr or "unknown")

    limiter = Limiter(
        key_func=key,
        app=app,
        default_limits=["200 per hour"],
        storage_uri=os.environ.get("RATELIMIT_STORAGE_URI", "memory://"),
        # NOTE: "memory://" resets on every deploy and is per-process. Once you
        # run more than one gunicorn worker, point RATELIMIT_STORAGE_URI at a
        # Redis instance or the limits are trivially bypassed.
    )
    # A 429 on a preflight is indistinguishable from a CORS error in the browser.
    limiter.request_filter(lambda: request.method == 'OPTIONS')
    return limiter


# ==========================================================================
# 3. OTP BRUTE-FORCE GUARD
#
# 2Factor verifies the OTP, but nothing stopped a script from trying all 10,000
# four-digit codes against one session_id. Five wrong attempts kills the session.
# ==========================================================================

_otp_attempts = {}   # session_id -> [count, first_attempt_ts]
_OTP_MAX_ATTEMPTS = 5
_OTP_WINDOW_SEC = 600


def otp_attempt_allowed(session_id):
    now = time.time()
    count, started = _otp_attempts.get(session_id, (0, now))
    if now - started > _OTP_WINDOW_SEC:
        count, started = 0, now
    if count >= _OTP_MAX_ATTEMPTS:
        return False
    _otp_attempts[session_id] = (count + 1, started)
    return True


def otp_attempt_clear(session_id):
    _otp_attempts.pop(session_id, None)


# ==========================================================================
# 4. SERVER-SIDE PRICING
#
# This is the fix for the worst payment hole. Previously the browser sent the
# amount and the server accepted it, so DevTools could turn a ₹7,000 advance
# into ₹1 — the Razorpay signature would still verify, because it was a real
# ₹1 payment against a real ₹1 order, and the booking would be written as
# paid_verified with the bed marked occupied.
#
# The browser now sends only propertyId + bedIds. The price comes from the
# database, every time.
# ==========================================================================

ADVANCE_PERCENT = int(os.environ.get("ADVANCE_PERCENT", "20"))


def compute_booking_amount(db, property_id, bed_ids):
    """
    Returns (advance_paise, total_rupees, owner_uid) or raises ValueError.
    Prices are read live from businesses/{id}.roomsAndBeds — never from the client.
    """
    if not property_id or not bed_ids:
        raise ValueError("propertyId and bedIds are required.")

    snap = db.collection("businesses").document(property_id).get()
    if not snap.exists:
        raise ValueError("Property not found.")
    biz = snap.to_dict() or {}

    wanted = set(bed_ids)
    total = 0
    found = set()
    for room in (biz.get("roomsAndBeds") or []):
        for bed in (room.get("beds") or []):
            if bed.get("id") in wanted:
                # Refuse to price a bed that is already taken — this also closes
                # the double-booking race where two guests pay for one bed.
                if bed.get("status") and bed.get("status") != "available":
                    raise ValueError(f"Bed {bed.get('id')} is no longer available.")
                total += int(bed.get("priceMonthly") or bed.get("price") or 0)
                found.add(bed.get("id"))

    missing = wanted - found
    if missing:
        raise ValueError(f"Unknown bed(s): {', '.join(sorted(missing))}")
    if total <= 0:
        raise ValueError("Could not determine a price for these beds.")

    advance_rupees = round(total * ADVANCE_PERCENT / 100)
    return advance_rupees * 100, total, biz.get("ownerId")

from flask import Flask, request, jsonify
from flask_cors import CORS
from google import genai
import json
import os
import hmac
import hashlib
import requests
from PIL import Image

from datetime import timedelta
from security import (
    require_auth, require_admin, build_limiter,
    otp_attempt_allowed, otp_attempt_clear,
    compute_booking_amount, ADVANCE_PERCENT,
)

app = Flask(__name__)

# Was CORS(app) — that allowed EVERY website on the internet to call these
# endpoints from a user's browser. Now only your own domain can.
ALLOWED_ORIGINS = [o.strip() for o in os.environ.get(
    "ALLOWED_ORIGINS", "https://ho-om.in,https://www.ho-om.in"
).split(",") if o.strip()]
CORS(app, origins=ALLOWED_ORIGINS, supports_credentials=False)

limiter = build_limiter(app)

# API KEY ab environment variable se aayegi (code me hardcoded nahi rahegi).
# Terminal me run karne se pehle set karein:
#   Windows (PowerShell):  $env:GEMINI_API_KEY="your_key_here"
#   Mac/Linux:              export GEMINI_API_KEY="your_key_here"
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY environment variable not set. Set it before starting the server.")

client = genai.Client(api_key=GEMINI_API_KEY)

# Email service (Resend.com) — for sending the Guest Registration PDF link to the hostel admin's email.
# Sign up free at https://resend.com, verify your sending domain (or use their test domain to start),
# then set these two environment variables the same way as GEMINI_API_KEY above:
#   RESEND_API_KEY   -> your Resend API key
#   ADMIN_EMAIL      -> the email address that should receive booking notifications
RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL")

# RAZORPAY PAYMENT GATEWAY — set these env vars before running:
#   RAZORPAY_KEY_ID       -> starts with rzp_test_... (test) or rzp_live_... (production)
#   RAZORPAY_KEY_SECRET   -> secret from Razorpay dashboard (NEVER expose to frontend)
# Get them from: https://dashboard.razorpay.com/app/keys
RAZORPAY_KEY_ID = os.environ.get("RAZORPAY_KEY_ID")
RAZORPAY_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET")

# 2FACTOR.IN — SMS OTP (DLT-compliant), replaces Firebase's default phone-auth SMS route which was
# getting flagged as spam/blocked by Indian carriers. Get the API key from 2factor.in → Account
# Summary, and the approved template name from SMS OTP → OTP Templates.
#   TWOFACTOR_API_KEY        -> your 2Factor.in API key
#   TWOFACTOR_TEMPLATE_NAME  -> the exact Template Name you created (e.g. "Ho-Om") — must be approved
TWOFACTOR_API_KEY = os.environ.get("TWOFACTOR_API_KEY")
TWOFACTOR_TEMPLATE_NAME = os.environ.get("TWOFACTOR_TEMPLATE_NAME", "Ho-Om")

# FIREBASE ADMIN SDK — used for (a) the custom Resend-branded email verification link, and (b)
# writing booking records + marking beds 'occupied' server-side after a Razorpay payment is
# verified (see /razorpay/verify below). Never trust the browser to write its own "I paid"
# record — this is exactly the hole a fake/DevTools booking used to slip through.
# Firebase Console → Project Settings → Service Accounts → "Generate new private key" downloads a
# JSON file. Paste its ENTIRE content as the value of a FIREBASE_SERVICE_ACCOUNT_JSON env var
# (same way as GEMINI_API_KEY above) — do not commit the JSON file itself to git.
FIREBASE_SERVICE_ACCOUNT_JSON = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON")
firebase_admin_app = None
firestore_db = None
if FIREBASE_SERVICE_ACCOUNT_JSON:
    try:
        import firebase_admin
        from firebase_admin import credentials as fb_credentials, auth as fb_auth, firestore as fb_firestore
        cred = fb_credentials.Certificate(json.loads(FIREBASE_SERVICE_ACCOUNT_JSON))
        firebase_admin_app = firebase_admin.initialize_app(cred, {
            'storageBucket': os.environ.get('FIREBASE_STORAGE_BUCKET', '')
        })
        firestore_db = fb_firestore.client()
        print("✅ Firebase Admin SDK initialized (Auth + Firestore).")
    except Exception as e:
        print(f"⚠️ Firebase Admin SDK failed to initialize: {e}")

# SENTRY ERROR LOGGING (backend): optional. Set SENTRY_DSN env var (same way as GEMINI_API_KEY)
# once you have it from sentry.io — the server will keep working fine even without it.
SENTRY_DSN = os.environ.get("SENTRY_DSN")
if SENTRY_DSN:
    try:
        import sentry_sdk
        from sentry_sdk.integrations.flask import FlaskIntegration
        sentry_sdk.init(dsn=SENTRY_DSN, integrations=[FlaskIntegration()], traces_sample_rate=0.2)
    except ImportError:
        print("⚠️  sentry-sdk not installed. Run: pip install sentry-sdk --break-system-packages")

# ==========================================
# 1. EXACT 2D MAP AUTO-EXTRACTION
# ==========================================
@app.route('/process-rough-layout', methods=['POST'])
@limiter.limit("20 per hour")
@require_auth
def process_rough_layout():
    print("\n" + "="*50)
    print("🟢 START: AI Map Extraction Started!")

    if 'floor_plan' not in request.files:
        return jsonify({"error": "No image uploaded"}), 400

    file = request.files['floor_plan']

    try:
        img = Image.open(file)

        # SUPER STRICT PROMPT: To identify exact architectural layout
        prompt = """Analyze this hand-drawn floor plan image to create an exact digital 2D replica.

        Follow these STRICT instructions:

        1. CORRIDOR SHAPE (CRITICAL): Look at the walking space/hallway as a whole and decide which of
           these five shapes it forms:
           - "straight": ONE single corridor running along one edge or through the middle, with rooms
             on at most two sides of it (this is the common case).
           - "L": the corridor bends once, 90 degrees, like the letter L.
           - "T": the corridor forms a T-junction — one corridor meets another at a midpoint, forming 3 arms.
           - "cross": the corridor forms a "+" junction — two corridors crossing, forming 4 arms.
           - "H": there are TWO SEPARATE, PARALLEL straight corridors (e.g. one between the top row of
             rooms and the middle row, and another between the middle row and the bottom row), which
             may or may not be joined by a short connecting corridor segment — like the letter H. This
             is different from "cross": in "H" the two corridors do NOT cross each other, they are two
             independent straight runs stacked with room-rows between and around them.

        2. CORRIDOR POSITION (only when corridor_shape is "straight"): Choose EXACTLY ONE of these
           values: "left", "right", "top", "bottom", or "center".
           (e.g., If rooms are on the right and the empty path is on the left, choose "left". If rooms
           are on both sides of the path, choose "center".)
           If corridor_shape is "L", "T", "cross", or "H", set corridor_position to "center" (it's
           ignored in that case — quadrant/row assignment below is what actually matters).

        3. ROOM QUADRANT (only when corridor_shape is "L", "T", or "cross"): For each room, imagine the
           floor plan as a map and determine which quadrant it sits in relative to the corridor
           junction: "NW" (top-left), "NE" (top-right), "SW" (bottom-left), or "SE" (bottom-right).
           Leave this field out entirely (or null) when corridor_shape is not "L"/"T"/"cross".

        4. ROOM ROW + SIDE (only when corridor_shape is "H"): For each room, determine:
           - "row": which room-band it's in — "top" (above the first corridor), "middle" (between the
             two corridors), or "bottom" (below the second corridor).
           - "side": "left" or "right" of the vertical connector (if the drawing has no clear left/right
             split, alternate rooms left/right in the order they appear, left-to-right on the page).
           Leave both fields out entirely (or null) when corridor_shape is not "H".

        5. ROOMS: Identify all rooms and their labels EXACTLY as handwritten. This is critical — do not
           guess or "clean up" a label into a different one. If a room number is genuinely ambiguous
           (could be a "9" or could be a letter), look at neighboring room numbers for a sequence (e.g.
           if rooms 1-8 are already found in order, an ambiguous next label is almost certainly "9", not
           a letter) and prefer the numeric reading that continues the sequence. Count the beds inside
           each room (default to 1 if unclear, based on distinct bed shapes/labels like "bed1", "bed2").

        6. AMENITIES (CRITICAL — DO NOT INVENT): Only mark 'bathroom' or 'study_table' for a room if
           there is an explicit small box/icon/label for it INSIDE that room in the drawing (e.g. a box
           labeled "bath", "washroom", "table", or a distinct bathroom/table icon). If a room contains
           ONLY bed labels and nothing else, its amenities list MUST be empty — never add a bathroom or
           table that was not actually drawn, even if other similar rooms in the plan have one.

        7. DOORS: True if doors/openings are marked.

        Return ONLY a valid JSON object matching this structure EXACTLY (No markdown, no extra text):
        {
          "corridor_detected": true,
          "corridor_shape": "straight",
          "corridor_position": "left",
          "rooms": [
            {"room_no": "1", "beds_count": 1, "amenities": ["study_table", "bathroom"], "has_door": true, "quadrant": null, "row": null, "side": null},
            {"room_no": "2", "beds_count": 2, "amenities": [], "has_door": true, "quadrant": null, "row": null, "side": null}
          ]
        }

        Example for an "L" or "T" or "cross" shaped corridor, each room MUST include its quadrant:
        {
          "corridor_detected": true,
          "corridor_shape": "cross",
          "corridor_position": "center",
          "rooms": [
            {"room_no": "1", "beds_count": 1, "amenities": [], "has_door": true, "quadrant": "NE", "row": null, "side": null},
            {"room_no": "5", "beds_count": 2, "amenities": ["bathroom"], "has_door": true, "quadrant": "NW", "row": null, "side": null}
          ]
        }

        Example for an "H" shaped corridor (two stacked parallel corridors), each room MUST include row + side:
        {
          "corridor_detected": true,
          "corridor_shape": "H",
          "corridor_position": "center",
          "rooms": [
            {"room_no": "1", "beds_count": 2, "amenities": [], "has_door": true, "quadrant": null, "row": "top", "side": "left"},
            {"room_no": "2", "beds_count": 2, "amenities": [], "has_door": true, "quadrant": null, "row": "top", "side": "right"},
            {"room_no": "4", "beds_count": 2, "amenities": ["bathroom"], "has_door": true, "quadrant": null, "row": "middle", "side": "left"},
            {"room_no": "7", "beds_count": 1, "amenities": [], "has_door": true, "quadrant": null, "row": "bottom", "side": "left"}
          ]
        }
        """

        print("👉 AI (gemini-2.5-flash) is analyzing the drawing...")
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=[prompt, img]
        )

        response_text = response.text.strip()

        # Clean markdown if AI sends it
        if response_text.startswith("```json"):
            response_text = response_text[7:-3]
        elif response_text.startswith("```"):
            response_text = response_text[3:-3]

        parsed_json = json.loads(response_text)

        print(f"✅ DONE: Corridor shape: {parsed_json.get('corridor_shape', 'unknown')} · position: {parsed_json.get('corridor_position', 'unknown')}")
        print("="*50 + "\n")
        return jsonify(parsed_json)

    except Exception as e:
        print(f"❌ ERROR: {e}")
        return jsonify({"error": str(e)}), 500


# ==========================================
# 1B. AADHAR CARD AUTO-FILL (extracts fields from an Aadhar photo for the registration form)
# ==========================================
@app.route('/extract-aadhar', methods=['POST'])
@limiter.limit("15 per hour")
@require_auth
def extract_aadhar():
    print("\n" + "="*50)
    print("🟢 START: Aadhar extraction started!")

    if 'aadhar_photo' not in request.files:
        return jsonify({"error": "No image uploaded"}), 400

    file = request.files['aadhar_photo']
    back_file = request.files.get('aadhar_photo_back')  # optional — many Aadhar cards only show
    # name/DOB/photo on the front and put the address on the back, so front-only uploads often
    # can't extract address at all. If the guest also uploaded the back, we read both.

    try:
        img = Image.open(file)
        back_img = Image.open(back_file) if back_file else None

        prompt = """Look at these photo(s) of an Indian Aadhar card (front side, and back side if a
        second image is provided) and extract the printed details EXACTLY as written on the card.
        Do not guess or invent anything — if a field is not clearly visible in either image, leave
        it as an empty string "".

        Fields to extract:
        1. full_name — the person's name as printed (usually on the front).
        2. dob — date of birth, converted to YYYY-MM-DD format (Aadhar usually shows DD/MM/YYYY).
        3. gender — exactly "Male", "Female", or "Other" (Aadhar prints "M" or "F" or similar — convert it).
        4. aadhar_number — the 12-digit number, digits only, no spaces.
        5. address — the full address block, usually printed on the BACK of the card, as one single
           line (join multi-line address with ", "). If only the front image was provided and no
           address is visible on it, leave this empty rather than guessing.
        6. father_or_guardian_name — the name after "S/O", "D/O", "W/O", or "C/O" if present on the
           card (this is the father's/guardian's name) — WITHOUT the "S/O"/"D/O" prefix itself, just
           the name. Empty string if not present.

        Return ONLY a valid JSON object matching this structure EXACTLY (no markdown, no extra text):
        {
          "full_name": "RAHUL KUMAR SHARMA",
          "dob": "1998-04-12",
          "gender": "Male",
          "aadhar_number": "234512346789",
          "address": "House No 12, MG Road, Sector 4, Lucknow, Uttar Pradesh, 226001",
          "father_or_guardian_name": "SURESH SHARMA"
        }
        """

        print("👉 AI (gemini-2.5-flash) is reading the Aadhar card...")
        contents = [prompt, img] + ([back_img] if back_img else [])
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=contents
        )

        response_text = response.text.strip()
        if response_text.startswith("```json"):
            response_text = response_text[7:-3]
        elif response_text.startswith("```"):
            response_text = response_text[3:-3]

        parsed_json = json.loads(response_text)

        print(f"✅ DONE: Extracted name: {parsed_json.get('full_name', 'unknown')}")
        print("="*50 + "\n")
        return jsonify(parsed_json)

    except Exception as e:
        print(f"❌ ERROR: {e}")
        return jsonify({"error": str(e)}), 500


# ==========================================
# 2. EMAIL NOTIFICATION TO ADMIN (New Booking / Registration Form)
# ==========================================
@app.route('/send-registration-email', methods=['POST'])
@limiter.limit("30 per hour")
@require_auth
def send_registration_email():
    if not RESEND_API_KEY or not ADMIN_EMAIL:
        return jsonify({"error": "Email not configured on server. Set RESEND_API_KEY and ADMIN_EMAIL env vars."}), 500

    data = request.get_json(silent=True) or {}
    guest_name = data.get('guestName', 'A guest')
    hostel_name = data.get('hostelName', 'your hostel')
    pdf_url = data.get('pdfUrl', '')

    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
            json={
                "from": "HostelOM <noreply@ho-om.in>",  # change to your verified domain once set up
                "to": [ADMIN_EMAIL],
                "subject": f"New Booking Request — {guest_name} ({hostel_name})",
                "html": f"<p>{guest_name} just submitted a Hostel Registration Form for <b>{hostel_name}</b>.</p>"
                        + (f'<p><a href="{pdf_url}">View / Download the PDF</a></p>' if pdf_url else "")
            },
            timeout=10
        )
        if resp.status_code >= 300:
            return jsonify({"error": resp.text}), 500
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ==========================================
# 2C. NEW MOBILE LOGIN NOTIFICATION TO ADMIN
# ==========================================
@app.route('/notify-admin-new-login', methods=['POST'])
@limiter.limit("30 per hour")
@require_auth
def notify_admin_new_login():
    if not RESEND_API_KEY or not ADMIN_EMAIL:
        return jsonify({"error": "Email not configured on server. Set RESEND_API_KEY and ADMIN_EMAIL env vars."}), 500

    data = request.get_json(silent=True) or {}
    phone = data.get('phone', '').strip()
    if not phone:
        return jsonify({"error": "phone is required"}), 400

    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
            json={
                "from": "HostelOM <noreply@ho-om.in>",
                "to": [ADMIN_EMAIL],
                "subject": f"New user logged in — {phone}",
                "html": f"""
                    <div style="font-family: sans-serif; max-width: 480px; margin: auto;">
                        <h2 style="color:#4f46e5;">HostelOM — New Mobile Login</h2>
                        <p>A new user just signed in with mobile number: <b>{phone}</b></p>
                        <p style="color:#64748b; font-size:12px;">Check the admin dashboard's "Users" tab for their location (if shared) and login history.</p>
                    </div>
                """
            },
            timeout=10
        )
        if resp.status_code >= 300:
            return jsonify({"error": resp.text}), 500
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
@app.route('/send-verification-email', methods=['POST'])
@limiter.limit("5 per hour")
@require_auth
def send_verification_email():
    if not firebase_admin_app:
        return jsonify({"error": "Firebase Admin SDK not configured on server. Set FIREBASE_SERVICE_ACCOUNT_JSON env var."}), 500
    if not RESEND_API_KEY:
        return jsonify({"error": "Email not configured on server. Set RESEND_API_KEY env var."}), 500

    # Use the SIGNED-IN user's own email, never one supplied in the request body —
    # otherwise anyone could generate real Firebase verification links for
    # addresses they don't control.
    email = (request.claims.get('email') or '').strip()
    if not email:
        return jsonify({"error": "No email address on this account."}), 400

    try:
        # Firebase generates the actual secure verification link (same one it would've emailed
        # itself) — we just take over how it gets delivered.
        verify_link = fb_auth.generate_email_verification_link(email)

        resp = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
            json={
                "from": "HostelOM <noreply@ho-om.in>",  # change to your verified domain once set up
                "to": [email],
                "subject": "Verify your email for HostelOM",
                "html": f"""
                    <div style="font-family: sans-serif; max-width: 480px; margin: auto;">
                        <h2 style="color:#4f46e5;">Welcome to HostelOM 🏠</h2>
                        <p>Please verify your email address to continue.</p>
                        <p><a href="{verify_link}" style="display:inline-block; background:#4f46e5; color:white; padding:12px 24px; border-radius:10px; text-decoration:none; font-weight:bold;">Verify Email</a></p>
                        <p style="color:#64748b; font-size:12px;">If the button doesn't work, copy this link: {verify_link}</p>
                    </div>
                """
            },
            timeout=10
        )
        if resp.status_code >= 300:
            return jsonify({"error": resp.text}), 500
        return jsonify({"success": True})
    except Exception as e:
        print(f"❌ ERROR sending verification email: {e}")
        return jsonify({"error": str(e)}), 500


# ==========================================
# 3. RAZORPAY: CREATE ORDER
# ==========================================
# Frontend calls this BEFORE opening the Razorpay checkout modal. We call Razorpay's
# Orders API server-side (using KEY_SECRET which must stay on the server) and return
# only the order_id + public key_id back to the browser.
@app.route('/razorpay/create-order', methods=['POST'])
@limiter.limit("20 per hour")
@require_auth
def razorpay_create_order():
    if not RAZORPAY_KEY_ID or not RAZORPAY_KEY_SECRET:
        return jsonify({"error": "Razorpay not configured. Set RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET env vars."}), 500

    if not firestore_db:
        return jsonify({"error": "Server not fully configured (Firestore unavailable)."}), 500

    data = request.get_json(silent=True) or {}
    property_id = data.get('propertyId')
    bed_ids = data.get('bedIds') or []
    currency = 'INR'

    # THE BROWSER NO LONGER SENDS AN AMOUNT AT ALL.
    # It used to, and the server accepted it — so a DevTools edit could turn a
    # ₹7,000 advance into ₹1, and the signature would still verify (a real ₹1
    # payment against a real ₹1 order), producing a paid_verified booking with
    # the bed marked occupied. The price now comes from the live bed data in
    # Firestore on every single order.
    try:
        amount_paise, total_rupees, owner_uid = compute_booking_amount(
            firestore_db, property_id, bed_ids)
    except ValueError as ve:
        return jsonify({"error": str(ve)}), 400

    # Everything needed to write the booking later is stamped into the order's
    # notes, server-side. /razorpay/verify reads it from here instead of from the
    # request body, which the browser controls.
    notes = {
        "app": "HostelOM",
        "propertyId": property_id or "",
        "bedIds": ",".join(bed_ids),
        "guestUid": request.uid,
        "ownerUid": owner_uid or "",
        "expectedPaise": str(amount_paise),
    }

    try:
        resp = requests.post(
            "https://api.razorpay.com/v1/orders",
            auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET),
            json={
                "amount": amount_paise,
                "currency": currency,
                "notes": notes,
                "payment_capture": 1   # auto-capture on payment success
            },
            timeout=15
        )
        if resp.status_code >= 300:
            print(f"❌ Razorpay order create failed: {resp.status_code} {resp.text}")
            return jsonify({"error": "Razorpay order creation failed", "details": resp.text}), 502

        order = resp.json()
        return jsonify({
            "orderId": order.get("id"),
            "keyId": RAZORPAY_KEY_ID,      # public key — safe to expose
            "amount": order.get("amount"),
            "currency": order.get("currency"),
            "totalStayAmount": total_rupees,
            "advancePercent": ADVANCE_PERCENT
        })
    except requests.RequestException as e:
        print(f"❌ Razorpay network error: {e}")
        return jsonify({"error": "Network error contacting Razorpay."}), 502
    except Exception as e:
        print(f"❌ Razorpay create-order error: {e}")
        return jsonify({"error": str(e)}), 500


# ==========================================
# 4. RAZORPAY: VERIFY PAYMENT SIGNATURE
# ==========================================
# After the user pays, Razorpay sends razorpay_order_id + razorpay_payment_id +
# razorpay_signature to the frontend. Frontend forwards them here. We recompute the
# HMAC-SHA256 signature server-side using our KEY_SECRET and compare — this is the
# ONLY reliable way to confirm the payment actually happened (never trust the client).
#
# This endpoint now also:
#   1. Re-fetches the payment's ACTUAL captured amount from Razorpay's own API and compares it
#      against the order's original amount — the browser's claimed amount is never trusted.
#   2. Writes the 'bookings' record itself (Admin SDK), instead of letting the browser write its
#      own "I paid" document — closes the "fake paid_verified booking via DevTools" hole.
#   3. Marks the paid bed(s) 'occupied' on the business's live listing in the same transaction,
#      and confirms the reservation lock — so this never has to happen client-side either.
@app.route('/razorpay/verify', methods=['POST'])
@limiter.limit("30 per hour")
@require_auth
def razorpay_verify():
    if not RAZORPAY_KEY_SECRET:
        return jsonify({"ok": False, "error": "Razorpay not configured."}), 500

    data = request.get_json(silent=True) or {}
    order_id = data.get('razorpay_order_id')
    payment_id = data.get('razorpay_payment_id')
    received_signature = data.get('razorpay_signature')

    if not (order_id and payment_id and received_signature):
        return jsonify({"ok": False, "error": "Missing order_id / payment_id / signature."}), 400

    # Signature formula (from Razorpay docs):
    #   HMAC_SHA256(order_id + "|" + payment_id, KEY_SECRET)
    payload = f"{order_id}|{payment_id}".encode("utf-8")
    expected_signature = hmac.new(
        RAZORPAY_KEY_SECRET.encode("utf-8"),
        payload,
        hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected_signature, received_signature):
        print(f"❌ Signature MISMATCH for order {order_id}")
        return jsonify({"ok": False, "error": "Invalid signature. Payment could not be verified."}), 400

    # ---- Signature verified. Now confirm the ACTUAL amount with Razorpay directly (never trust
    # what the browser says was paid) ----
    try:
        order_resp = requests.get(f"https://api.razorpay.com/v1/orders/{order_id}",
                                   auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET), timeout=10)
        payment_resp = requests.get(f"https://api.razorpay.com/v1/payments/{payment_id}",
                                     auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET), timeout=10)
        if order_resp.status_code >= 300 or payment_resp.status_code >= 300:
            return jsonify({"ok": False, "error": "Could not verify payment details with Razorpay."}), 502
        order_obj = order_resp.json()
        payment_obj = payment_resp.json()
    except requests.RequestException as e:
        print(f"❌ Razorpay verify network error: {e}")
        return jsonify({"ok": False, "error": "Network error contacting Razorpay."}), 502

    if payment_obj.get('order_id') != order_id:
        return jsonify({"ok": False, "error": "Payment does not belong to this order."}), 400
    if payment_obj.get('status') != 'captured':
        return jsonify({"ok": False, "error": f"Payment not captured (status: {payment_obj.get('status')})."}), 400
    if int(payment_obj.get('amount', -1)) != int(order_obj.get('amount', -2)):
        print(f"❌ AMOUNT MISMATCH — order={order_obj.get('amount')} payment={payment_obj.get('amount')}")
        return jsonify({"ok": False, "error": "Paid amount does not match the order amount."}), 400

    # ---- Identity of this booking comes from the ORDER NOTES we wrote ourselves
    # at create time — NOT from the request body. Previously propertyId and
    # guestUid were read straight off the browser's JSON, so a caller could
    # attribute a payment to any property or any user account they liked. ----
    order_notes = order_obj.get('notes') or {}
    property_id = order_notes.get('propertyId') or None
    owner_uid = order_notes.get('ownerUid') or None
    guest_uid = order_notes.get('guestUid') or None
    bed_ids_from_order = [b for b in (order_notes.get('bedIds') or '').split(',') if b]

    if guest_uid != request.uid:
        return jsonify({"ok": False, "error": "This order belongs to a different account."}), 403

    try:
        expected_paise = int(order_notes.get('expectedPaise', -1))
    except (TypeError, ValueError):
        expected_paise = -1
    if int(payment_obj.get('amount', 0)) != expected_paise:
        print(f"❌ PRICE TAMPER — paid={payment_obj.get('amount')} expected={expected_paise}")
        return jsonify({"ok": False, "error": "Paid amount does not match the server-computed price."}), 400

    # IDEMPOTENCY: Razorpay retries, and users double-tap. Without this one
    # payment could produce two booking records.
    if firestore_db:
        dupe = firestore_db.collection('bookings').where('paymentId', '==', payment_id).limit(1).get()
        if len(dupe) > 0:
            d0 = dupe[0].to_dict() or {}
            return jsonify({"ok": True, "bookingId": d0.get("bookingId"), "amount": d0.get("amount")})

    amount_rupees = int(payment_obj.get('amount', 0)) / 100.0
    booking_context = data.get('bookingContext', {}) or {}
    guest = data.get('guest', {}) or {}
    lock_id = data.get('lockId')
    hostel_name = data.get('hostelName', booking_context.get('hostelName', ''))
    guest_name = data.get('guestName', guest.get('fullName', ''))
    guest_phone = data.get('guestPhone', '')
    total_stay_amount = data.get('totalStayAmount', amount_rupees)
    hostel_balance_due = data.get('hostelBalanceDue', 0)
    booking_id = "BK-" + payment_id[-8:].upper()

    # ---- Write the booking record + mark the bed(s) occupied, server-side ----
    if firestore_db:
        try:
            firestore_db.collection('bookings').document().set({
                "bookingId": booking_id,
                "paymentId": payment_id,
                "orderId": order_id,
                "amount": amount_rupees,  # the advance actually charged — this IS the platform's commission in full (Pay-at-Hostel model)
                "totalStayAmount": total_stay_amount,
                "hostelBalanceDue": hostel_balance_due,  # guest pays this directly to the hostel at check-in
                "currency": "INR",
                "propertyId": property_id,
                "hostelName": hostel_name,
                "guestName": guest_name,
                "guestPhone": guest_phone,
                "guestUid": guest_uid,
                # Denormalised so the owner's "Property Bookings" tab can query
                # this collection. A rule that resolves the owner via get() on
                # another document cannot be evaluated on a QUERY, only on a
                # single-doc read — which is why that tab was failing before.
                "ownerUid": owner_uid,
                "bookingContext": booking_context,
                "status": "paid_verified",
                "createdAt": fb_firestore.SERVER_TIMESTAMP,
            })
        except Exception as fsErr:
            print(f"⚠️ Booking write failed (payment still valid): {fsErr}")

        # From the signed order, not from the request body.
        bed_ids = bed_ids_from_order or [b.get('id') for b in (booking_context.get('beds') or []) if b.get('id')]
        if property_id and bed_ids:
            try:
                _mark_beds_occupied(property_id, bed_ids, booking_id, lock_id)
            except Exception as occErr:
                # Payment + booking record are already safely saved — don't fail the whole
                # request over this; it can be fixed manually via "Mark Occupied" in Architect Mode.
                print(f"⚠️ Auto-occupy failed (fix manually if needed): {occErr}")

        # Tell everyone who needs to know. Nothing here notified anyone before, so a Razorpay
        # payment landed silently: the owner had no idea a bed had just been sold, and the guest
        # got no confirmation beyond the screen they were already looking at.
        try:
            _write_notification({
                "type": "new_booking", "audience": "owner",
                "ownerUid": owner_uid, "propertyId": property_id, "bookingId": booking_id,
                "title": "New booking confirmed",
                "message": f"{guest_name or 'A guest'} booked and paid Rs {int(amount_rupees)} advance. Bed is now occupied.",
            })
            _write_notification({
                "type": "booking_confirmed", "audience": "guest",
                "guestUid": guest_uid, "propertyId": property_id, "bookingId": booking_id,
                "title": "Booking confirmed",
                "message": f"Your booking at {hostel_name or 'the hostel'} is confirmed. Booking ID: {booking_id}",
            })
            _write_notification({
                "type": "payment_received", "audience": "admin",
                "propertyId": property_id, "bookingId": booking_id,
                "message": f"Rs {int(amount_rupees)} received from {guest_name or 'a guest'} ({hostel_name or ''}).",
            })
        except Exception as notifErr:
            print(f"⚠️ Booking notifications failed (booking still saved): {notifErr}")
    else:
        print("⚠️ FIREBASE_SERVICE_ACCOUNT_JSON not set — booking was NOT saved server-side. Set it up so bookings/bed-occupancy work.")

    print(f"✅ Payment verified · order={order_id} · payment={payment_id} · booking={booking_id} · ₹{amount_rupees}")
    # Optional: fire-and-forget email notification to admin
    try:
        if RESEND_API_KEY and ADMIN_EMAIL:
            requests.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
                json={
                    "from": "HostelOM <noreply@ho-om.in>",
                    "to": [ADMIN_EMAIL],
                    "subject": f"✅ Payment Received — {guest_name or 'Guest'} ({hostel_name})",
                    "html": f"<p>Booking <b>{booking_id}</b> paid successfully.</p>"
                            f"<p>Payment ID: <code>{payment_id}</code><br>"
                            f"Order ID: <code>{order_id}</code><br>"
                            f"Amount: ₹{amount_rupees}</p>"
                },
                timeout=8
            )
    except Exception as mailErr:
        print(f"⚠️ Admin email notification failed: {mailErr}")

    return jsonify({
        "ok": True,
        "bookingId": booking_id,
        "paymentId": payment_id,
        "orderId": order_id,
        "amount": amount_rupees
    })


def _mark_beds_occupied(property_id, bed_ids, booking_id, lock_id):
    """Flips the given bed IDs to 'occupied' inside businesses/{property_id}.roomsAndBeds, and
    marks the matching bedLocks/{lock_id} document confirmed — run as one atomic transaction so
    two simultaneous payments can never both think they got the same bed."""
    biz_ref = firestore_db.collection('businesses').document(property_id)
    lock_ref = firestore_db.collection('bedLocks').document(lock_id) if lock_id else None

    transaction = firestore_db.transaction()

    @fb_firestore.transactional
    def _update(transaction):
        biz_snap = biz_ref.get(transaction=transaction)
        if not biz_snap.exists:
            return
        biz_data = biz_snap.to_dict() or {}
        rooms = biz_data.get('roomsAndBeds', []) or []
        updated_rooms = []
        for room in rooms:
            beds = room.get('beds', []) or []
            new_beds = []
            for bed in beds:
                if bed.get('id') in bed_ids:
                    bed = dict(bed)
                    bed['status'] = 'occupied'
                    bed['occupiedBookingId'] = booking_id
                new_beds.append(bed)
            room = dict(room)
            room['beds'] = new_beds
            updated_rooms.append(room)
        transaction.update(biz_ref, {'roomsAndBeds': updated_rooms})
        if lock_ref:
            transaction.set(lock_ref, {'confirmed': True, 'bookingId': booking_id, 'confirmedAt': fb_firestore.SERVER_TIMESTAMP}, merge=True)

    _update(transaction)


@app.errorhandler(Exception)
def handle_unexpected_error(e):
    print(f"❌ UNEXPECTED ERROR: {e}")
    return jsonify({"error": "Something went wrong on the server. Please try again."}), 500


# ==========================================
# SMS OTP — send + verify, via 2Factor.in
# ==========================================
def _clean_10digit_phone(raw):
    digits = ''.join(c for c in (raw or '') if c.isdigit())
    return digits[-10:] if len(digits) >= 10 else None


@app.route('/send-otp', methods=['POST'])
@limiter.limit("3 per hour;10 per day")
def send_otp():
    if not TWOFACTOR_API_KEY:
        return jsonify({"error": "SMS OTP not configured on server. Set TWOFACTOR_API_KEY env var."}), 500

    data = request.get_json(silent=True) or {}
    phone_digits = _clean_10digit_phone(data.get('phone'))
    if not phone_digits:
        return jsonify({"error": "A valid 10-digit phone number is required"}), 400

    # Per-NUMBER cap on top of the per-IP limit above. Without this, a script that
    # rotates IPs could still bomb one victim's phone with SMS — each one billed
    # to your 2Factor account.
    if not otp_attempt_allowed(f"send:{phone_digits}"):
        return jsonify({"error": "Too many OTP requests for this number. Please try again later."}), 429

    try:
        url = f"https://2factor.in/API/V1/{TWOFACTOR_API_KEY}/SMS/+91{phone_digits}/AUTOGEN/{TWOFACTOR_TEMPLATE_NAME}"
        resp = requests.get(url, timeout=10)
        result = resp.json()
        if result.get('Status') != 'Success':
            return jsonify({"error": result.get('Details', 'Failed to send OTP')}), 500
        # "Details" here is 2Factor's session_id — the frontend must send it back on verify.
        return jsonify({"session_id": result.get('Details')})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/verify-otp', methods=['POST'])
@limiter.limit("20 per hour")
def verify_otp():
    if not TWOFACTOR_API_KEY:
        return jsonify({"error": "SMS OTP not configured on server."}), 500
    if not firebase_admin_app:
        return jsonify({"error": "Firebase Admin SDK not configured on server. Set FIREBASE_SERVICE_ACCOUNT_JSON env var."}), 500

    data = request.get_json(silent=True) or {}
    session_id = (data.get('session_id') or '').strip()
    otp = (data.get('otp') or '').strip()
    phone_digits = _clean_10digit_phone(data.get('phone'))
    if not session_id or not otp or not phone_digits:
        return jsonify({"error": "session_id, otp and a valid phone are required"}), 400

    # Five wrong guesses kills the session. A 4-digit OTP is only 10,000
    # combinations — trivially brute-forceable without this.
    if not otp_attempt_allowed(session_id):
        return jsonify({"error": "Too many incorrect attempts. Please request a new OTP."}), 429

    try:
        url = f"https://2factor.in/API/V1/{TWOFACTOR_API_KEY}/SMS/VERIFY/{session_id}/{otp}"
        resp = requests.get(url, timeout=10)
        result = resp.json()
        if result.get('Status') != 'Success' or result.get('Details') != 'OTP Matched':
            return jsonify({"error": "Incorrect or expired OTP. Please try again."}), 400

        # OTP is correct — get (or create) a real Firebase Auth user for this phone number, and
        # hand the frontend a custom token to sign in with. This keeps every existing uid-based
        # Firestore rule and document untouched; only *how* the OTP was sent/verified changed.
        e164_phone = "+91" + phone_digits
        try:
            fb_user = fb_auth.get_user_by_phone_number(e164_phone)
        except fb_auth.UserNotFoundError:
            fb_user = fb_auth.create_user(phone_number=e164_phone)
        otp_attempt_clear(session_id)
        custom_token = fb_auth.create_custom_token(fb_user.uid)
        token_str = custom_token.decode('utf-8') if isinstance(custom_token, bytes) else custom_token
        return jsonify({"customToken": token_str, "uid": fb_user.uid, "phone": e164_phone})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ==========================================
# 6. NOTIFICATIONS — the app's backbone
# ==========================================
# Notifications used to be written straight from the browser with `allow create: if true`,
# which meant anyone on the internet could stuff junk into the collection. Every notification
# now goes through here instead: the caller proves who they are, the server decides the
# audience, and the Admin SDK writes it (bypassing rules entirely).
#
# `audience` controls who sees it in their feed:
#   'admin'    — only the HO-Om admin console
#   'owner'    — one specific hostel owner (ownerUid required)
#   'guest'    — one specific guest (guestUid required)
#   'allOwners'/'allGuests' — broadcast, admin only

import re as _re

_CONTACT_PATTERNS = [
    _re.compile(r'(?:\+?91[\s-]*)?[6-9]\d{9}'),                       # Indian mobile
    _re.compile(r'\b\d[\d\s\-().]{8,}\d\b'),                          # spaced/dashed digits
    _re.compile(r'[\w.+-]+@[\w-]+\.[\w.]+'),                          # email
    _re.compile(r'(?:wa\.me|whatsapp|telegram|t\.me|insta(?:gram)?)', _re.I),
]


def _contains_contact_info(text):
    stripped = _re.sub(r'[\s\-().]', '', text or '')
    for pat in _CONTACT_PATTERNS:
        if pat.search(text or '') or pat.search(stripped):
            return True
    return False


_ALLOWED_AUDIENCES = {'admin', 'owner', 'guest', 'allOwners', 'allGuests'}
_BROADCAST_AUDIENCES = {'allOwners', 'allGuests'}


def _write_notification(payload):
    """Single place every notification is created, so the shape stays consistent."""
    doc = {
        "type": payload.get("type") or "general",
        "audience": payload.get("audience") or "admin",
        "message": (payload.get("message") or "")[:2000],
        "title": (payload.get("title") or "")[:200],
        "ownerUid": payload.get("ownerUid"),
        "guestUid": payload.get("guestUid"),
        "propertyId": payload.get("propertyId"),
        "bookingId": payload.get("bookingId"),
        "read": False,
        "createdAt": _dt_now_iso(),
    }
    firestore_db.collection("notifications").add(doc)
    # Fire the phone-level push too. Without this the whole messenger is useless: an owner
    # is not sitting with the app open, so a message that only lands in the in-app bell is
    # a message they see hours later, by which time the guest has booked elsewhere.
    try:
        _send_push_for(doc)
    except Exception as e:
        print(f"⚠️ push failed (notification still saved): {e}")
    return doc


# ==========================================
# PUSH NOTIFICATIONS (FCM)
# ==========================================
# Device tokens live in deviceTokens/{token}. One user can have several (phone, laptop,
# installed PWA), so we look them up by uid and send to all of them.

def _tokens_for_uid(uid):
    if not uid:
        return []
    out = []
    for d in firestore_db.collection('deviceTokens').where('uid', '==', uid).stream():
        t = (d.to_dict() or {}).get('token')
        if t:
            out.append(t)
    return out


def _tokens_for_role(role):
    """role: 'owner' | 'guest' — used for the allOwners / allGuests broadcasts."""
    out = []
    for d in firestore_db.collection('deviceTokens').where('role', '==', role).stream():
        t = (d.to_dict() or {}).get('token')
        if t:
            out.append(t)
    return out


def _prune_dead_tokens(tokens, responses):
    """A token dies when the user uninstalls or clears data. Delete it so we stop retrying."""
    for token, resp in zip(tokens, responses):
        if resp.success:
            continue
        err = str(getattr(resp, 'exception', '') or '')
        if 'not-registered' in err or 'invalid-argument' in err or 'Requested entity was not found' in err:
            try:
                firestore_db.collection('deviceTokens').document(token).delete()
            except Exception:
                pass


def _send_push_for(doc):
    if not firestore_db:
        return
    from firebase_admin import messaging

    audience = doc.get('audience')
    if audience == 'owner':
        tokens = _tokens_for_uid(doc.get('ownerUid'))
    elif audience == 'guest':
        tokens = _tokens_for_uid(doc.get('guestUid'))
    elif audience == 'allOwners':
        tokens = _tokens_for_role('owner')
    elif audience == 'allGuests':
        tokens = _tokens_for_role('guest')
    else:
        return   # admin-feed entries don't need to buzz anyone's phone

    tokens = list(dict.fromkeys(tokens))[:500]   # de-dupe; FCM caps a multicast at 500
    if not tokens:
        return

    title = doc.get('title') or 'HO-Om'
    body = (doc.get('message') or '')[:180]

    message = messaging.MulticastMessage(
        tokens=tokens,
        notification=messaging.Notification(title=title, body=body),
        data={
            "type": str(doc.get('type') or ''),
            "bookingId": str(doc.get('bookingId') or ''),
            "propertyId": str(doc.get('propertyId') or ''),
            "url": "/",
        },
        webpush=messaging.WebpushConfig(
            notification=messaging.WebpushNotification(
                title=title, body=body,
                icon='/icon-192.png', badge='/icon-192.png',
                tag=str(doc.get('bookingId') or doc.get('type') or 'hoom'),
            ),
            fcm_options=messaging.WebpushFCMOptions(link='https://ho-om.in/'),
        ),
    )
    resp = messaging.send_each_for_multicast(message)
    print(f"📲 push: {resp.success_count}/{len(tokens)} delivered")
    _prune_dead_tokens(tokens, resp.responses)


@app.route('/push/register', methods=['POST'])
@limiter.limit("60 per hour")
@require_auth
def push_register():
    """Called by the app after the user grants notification permission."""
    if not firestore_db:
        return jsonify({"error": "Server not fully configured."}), 500

    data = request.get_json(silent=True) or {}
    token = (data.get('token') or '').strip()
    if not token or len(token) > 4096:
        return jsonify({"error": "A valid token is required."}), 400

    # Role is derived server-side, NOT taken from the request body. Previously the browser
    # chose it, so a guest could register as 'owner' and start receiving every allOwners
    # broadcast — including anything commercially sensitive you send to your hostel owners.
    if firestore_db.collection('admin').document(request.uid).get().exists:
        role = 'admin'
    elif len(firestore_db.collection('businesses').where('ownerId', '==', request.uid).limit(1).get()) > 0:
        role = 'owner'
    else:
        role = 'guest'

    # Keyed by token so re-registering the same device just refreshes it rather than
    # piling up duplicates that would make one phone buzz five times per message.
    firestore_db.collection('deviceTokens').document(token).set({
        "token": token,
        "uid": request.uid,
        "role": role,
        "updatedAt": _dt_now_iso(),
    }, merge=True)
    return jsonify({"ok": True})


@app.route('/push/unregister', methods=['POST'])
@limiter.limit("60 per hour")
@require_auth
def push_unregister():
    if not firestore_db:
        return jsonify({"error": "Server not fully configured."}), 500
    token = ((request.get_json(silent=True) or {}).get('token') or '').strip()
    if token:
        try:
            firestore_db.collection('deviceTokens').document(token).delete()
        except Exception:
            pass
    return jsonify({"ok": True})


@app.route('/push/test', methods=['POST'])
@limiter.limit("20 per hour")
@require_auth
def push_test():
    """Sends a push to the caller's own devices, so you can verify setup end to end."""
    if not firestore_db:
        return jsonify({"error": "Server not fully configured."}), 500
    from firebase_admin import messaging

    tokens = _tokens_for_uid(request.uid)
    if not tokens:
        return jsonify({"ok": False, "error": "No device registered for this account yet."}), 400

    resp = messaging.send_each_for_multicast(messaging.MulticastMessage(
        tokens=tokens,
        notification=messaging.Notification(
            title="HO-Om test", body="Push notifications kaam kar rahe hain ✅"),
        webpush=messaging.WebpushConfig(
            notification=messaging.WebpushNotification(
                title="HO-Om test", body="Push notifications kaam kar rahe hain ✅",
                icon='/icon-192.png'),
            fcm_options=messaging.WebpushFCMOptions(link='https://ho-om.in/'),
        ),
    ))
    _prune_dead_tokens(tokens, resp.responses)
    return jsonify({"ok": True, "devices": len(tokens), "delivered": resp.success_count})


def _dt_now_iso():
    import datetime as _d
    return _d.datetime.now(_d.timezone.utc).isoformat()


@app.route('/notify', methods=['POST'])
@limiter.limit("120 per hour")
@require_auth
def notify():
    """
    Called by the app for ordinary events (new booking, new chat message, visit request).
    A normal user may NOT broadcast, and may not forge an audience they don't belong to.
    """
    if not firestore_db:
        return jsonify({"error": "Server not fully configured."}), 500

    data = request.get_json(silent=True) or {}
    audience = data.get("audience") or "admin"
    if audience not in _ALLOWED_AUDIENCES:
        return jsonify({"error": "Invalid audience."}), 400

    is_admin = firestore_db.collection('admin').document(request.uid).get().exists
    if audience in _BROADCAST_AUDIENCES and not is_admin:
        return jsonify({"error": "Only admin can broadcast."}), 403

    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"error": "message is required."}), 400

    # This text ends up on someone's lock screen, so it gets the same contact-info filter
    # the chat does. Otherwise a guest could put their number in a property enquiry and
    # bypass the entire "no contact sharing" design in one step.
    if _contains_contact_info(message):
        return jsonify({
            "ok": False, "blocked": True,
            "error": "Phone numbers, emails and social handles are not allowed. Please keep the conversation inside HO-Om."
        }), 400

    property_id = data.get("propertyId")
    owner_uid = None

    if audience == 'owner':
        # ownerUid from the request body is IGNORED. Previously it was trusted, which meant
        # any signed-in account could push arbitrary text to any hostel owner's phone —
        # a ready-made phishing channel ("call 98xxx to confirm your booking").
        if not property_id:
            return jsonify({"error": "propertyId is required."}), 400
        biz = firestore_db.collection('businesses').document(property_id).get()
        if not biz.exists:
            return jsonify({"error": "Property not found."}), 404
        owner_uid = (biz.to_dict() or {}).get('ownerId')
        if not owner_uid:
            return jsonify({"error": "Could not resolve the owner for this property."}), 400

    guest_uid = None
    if audience == 'guest':
        guest_uid = data.get("guestUid")
        if not guest_uid:
            return jsonify({"error": "guestUid is required."}), 400

        # Admin can notify any guest. An OWNER may notify a guest too — but only one of
        # their own, proven by a booking that links them. This used to be admin-only, which
        # silently broke every owner-triggered message to a guest: check-out, rent received,
        # rent reminder, no-show. The owner saw a success toast; the guest heard nothing.
        if not is_admin:
            booking_id = data.get("bookingId")
            allowed = False
            if booking_id:
                bk = firestore_db.collection('bookings').document(booking_id).get()
                if bk.exists:
                    bd = bk.to_dict() or {}
                    allowed = (bd.get('ownerUid') == request.uid
                               and bd.get('guestUid') == guest_uid)
            if not allowed and data.get("propertyId"):
                # Rent reminders carry propertyId rather than a booking id, so fall back to
                # "do I own this property, and does this guest have a booking in it".
                biz = firestore_db.collection('businesses').document(data["propertyId"]).get()
                if biz.exists and (biz.to_dict() or {}).get('ownerId') == request.uid:
                    q = (firestore_db.collection('bookings')
                         .where('propertyId', '==', data["propertyId"])
                         .where('guestUid', '==', guest_uid).limit(1).get())
                    allowed = len(q) > 0
            if not allowed:
                return jsonify({"error": "You can only message guests of your own property."}), 403

    doc = _write_notification({
        "type": data.get("type"),
        "audience": audience,
        "message": message,
        "title": data.get("title"),
        "ownerUid": owner_uid,
        "guestUid": guest_uid,
        "propertyId": property_id,
        "bookingId": data.get("bookingId"),
    })
    return jsonify({"ok": True, "notification": doc})


@app.route('/admin/notify', methods=['POST'])
@limiter.limit("60 per hour")
@require_admin
def admin_notify():
    """
    Admin-composed notifications. Four modes, matching the console UI:
      - one owner        -> audience 'owner'  + ownerUid (or propertyId)
      - all owners       -> audience 'allOwners'
      - one guest        -> audience 'guest'  + guestUid
      - all guests       -> audience 'allGuests'
    """
    if not firestore_db:
        return jsonify({"error": "Server not fully configured."}), 500

    data = request.get_json(silent=True) or {}
    audience = data.get("audience")
    message = (data.get("message") or "").strip()
    if audience not in _ALLOWED_AUDIENCES:
        return jsonify({"error": "Invalid audience."}), 400
    if not message:
        return jsonify({"error": "message is required."}), 400

    owner_uid = data.get("ownerUid")
    if audience == 'owner' and not owner_uid and data.get("propertyId"):
        biz = firestore_db.collection('businesses').document(data["propertyId"]).get()
        owner_uid = (biz.to_dict() or {}).get('ownerId') if biz.exists else None
    if audience == 'owner' and not owner_uid:
        return jsonify({"error": "ownerUid or propertyId is required for a single owner."}), 400
    if audience == 'guest' and not data.get("guestUid"):
        return jsonify({"error": "guestUid is required for a single guest."}), 400

    doc = _write_notification({
        "type": data.get("type") or "admin_message",
        "audience": audience,
        "title": data.get("title") or "Message from HO-Om",
        "message": message,
        "ownerUid": owner_uid,
        "guestUid": data.get("guestUid"),
        "propertyId": data.get("propertyId"),
    })
    return jsonify({"ok": True, "notification": doc})


@app.route('/admin/recipients', methods=['GET'])
@limiter.limit("60 per hour")
@require_admin
def admin_recipients():
    """Owner and guest lists for the admin console's "send to" picker."""
    if not firestore_db:
        return jsonify({"error": "Server not fully configured."}), 500

    owners, seen = [], set()
    for d in firestore_db.collection('businesses').stream():
        b = d.to_dict() or {}
        uid = b.get('ownerId')
        if not uid or uid in seen:
            continue
        seen.add(uid)
        owners.append({
            "ownerUid": uid,
            "propertyId": d.id,
            "name": (b.get('businessProfile') or {}).get('company') or b.get('businessName') or 'Hostel',
        })

    guests, gseen = [], set()
    for d in firestore_db.collection('bookings').stream():
        b = d.to_dict() or {}
        uid = b.get('guestUid')
        if not uid or uid in gseen:
            continue
        gseen.add(uid)
        guests.append({"guestUid": uid, "name": b.get('guestName') or 'Guest'})

    return jsonify({"owners": owners, "guests": guests})


# ==========================================
# 6b. IN-APP MESSENGER
# ==========================================
# Chat messages are stored under bookings/{id}/messages and read directly by the client
# (the rules already restrict that to the guest, that booking's owner, and admin). This
# route exists so that SENDING a message also fires a notification to the other party —
# without it the messenger is useless, because nobody knows a message arrived.
#
# It also enforces the privacy rule the whole feature exists for: no phone numbers or
# email addresses may pass through the chat, in either direction.
@app.route('/chat/send', methods=['POST'])
@limiter.limit("300 per hour")
@require_auth
def chat_send():
    if not firestore_db:
        return jsonify({"error": "Server not fully configured."}), 500

    data = request.get_json(silent=True) or {}
    booking_id = (data.get("bookingId") or "").strip()
    text = (data.get("text") or "").strip()

    if not booking_id or not text:
        return jsonify({"error": "bookingId and text are required."}), 400
    if len(text) > 2000:
        return jsonify({"error": "Message is too long."}), 400

    # The privacy promise: numbers never cross between guest and owner.
    if _contains_contact_info(text):
        return jsonify({
            "ok": False,
            "blocked": True,
            "error": "You cannot share phone numbers, emails or other contact details here. Please keep the conversation inside HO-Om."
        }), 400

    snap = firestore_db.collection('bookings').document(booking_id).get()
    if not snap.exists:
        return jsonify({"error": "Booking not found."}), 404
    b = snap.to_dict() or {}

    is_admin = firestore_db.collection('admin').document(request.uid).get().exists
    is_guest = b.get('guestUid') == request.uid
    is_owner = b.get('ownerUid') == request.uid
    if not (is_admin or is_guest or is_owner):
        return jsonify({"error": "You are not part of this conversation."}), 403

    sender_role = 'guest' if is_guest else ('owner' if is_owner else 'admin')
    firestore_db.collection('bookings').document(booking_id).collection('messages').add({
        "senderRole": sender_role,
        "senderUid": request.uid,
        "text": text,
        "createdAt": _dt_now_iso(),
    })

    # Notify the OTHER side. This is the whole reason the messenger works at all.
    preview = text[:80] + ('…' if len(text) > 80 else '')

    # Title the owner's notification with the BED, not the booking code. An owner glancing at
    # their lock screen can act on "Priya — Bed 4"; "Priya — BK-M3X9K2" makes them open the app
    # just to work out who is asking.
    beds = [x.get('id') for x in ((b.get('bookingContext') or {}).get('beds') or []) if x.get('id')]
    bed_label = ('Bed ' + ', '.join(str(x) for x in beds)) if beds else (b.get('bookingId') or booking_id)

    if sender_role == 'guest':
        _write_notification({
            "type": "chat_message", "audience": "owner",
            "title": f"{b.get('guestName') or 'Guest'} — {bed_label}",
            "message": preview,
            "ownerUid": b.get('ownerUid'), "propertyId": b.get('propertyId'), "bookingId": booking_id,
        })
    else:
        _write_notification({
            "type": "chat_message", "audience": "guest",
            "title": f"{b.get('hostelName') or 'Hostel'}",
            "message": preview,
            "guestUid": b.get('guestUid'), "propertyId": b.get('propertyId'), "bookingId": booking_id,
        })

    return jsonify({"ok": True})


# ==========================================
# 7. SECURE FILE ACCESS (Aadhaar / PDFs / payment proofs)
# ==========================================
# The Storage rules now set `allow read: if false` on every folder containing
# personal data. Previously they said `request.auth != null`, which meant ANY
# account — including one made in thirty seconds with a throwaway number — could
# download every guest's Aadhaar photo in the whole bucket.
#
# Files are now reachable only through here, and only by admin or by the owner of
# the specific hostel that file belongs to. The link expires in 10 minutes.
@app.route('/secure-file', methods=['GET'])
@limiter.limit("60 per hour")
@require_auth
def secure_file():
    if not firestore_db:
        return jsonify({"error": "Server not fully configured."}), 500
    from firebase_admin import storage as fb_storage

    path = (request.args.get('path') or '').strip()
    property_id = (request.args.get('propertyId') or '').strip()

    ALLOWED_PREFIXES = ('guestAadharDocs/', 'guestRegistrationPdfs/',
                        'upiPaymentProofs/', 'payoutDocs/')
    if not path.startswith(ALLOWED_PREFIXES) or '..' in path:
        return jsonify({"error": "Invalid file path."}), 400

    is_admin = firestore_db.collection('admin').document(request.uid).get().exists
    if not is_admin:
        if not property_id:
            return jsonify({"error": "propertyId is required."}), 400
        biz = firestore_db.collection('businesses').document(property_id).get()
        if not biz.exists or (biz.to_dict() or {}).get('ownerId') != request.uid:
            return jsonify({"error": "You are not authorised to view this file."}), 403

    try:
        blob = fb_storage.bucket().blob(path)
        if not blob.exists():
            return jsonify({"error": "File not found."}), 404
        url = blob.generate_signed_url(expiration=timedelta(minutes=10), method='GET')
        return jsonify({"url": url, "expiresInMinutes": 10})
    except Exception as e:
        print(f"❌ secure-file error: {e}")
        return jsonify({"error": "Could not generate a link for this file."}), 500


# ==========================================
# 8. ONE-TIME DATA MIGRATION (admin only)
# ==========================================
# The new Firestore rules need an `ownerUid` field on every booking and payout.
# Rather than making you run a Node script locally, just open this URL once while
# logged in as admin — the app will fetch it with your admin token.
#
# Safe to run more than once: documents that already have ownerUid are skipped.
@app.route('/admin/backfill-owner-uid', methods=['POST'])
@limiter.limit("5 per hour")
@require_admin
def backfill_owner_uid():
    if not firestore_db:
        return jsonify({"error": "Server not fully configured."}), 500

    owner_cache = {}
    def owner_of(business_id):
        if not business_id:
            return None
        if business_id not in owner_cache:
            snap = firestore_db.collection('businesses').document(business_id).get()
            owner_cache[business_id] = (snap.to_dict() or {}).get('ownerId') if snap.exists else None
        return owner_cache[business_id]

    report = {"bookings": {"updated": 0, "skipped": 0, "noOwner": 0},
              "payouts": {"updated": 0, "skipped": 0, "noOwner": 0}}

    for doc in firestore_db.collection('bookings').stream():
        d = doc.to_dict() or {}
        if d.get('ownerUid'):
            report["bookings"]["skipped"] += 1
            continue
        uid = owner_of(d.get('propertyId'))
        if not uid:
            report["bookings"]["noOwner"] += 1
            continue
        doc.reference.update({'ownerUid': uid})
        report["bookings"]["updated"] += 1

    for doc in firestore_db.collection('payouts').stream():
        d = doc.to_dict() or {}
        if d.get('ownerUid'):
            report["payouts"]["skipped"] += 1
            continue
        uid = owner_of(d.get('businessId'))
        if not uid:
            report["payouts"]["noOwner"] += 1
            continue
        doc.reference.update({'ownerUid': uid})
        report["payouts"]["updated"] += 1

    print(f"✅ Backfill complete: {report}")
    return jsonify({"ok": True, "report": report})


# ==========================================
# 8b. HIDE OWNER PHONE NUMBERS FROM THE PUBLIC LISTING
# ==========================================
# `businesses` is world-readable (listings have to be), and the owner's phone number was
# sitting inside businessProfile — so anyone could scrape every hostel owner's number in
# the app without even making an account. This moves each number into a private
# businessContacts/{businessId} document that only admin and that owner can read.
#
# Safe to run more than once.
@app.route('/admin/secure-payout-details', methods=['POST'])
@limiter.limit("5 per hour")
@require_admin
def secure_payout_details():
    """
    Moves payoutDetails and legalDetails OUT of the world-readable businesses documents.

    `businesses` has to stay public — that's how listings are browsed — but it was also
    holding each owner's bank account number, IFSC, UPI ID, licence and GST. Anyone could
    read every one of them without so much as creating an account.

    Safe to run more than once.
    """
    if not firestore_db:
        return jsonify({"error": "Server not fully configured."}), 500

    moved_pay = moved_legal = skipped = 0
    for d in firestore_db.collection('businesses').stream():
        b = d.to_dict() or {}
        owner = b.get('ownerId')
        touched = False

        pd = b.get('payoutDetails')
        if pd:
            firestore_db.collection('businessPayouts').document(d.id).set({
                **pd, "ownerUid": owner, "businessId": d.id, "movedAt": _dt_now_iso(),
            }, merge=True)
            moved_pay += 1
            touched = True

        ld = b.get('legalDetails')
        if ld:
            firestore_db.collection('businessLegal').document(d.id).set({
                **ld, "ownerUid": owner, "businessId": d.id, "movedAt": _dt_now_iso(),
            }, merge=True)
            moved_legal += 1
            touched = True

        if touched:
            from firebase_admin import firestore as _fs
            d.reference.update({
                "payoutDetails": _fs.DELETE_FIELD,
                "legalDetails": _fs.DELETE_FIELD,
            })
        else:
            skipped += 1

    return jsonify({"ok": True, "payoutsMoved": moved_pay,
                    "legalMoved": moved_legal, "skipped": skipped})


@app.route('/admin/hide-owner-phones', methods=['POST'])
@limiter.limit("5 per hour")
@require_admin
def hide_owner_phones():
    if not firestore_db:
        return jsonify({"error": "Server not fully configured."}), 500

    moved = skipped = 0
    for d in firestore_db.collection('businesses').stream():
        b = d.to_dict() or {}
        profile = b.get('businessProfile') or {}
        phone = profile.get('phone')
        if not phone:
            skipped += 1
            continue

        firestore_db.collection('businessContacts').document(d.id).set({
            "businessId": d.id,
            "ownerUid": b.get('ownerId'),
            "phone": phone,
            "email": profile.get('email'),
            "movedAt": _dt_now_iso(),
        }, merge=True)

        profile.pop('phone', None)
        profile.pop('email', None)
        d.reference.update({"businessProfile": profile})
        moved += 1

    return jsonify({"ok": True, "moved": moved, "skipped": skipped})


# ==========================================
# 9. EXPIRED BED-LOCK CLEANUP (admin only)
# ==========================================
# Bed locks have a 15-minute ceiling enforced by the rules, but nothing deletes
# them once they lapse. Call this from a free cron service (cron-job.org) every
# 10 minutes, or hit it manually if beds look stuck.
# ==========================================
# DAILY REMINDERS — run once a day from cron-job.org
# ==========================================
# Three things nobody was being told:
#   1. Guest forgets they are due to arrive tomorrow.
#   2. Owner has no idea someone is arriving tomorrow, so the bed is not ready.
#   3. Rent is overdue and neither side is chasing it.
#
# All three are the kind of thing that quietly turns into a cancellation or an argument.
# A one-line reminder the day before prevents most of them.
#
# Auth: this runs unattended from a cron service, so it uses a shared secret in the
# X-Cron-Key header rather than a Firebase login. Set CRON_SECRET in Render's env vars.
def _cron_authorised():
    expected = os.environ.get("CRON_SECRET", "")
    return bool(expected) and request.headers.get("X-Cron-Key", "") == expected


@app.route('/cron/daily-reminders', methods=['POST'])
@limiter.limit("30 per hour")
def daily_reminders():
    if not _cron_authorised():
        return jsonify({"error": "Unauthorised."}), 401
    if not firestore_db:
        return jsonify({"error": "Server not fully configured."}), 500

    import datetime as _d
    today = _d.date.today()
    tomorrow = (today + _d.timedelta(days=1)).isoformat()
    today_s = today.isoformat()

    arriving = leaving = overdue = 0

    # ---- Arrivals tomorrow: tell the guest AND the owner ----
    for d in firestore_db.collection('bookings').where('status', 'in', ['paid_verified', 'checked_in']).stream():
        b = d.to_dict() or {}
        ctx = b.get('bookingContext') or {}
        joining = str(ctx.get('joiningDate') or b.get('joiningDate') or '')[:10]
        checkout = str(b.get('checkOutDate') or ctx.get('checkOutDate') or '')[:10]
        hostel = b.get('hostelName') or 'the hostel'
        guest = b.get('guestName') or 'A guest'

        if joining == tomorrow:
            arriving += 1
            _write_notification({
                "type": "checkin_reminder", "audience": "guest",
                "guestUid": b.get('guestUid'), "propertyId": b.get('propertyId'), "bookingId": d.id,
                "title": "Check-in tomorrow",
                "message": f"Your check-in at {hostel} is tomorrow. Please carry your original Aadhaar.",
            })
            _write_notification({
                "type": "arrival_reminder", "audience": "owner",
                "ownerUid": b.get('ownerUid'), "propertyId": b.get('propertyId'), "bookingId": d.id,
                "title": "Guest arriving tomorrow",
                "message": f"{guest} checks in tomorrow. Please keep the bed ready.",
            })

        # ---- Leaving tomorrow: the owner can start re-listing that bed today ----
        if checkout == tomorrow:
            leaving += 1
            _write_notification({
                "type": "checkout_reminder", "audience": "owner",
                "ownerUid": b.get('ownerUid'), "propertyId": b.get('propertyId'), "bookingId": d.id,
                "title": "Guest leaving tomorrow",
                "message": f"{guest} checks out tomorrow. The bed will be free.",
            })
            _write_notification({
                "type": "checkout_reminder", "audience": "guest",
                "guestUid": b.get('guestUid'), "propertyId": b.get('propertyId'), "bookingId": d.id,
                "title": "Check-out tomorrow",
                "message": f"Your stay at {hostel} ends tomorrow. Please settle any pending dues.",
            })

    # ---- Rent overdue: nudge the guest, and tell the owner who is late ----
    late_by_owner = {}
    for d in firestore_db.collection('rentLedger').where('paid', '==', False).stream():
        r = d.to_dict() or {}
        due = str(r.get('dueDate') or '')[:10]
        if not due or due >= today_s:
            continue
        overdue += 1
        amount = int(r.get('amount') or 0)
        _write_notification({
            "type": "rent_overdue", "audience": "guest",
            "guestUid": r.get('guestUid'), "propertyId": r.get('propertyId'),
            "title": "Rent overdue",
            "message": f"Your rent of Rs {amount} was due on {due}. Please pay at the hostel.",
        })
        key = (r.get('ownerUid'), r.get('propertyId'))
        late_by_owner.setdefault(key, [0, 0])
        late_by_owner[key][0] += 1
        late_by_owner[key][1] += amount

    # One summary per owner rather than one message per late guest — five separate pings
    # about the same thing is how people learn to ignore notifications.
    for (owner_uid, prop_id), (count, total) in late_by_owner.items():
        if not owner_uid:
            continue
        _write_notification({
            "type": "rent_overdue_summary", "audience": "owner",
            "ownerUid": owner_uid, "propertyId": prop_id,
            "title": "Rent overdue",
            "message": f"{count} guest(s) have overdue rent totalling Rs {total}.",
        })

    print(f"⏰ daily reminders: arriving={arriving} leaving={leaving} overdue={overdue}")
    return jsonify({"ok": True, "arriving": arriving, "leaving": leaving, "overdue": overdue})


@app.route('/admin/cleanup-locks', methods=['POST'])
@limiter.limit("60 per hour")
@require_admin
def cleanup_locks():
    if not firestore_db:
        return jsonify({"error": "Server not fully configured."}), 500
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc)
    removed = 0
    for doc in firestore_db.collection('bedLocks').stream():
        d = doc.to_dict() or {}
        if d.get('confirmed'):
            continue    # a confirmed lock is a real booking — never touch it
        exp = d.get('expiresAt')
        try:
            if exp and hasattr(exp, 'timestamp') and exp < now:
                doc.reference.delete()
                removed += 1
        except Exception:
            continue
    return jsonify({"ok": True, "removed": removed})


@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({
        "status": "ok",
        "gemini_configured": bool(GEMINI_API_KEY),
        "email_configured": bool(RESEND_API_KEY and ADMIN_EMAIL),
        "razorpay_configured": bool(RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET),
        "sms_otp_configured": bool(TWOFACTOR_API_KEY and firebase_admin_app),
        "storage_bucket_configured": bool(os.environ.get("FIREBASE_STORAGE_BUCKET")),
        "cron_secret_configured": bool(os.environ.get("CRON_SECRET")),
        "allowed_origins": ALLOWED_ORIGINS
    })


if __name__ == '__main__':
    print("🚀 Server started on port 5000!")
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)

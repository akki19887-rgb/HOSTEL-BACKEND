from flask import Flask, request, jsonify
from flask_cors import CORS
from google import genai
import json
import os
import hmac
import hashlib
import requests
from PIL import Image

app = Flask(__name__)
CORS(app)

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

# FIREBASE ADMIN SDK — needed only for the custom (Resend-branded) email verification link below.
# Firebase Console → Project Settings → Service Accounts → "Generate new private key" downloads a
# JSON file. Paste its ENTIRE content as the value of a FIREBASE_SERVICE_ACCOUNT_JSON env var
# (same way as GEMINI_API_KEY above) — do not commit the JSON file itself to git.
FIREBASE_SERVICE_ACCOUNT_JSON = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON")
firebase_admin_app = None
if FIREBASE_SERVICE_ACCOUNT_JSON:
    try:
        import firebase_admin
        from firebase_admin import credentials as fb_credentials, auth as fb_auth
        cred = fb_credentials.Certificate(json.loads(FIREBASE_SERVICE_ACCOUNT_JSON))
        firebase_admin_app = firebase_admin.initialize_app(cred)
        print("✅ Firebase Admin SDK initialized.")
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
           these four shapes it forms:
           - "straight": ONE single corridor running along one edge or through the middle, with rooms
             on at most two sides of it (this is the common case).
           - "L": the corridor bends once, 90 degrees, like the letter L.
           - "T": the corridor forms a T-junction — one corridor meets another at a midpoint, forming 3 arms.
           - "cross": the corridor forms a "+" junction — two corridors crossing, forming 4 arms.

        2. CORRIDOR POSITION (only when corridor_shape is "straight"): Choose EXACTLY ONE of these
           values: "left", "right", "top", "bottom", or "center".
           (e.g., If rooms are on the right and the empty path is on the left, choose "left". If rooms
           are on both sides of the path, choose "center".)
           If corridor_shape is "L", "T", or "cross", set corridor_position to "center" (it's ignored
           in that case — quadrant assignment below is what actually matters).

        3. ROOM QUADRANT (only when corridor_shape is "L", "T", or "cross"): For each room, imagine the
           floor plan as a map and determine which quadrant it sits in relative to the corridor
           junction: "NW" (top-left), "NE" (top-right), "SW" (bottom-left), or "SE" (bottom-right).
           Leave this field out entirely (or null) when corridor_shape is "straight".

        4. ROOMS: Identify all rooms, logical numbers (e.g., 1, 2, 3), and count the beds inside each
           (default to 1 if unclear).
        5. AMENITIES: Identify if 'Bathroom' or 'Study Table' are written inside a room, as their own
           small boxes/icons (not a bed).
        6. DOORS: True if doors/openings are marked.

        Return ONLY a valid JSON object matching this structure EXACTLY (No markdown, no extra text):
        {
          "corridor_detected": true,
          "corridor_shape": "straight",
          "corridor_position": "left",
          "rooms": [
            {"room_no": "1", "beds_count": 1, "amenities": ["study_table", "bathroom"], "has_door": true, "quadrant": null},
            {"room_no": "2", "beds_count": 2, "amenities": [], "has_door": true, "quadrant": null}
          ]
        }

        Example for an "L" or "T" or "cross" shaped corridor, each room MUST include its quadrant:
        {
          "corridor_detected": true,
          "corridor_shape": "cross",
          "corridor_position": "center",
          "rooms": [
            {"room_no": "1", "beds_count": 1, "amenities": [], "has_door": true, "quadrant": "NE"},
            {"room_no": "5", "beds_count": 2, "amenities": ["bathroom"], "has_door": true, "quadrant": "NW"}
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
def extract_aadhar():
    print("\n" + "="*50)
    print("🟢 START: Aadhar extraction started!")

    if 'aadhar_photo' not in request.files:
        return jsonify({"error": "No image uploaded"}), 400

    file = request.files['aadhar_photo']

    try:
        img = Image.open(file)

        prompt = """Look at this photo of an Indian Aadhar card and extract the printed details EXACTLY
        as written on the card. Do not guess or invent anything — if a field is not clearly visible,
        leave it as an empty string "".

        Fields to extract:
        1. full_name — the person's name as printed.
        2. dob — date of birth, converted to YYYY-MM-DD format (Aadhar usually shows DD/MM/YYYY).
        3. gender — exactly "Male", "Female", or "Other" (Aadhar prints "M" or "F" or similar — convert it).
        4. aadhar_number — the 12-digit number, digits only, no spaces.
        5. address — the full address block printed on the back of the card, as one single line
           (join multi-line address with ", ").
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
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=[prompt, img]
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
                "from": "HostelOM <onboarding@resend.dev>",  # change to your verified domain once set up
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
# 2B. CUSTOM EMAIL VERIFICATION (sent via Resend instead of Firebase's default sender —
# better deliverability, less likely to land in spam, and matches HostelOM's branding)
# ==========================================
@app.route('/send-verification-email', methods=['POST'])
def send_verification_email():
    if not firebase_admin_app:
        return jsonify({"error": "Firebase Admin SDK not configured on server. Set FIREBASE_SERVICE_ACCOUNT_JSON env var."}), 500
    if not RESEND_API_KEY:
        return jsonify({"error": "Email not configured on server. Set RESEND_API_KEY env var."}), 500

    data = request.get_json(silent=True) or {}
    email = data.get('email', '').strip()
    if not email:
        return jsonify({"error": "email is required"}), 400

    try:
        # Firebase generates the actual secure verification link (same one it would've emailed
        # itself) — we just take over how it gets delivered.
        verify_link = fb_auth.generate_email_verification_link(email)

        resp = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
            json={
                "from": "HostelOM <onboarding@resend.dev>",  # change to your verified domain once set up
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
def razorpay_create_order():
    if not RAZORPAY_KEY_ID or not RAZORPAY_KEY_SECRET:
        return jsonify({"error": "Razorpay not configured. Set RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET env vars."}), 500

    data = request.get_json(silent=True) or {}
    amount_paise = int(data.get('amount', 0))      # already in paise from frontend
    currency = data.get('currency', 'INR')
    notes = data.get('notes', {}) or {}

    if amount_paise <= 0:
        return jsonify({"error": "Amount must be greater than zero."}), 400
    # Sanity cap: max ₹5,00,000 per single order (prevents accidental huge charges)
    if amount_paise > 50000000:
        return jsonify({"error": "Amount exceeds allowed limit."}), 400

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
            "currency": order.get("currency")
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
@app.route('/razorpay/verify', methods=['POST'])
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

    # ---- Signature verified. Payment is genuine. ----
    # Generate a human-readable booking ID. In production, save the full booking record
    # to your DB here (Firestore, Postgres, etc.) with status='paid_verified' so the
    # admin dashboard picks it up.
    booking_context = data.get('bookingContext', {}) or {}
    guest = data.get('guest', {}) or {}
    booking_id = "BK-" + payment_id[-8:].upper()

    print(f"✅ Payment verified · order={order_id} · payment={payment_id} · booking={booking_id}")
    # Optional: fire-and-forget email notification to admin
    try:
        if RESEND_API_KEY and ADMIN_EMAIL:
            requests.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
                json={
                    "from": "HostelOM <onboarding@resend.dev>",
                    "to": [ADMIN_EMAIL],
                    "subject": f"✅ Payment Received — {guest.get('fullName', 'Guest')} ({booking_context.get('hostelName', '')})",
                    "html": f"<p>Booking <b>{booking_id}</b> paid successfully.</p>"
                            f"<p>Payment ID: <code>{payment_id}</code><br>"
                            f"Order ID: <code>{order_id}</code></p>"
                },
                timeout=8
            )
    except Exception as mailErr:
        print(f"⚠️ Admin email notification failed: {mailErr}")

    return jsonify({
        "ok": True,
        "bookingId": booking_id,
        "paymentId": payment_id,
        "orderId": order_id
    })


@app.errorhandler(Exception)
def handle_unexpected_error(e):
    print(f"❌ UNEXPECTED ERROR: {e}")
    return jsonify({"error": "Something went wrong on the server. Please try again."}), 500


@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({
        "status": "ok",
        "gemini_configured": bool(GEMINI_API_KEY),
        "email_configured": bool(RESEND_API_KEY and ADMIN_EMAIL),
        "razorpay_configured": bool(RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET)
    })


if __name__ == '__main__':
    print("🚀 Server started on port 5000!")
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)

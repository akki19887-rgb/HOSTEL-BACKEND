from flask import Flask, request, jsonify
from flask_cors import CORS
from google import genai
import json
import os
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
        1. CORRIDOR POSITION (CRITICAL): Identify the main empty walking space/hallway. Determine its placement relative to the rooms. 
           Choose EXACTLY ONE of these values: "left", "right", "top", "bottom", or "center". 
           (e.g., If rooms are on the right and the empty path is on the left, choose "left". If rooms are on both sides of the path, choose "center").
        2. ROOMS: Identify all rooms, logical numbers (e.g., 1, 2, 3), and count the beds inside each (default to 1 if unclear).
        3. AMENITIES: Identify if 'Bathroom' or 'Study Table' are written inside.
        4. DOORS: True if doors/openings are marked.

        Return ONLY a valid JSON object matching this structure EXACTLY (No markdown, no extra text):
        {
          "corridor_detected": true,
          "corridor_position": "left",
          "rooms": [
            {"room_no": "1", "beds_count": 1, "amenities": ["study_table", "bathroom"], "has_door": true},
            {"room_no": "2", "beds_count": 2, "amenities": [], "has_door": true}
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

        print(f"✅ DONE: Corridor detected at: {parsed_json.get('corridor_position', 'unknown')}")
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


@app.errorhandler(Exception)
def handle_unexpected_error(e):
    print(f"❌ UNEXPECTED ERROR: {e}")
    return jsonify({"error": "Something went wrong on the server. Please try again."}), 500


@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({"status": "ok", "gemini_configured": bool(GEMINI_API_KEY), "email_configured": bool(RESEND_API_KEY and ADMIN_EMAIL)})


if __name__ == '__main__':
    print("🚀 Server started on port 5000!")
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)

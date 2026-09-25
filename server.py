# server.py - Smart E-Waste Dustbin Backend (Enhanced) Diyloyable version with real-time dashboard and premium certificate generation

from flask import Flask, request, jsonify, render_template_string, Response, stream_with_context
from flask_cors import CORS
import requests
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from PIL import Image, ImageDraw, ImageFont, ImageFilter
import io
import base64
import qrcode
import os
import json
import math
import queue
import threading
import time
from datetime import datetime
import random
from reportlab.lib.utils import ImageReader

app = Flask(__name__)
CORS(app)

# ==================== CONFIGURATION ====================
ESP32_CAM_IP      = "10.175.166.73"
ESP32_TRIGGER_URL = f"http://{ESP32_CAM_IP}/trigger"

SMTP_SERVER    = "smtp.gmail.com"
SMTP_PORT      = 587
SENDER_EMAIL   = "eexplorations12@gmail.com"
SENDER_PASSWORD = ""

OUTPUT_DIR = "certificates"
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ==================== REAL-TIME STATE ====================
pending_requests = {}

# Global system status (updated by ESP32 or server logic)
system_status = {
    "lid":           "closed",       # "open" | "closed"
    "last_event":    "System ready", # human-readable last event
    "last_user":     None,           # name of last/current user
    "last_updated":  datetime.now().strftime("%H:%M:%S"),
    "total_today":   0,              # recycling count for today
    "esp32_online":  False,
    "processing":    False,
}

# SSE subscriber queues  (one queue per connected browser tab)
sse_subscribers = []   # list of queue.Queue  (compatible with Python 3.8)
sse_lock = threading.Lock()

def push_event(event_type: str, data: dict):
    """Push an SSE event to all connected browser clients."""
    system_status["last_updated"] = datetime.now().strftime("%H:%M:%S")
    payload = json.dumps({"type": event_type, "status": system_status, **data})
    with sse_lock:
        dead = []
        for q in sse_subscribers:
            try:
                q.put_nowait(payload)
            except queue.Full:
                dead.append(q)
        for q in dead:
            sse_subscribers.remove(q)

# ==================== SSE STREAM ENDPOINT ====================

@app.route('/api/stream')
def stream():
    """Server-Sent Events endpoint — browser subscribes here for live updates."""
    client_q: queue.Queue = queue.Queue(maxsize=30)
    with sse_lock:
        sse_subscribers.append(client_q)

    # Send current state immediately on connect
    client_q.put_nowait(json.dumps({"type": "init", "status": system_status}))

    def generate():
        try:
            while True:
                try:
                    msg = client_q.get(timeout=25)
                    yield f"data: {msg}\n\n"
                except queue.Empty:
                    yield ": heartbeat\n\n"   # keep-alive ping
        except GeneratorExit:
            pass
        finally:
            with sse_lock:
                if client_q in sse_subscribers:
                    sse_subscribers.remove(client_q)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control":   "no-cache",
            "X-Accel-Buffering": "no",
        }
    )

# ==================== ESP32 LID-STATUS ENDPOINT ====================

@app.route('/api/lid-status', methods=['POST'])
def lid_status():
    """
    ESP32 calls this whenever the lid opens or closes.
    Body: {"status": "open"|"closed", "request_id": "..."}
    """
    data       = request.json or {}
    status     = data.get("status", "closed")
    request_id = data.get("request_id", "")

    system_status["lid"]         = status
    system_status["esp32_online"] = True

    if status == "open":
        system_status["last_event"] = "Lid opened — waiting for e-waste deposit"
        push_event("lid_open",  {"request_id": request_id})
    else:
        system_status["last_event"] = "Lid closed — processing complete"
        push_event("lid_closed", {"request_id": request_id})

    return jsonify({"success": True})

# ==================== ROUTES ====================

@app.route('/')
def index():
    return render_template_string(WEB_FORM_HTML)

@app.route('/api/submit', methods=['POST'])
def submit_details():
    data = request.json
    name       = data.get('name')
    mobile     = data.get('mobile')
    email      = data.get('email')
    request_id = data.get('request_id')

    if not all([name, mobile, email, request_id]):
        return jsonify({"error": "Missing required fields"}), 400

    pending_requests[request_id] = {
        "name":           name,
        "mobile":         mobile,
        "email":          email,
        "timestamp":      datetime.now(),
        "photo_received": False,
        "photo_path":     None,
    }

    system_status["last_user"]  = name
    system_status["processing"] = True
    system_status["last_event"] = f"New submission from {name} — triggering camera"
    push_event("submission", {"name": name, "request_id": request_id})

    # Trigger ESP32-CAM
    esp_status = False
    try:
        r = requests.get(ESP32_TRIGGER_URL,
                         params={"request_id": request_id},
                         timeout=10)
        esp_status = r.status_code == 200
        system_status["esp32_online"] = esp_status
        if esp_status:
            system_status["last_event"] = f"ESP32 triggered for {name} — opening lid"
            push_event("esp32_triggered", {"name": name})
    except Exception as e:
        print(f"ESP32 trigger failed: {e}")
        system_status["esp32_online"] = False
        push_event("esp32_offline", {"error": str(e)})

    def process_after_timeout(req_id):
        time.sleep(30)
        if req_id in pending_requests and not pending_requests[req_id]['photo_received']:
            system_status["last_event"] = "Timeout — generating certificate without photo"
            push_event("timeout", {"request_id": req_id})
            generate_and_send(req_id, use_placeholder=True)

    threading.Thread(target=process_after_timeout, args=(request_id,), daemon=True).start()

    return jsonify({
        "success":       True,
        "message":       "Details received. Please stand in front of camera.",
        "esp32_triggered": esp_status,
        "request_id":    request_id,
    })

@app.route('/api/upload-photo', methods=['POST'])
def upload_photo():
    request_id = request.form.get('request_id')

    if 'photo' not in request.files:
        return jsonify({"error": "No photo uploaded"}), 400
    if request_id not in pending_requests:
        return jsonify({"error": "Invalid or expired request"}), 400

    photo      = request.files['photo']
    photo_path = os.path.join(OUTPUT_DIR, f"{request_id}_photo.jpg")
    photo.save(photo_path)

    pending_requests[request_id]['photo_received'] = True
    pending_requests[request_id]['photo_path']     = photo_path

    system_status["last_event"] = "Photo received — generating certificate"
    push_event("photo_received", {"request_id": request_id})

    threading.Thread(target=generate_and_send, args=(request_id,), daemon=True).start()

    return jsonify({"success": True, "message": "Photo received, processing certificate..."})

@app.route('/api/test-upload', methods=['POST'])
def test_upload_file():
    request_id = request.form.get('request_id')

    if 'photo' not in request.files:
        return jsonify({"error": "No photo file provided"}), 400
    if request_id not in pending_requests:
        return jsonify({"error": "Invalid or expired request"}), 400

    photo      = request.files['photo']
    photo_path = os.path.join(OUTPUT_DIR, f"{request_id}_photo.jpg")
    photo.save(photo_path)

    pending_requests[request_id]['photo_received'] = True
    pending_requests[request_id]['photo_path']     = photo_path

    system_status["last_event"] = "Webcam photo received — generating certificate"
    push_event("photo_received", {"request_id": request_id})

    threading.Thread(target=generate_and_send, args=(request_id,), daemon=True).start()

    return jsonify({"success": True, "message": "Test photo uploaded, processing certificate..."})

# ==================== CERTIFICATE GENERATION ====================
def generate_certificate(name, mobile, email, photo_path, request_id):

    import os
    import random
    import qrcode

    from datetime import datetime

    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import landscape, A4
    from reportlab.lib.colors import HexColor

    # =====================================================
    # USER DATA
    # =====================================================

    waste_category = "E-Waste"

    location = "Smart Eco E Waste Dustbin Facility"

    civic_score = random.randint(95, 100)

    certificate_no = f"ECO-{random.randint(1000,9999)}"

    now = datetime.now()

    date_text = now.strftime("%d %B %Y")
    time_text = now.strftime("%I:%M %p")

    # =====================================================
    # QR DATA
    # =====================================================

    qr_text = f"""
SMART ECO CERTIFICATE

Name: {name}
Category: {waste_category}
Location: {location}
Date: {date_text}
Time: {time_text}
Civic Score: {civic_score}
Certificate No: {certificate_no}

Status: VERIFIED
"""

    # =====================================================
    # GENERATE QR
    # =====================================================

    qr = qrcode.QRCode(
        version=1,
        box_size=8,
        border=2
    )

    qr.add_data(qr_text)

    qr.make(fit=True)

    img = qr.make_image(
        fill_color="black",
        back_color="white"
    )

    qr_path = os.path.join(
        OUTPUT_DIR,
        f"{request_id}_qr.png"
    )

    img.save(qr_path)

    # =====================================================
    # OUTPUT PDF PATH
    # =====================================================

    output_pdf = os.path.join(
        OUTPUT_DIR,
        f"certificate_{request_id}.pdf"
    )

    # =====================================================
    # PDF SETUP
    # =====================================================

    c = canvas.Canvas(
        output_pdf,
        pagesize=landscape(A4)
    )

    width, height = landscape(A4)

    # =====================================================
    # DARK GREEN BACKGROUND
    # =====================================================

    c.setFillColor(HexColor("#0B1F16"))

    c.rect(0, 0, width, height, fill=1)

    # =====================================================
    # TOP STRIPS
    # =====================================================

    c.setFillColor(HexColor("#163D2B"))

    c.rect(
        0,
        height-80,
        width,
        80,
        fill=1,
        stroke=0
    )

    c.setFillColor(HexColor("#10281D"))

    c.rect(
        0,
        0,
        width,
        55,
        fill=1,
        stroke=0
    )

    # =====================================================
    # OUTER BORDER
    # =====================================================

    c.setStrokeColor(HexColor("#4CAF50"))

    c.setLineWidth(5)

    c.roundRect(
        25,
        25,
        width-50,
        height-50,
        20
    )

    # =====================================================
    # INNER BORDER
    # =====================================================

    c.setStrokeColor(HexColor("#81C784"))

    c.setLineWidth(1.5)

    c.roundRect(
        40,
        40,
        width-80,
        height-80,
        15
    )

    # =====================================================
    # HEADER
    # =====================================================

    c.setFont("Helvetica-Bold", 15)

    c.setFillColor(HexColor("#A5D6A7"))

    c.drawCentredString(
        width/2,
        height-60,
        "SMART E WASTE MANAGEMENT • SUSTAINABILITY RECOGNITION CERTIFICATE"
    )

    # =====================================================
    # MAIN TITLE
    # =====================================================

    c.setFont("Helvetica-Bold", 34)

    c.setFillColor(HexColor("#F6D607"))

    c.drawCentredString(
        width/2,
        height-120,
        "CERTIFICATE"
    )

    # =====================================================
    # SUBTITLE
    # =====================================================

    c.setFont("Helvetica", 18)

    c.setFillColor(HexColor("#C8E6C9"))

    c.drawCentredString(
        width/2,
        height-155,
        "OF ECO-RESPONSIBILITY"
    )

    # =====================================================
    # PRESENTED TEXT
    # =====================================================

    c.setFont("Helvetica", 19)

    c.setFillColor(HexColor("#E8F5E9"))

    c.drawCentredString(
        width/2,
        height-225,
        "This certifies that"
    )

    # =====================================================
    # NAME
    # =====================================================

    c.setFont("Helvetica-Bold", 33)

    c.setFillColor(HexColor("#76FF03"))

    c.drawCentredString(
        width/2,
        height-285,
        name.upper()
    )

    text_width = c.stringWidth(
        name.upper(),
        "Helvetica-Bold",
        33
    )

    c.setStrokeColor(HexColor("#76FF03"))

    c.line(
        (width-text_width)/2,
        height-295,
        (width+text_width)/2,
        height-295
    )

    # =====================================================
    # DESCRIPTION
    # =====================================================

    c.setFont("Helvetica", 17)

    c.setFillColor(HexColor("#F6D607"))

    desc = "has responsibly deposited waste at our Smart Eco-Dustbin facility"

    c.drawCentredString(
        width/2,
        height-320,
        desc
    )

    # =====================================================
    # DETAILS BOX
    # =====================================================

    box_x = 110
    box_y =100
    box_w = width - 220
    box_h = 160

    c.setFillColor(HexColor("#163D2B"))

    c.roundRect(
        box_x,
        box_y,
        box_w,
        box_h,
        15,
        fill=1,
        stroke=0
    )

    # =====================================================
# DETAILS TEXT
# =====================================================

    c.setFont("Helvetica-Bold", 14)

    c.setFillColor(HexColor("#FFFFFF"))

    line_y = box_y + 140

     # LEFT SIDE

    c.drawString(
    box_x + 25,
    line_y,
    f"Waste Category: {waste_category}"
    )

    c.drawString(
    box_x + 25,
    line_y - 30,
    f"Deposited At: {location}"
    )

    c.drawString(
    box_x + 25,
    line_y - 60,
    f"Date & Time: {date_text} • {time_text}"
   )

    c.drawString(
    box_x + 25,
    line_y - 90,
    f"Mobile: {mobile}"
    )

    # RIGHT SIDE

    c.drawRightString(
    box_x + box_w - 25,
    line_y,
    f"Civic Score: {civic_score}"
    )

    c.drawRightString(
    box_x + box_w - 25,
    line_y - 30,
    f"Certificate No: {certificate_no}"
    )

    c.drawRightString(
    box_x + box_w - 25,
    line_y - 60,
    "Status: E-Waste Verified"
    )

    c.drawRightString(
    box_x + box_w - 25,
    line_y - 90,
    f"Email: {email}"
   ) 
    

    # =====================================================
    # QR CODE
    # =====================================================

    c.drawImage(
        qr_path,
        width - 190,
        height - 290,
        width=110,
        height=110
    )

   # =====================================================
       # USER PHOTO
   # =====================================================

    if photo_path and os.path.exists(photo_path):

      c.drawImage(
        photo_path,
        70,
        height - 280,
        width=100,
        height=100,
        mask='auto'
    )

    # =====================================================
    # FOOTER
    # =====================================================

    footer = (
        "Powered by Smart E-Waste Management System • "
        "Generated with Hope to Inspire Responsible Waste Disposal"
    )

    c.setFont("Helvetica-Oblique", 10)

    c.setFillColor(HexColor("#A5D6A7"))

    c.drawCentredString(
        width/2,
        70,
        footer
    )

    # =====================================================
    # SAVE PDF
    # =====================================================

    c.save()

    return output_pdf
# ==================== EMAIL ====================

def send_certificate_email(recipient_email, name, certificate_path):
    msg             = MIMEMultipart()
    msg['From']     = SENDER_EMAIL
    msg['To']       = recipient_email
    msg['Subject']  = " Your E-Waste Contribution Certificate — Smart Dustbin System"

    cert_id = os.path.basename(certificate_path).replace("certificate_", "").replace(".png", "")[:8].upper()
    body = f"""Dear {name},

Thank you for contributing to a cleaner and greener environment by using our Smart E-Waste Collection System.

Your official Certificate of Eco-Recycling is attached to this email.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Certificate Details
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Contributor  : {name}
  Date         : {datetime.now().strftime("%B %d, %Y")}
  Certificate  : E - Waste 
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

By recycling your electronic waste responsibly, you have:
  • Prevented hazardous materials from polluting soil and water
  • Helped recover valuable raw materials
  • Supported a circular economy for electronics

We appreciate your commitment to a sustainable future.

Warm regards,
Smart E-Waste Management Team
www.smartewaste.com  |  support@smartewaste.com
"""

    msg.attach(MIMEText(body, 'plain'))

    with open(certificate_path, 'rb') as f:
        att = MIMEBase('application', 'octet-stream')
        att.set_payload(f.read())
        encoders.encode_base64(att)
        att.add_header('Content-Disposition',
                       f'attachment; filename=E-Waste_Certificate_{name.replace(" ", "_")}.pdf')
        msg.attach(att)

    try:
        srv = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        srv.starttls()
        srv.login(SENDER_EMAIL, SENDER_PASSWORD)
        srv.send_message(msg)
        srv.quit()
        print(f" Email sent to {recipient_email}")
        return True
    except Exception as e:
        print(f" Email failed: {e}")
        return False

# ==================== MAIN PROCESSING ====================

def generate_and_send(request_id, use_placeholder=False):
    req = pending_requests.pop(request_id, None)
    if req is None:
        return

    system_status["last_event"] = f"Generating certificate for {req['name']}"
    push_event("generating", {"name": req["name"]})

    photo_path = req.get('photo_path') if not use_placeholder else None
    cert_path  = generate_certificate(
        req['name'], req['mobile'], req['email'], photo_path, request_id
    )

    system_status["last_event"] = f"Sending certificate email to {req['email']}"
    push_event("sending_email", {"email": req["email"]})

    email_sent = send_certificate_email(req['email'], req['name'], cert_path)

    system_status["processing"]  = False
    system_status["total_today"] = system_status["total_today"] + 1

    if email_sent:
        system_status["last_event"] = f"Certificate emailed to {req['name']} ✓"
        push_event("done", {"name": req["name"], "email": req["email"]})
    else:
        system_status["last_event"] = f"Certificate generated — email failed for {req['name']}"
        push_event("email_failed", {"name": req["name"]})

    print(f" Done for {req['name']} — Email: {email_sent}")

# ==================== WEB FORM HTML ====================

WEB_FORM_HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Smart E-Waste Collection System</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@300;400;500;600&family=Playfair+Display:wght@600;700&display=swap" rel="stylesheet">
<style>
:root {
  --forest:  #0D3B2E;
  --green:   #1A5C42;
  --mid:     #2D8A62;
  --leaf:    #4DB87A;
  --cream:   #F7F3EC;
  --amber:   #D4680A;
  --gold:    #C9A84C;
  --ink:     #1C1C1C;
  --gray:    #6B7280;
  --lgray:   #E5E7EB;
  --white:   #FFFFFF;
  --red:     #DC2626;
}
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

body {
  font-family: 'DM Sans', sans-serif;
  background: var(--forest);
  min-height: 100vh;
  display: grid;
  grid-template-columns: 1fr 420px;
  grid-template-rows: 1fr;
  gap: 0;
}

/* ── LEFT: Live Dashboard ─────────────────── */
.dashboard {
  background: linear-gradient(160deg, #0a2e22 0%, #0D3B2E 60%, #112e24 100%);
  padding: 48px 44px;
  display: flex;
  flex-direction: column;
  gap: 28px;
  overflow-y: auto;
}

.brand {
  display: flex;
  align-items: center;
  gap: 16px;
  margin-bottom: 8px;
}
.brand-icon {
  width: 52px; height: 52px;
  background: var(--leaf);
  border-radius: 14px;
  display: flex; align-items: center; justify-content: center;
  font-size: 26px;
  flex-shrink: 0;
}
.brand-text h1 {
  font-family: 'Playfair Display', serif;
  font-size: 22px;
  color: var(--white);
  line-height: 1.2;
}
.brand-text p { font-size: 13px; color: rgba(255,255,255,.45); margin-top: 3px; }

/* Lid status hero */
.lid-hero {
  background: rgba(255,255,255,.05);
  border: 1px solid rgba(255,255,255,.08);
  border-radius: 20px;
  padding: 32px 24px;
  text-align: center;
  position: relative;
  overflow: hidden;
}
.lid-hero::before {
  content: '';
  position: absolute; inset: 0;
  background: radial-gradient(ellipse at 50% 0%, rgba(77,184,122,.12) 0%, transparent 70%);
}
.lid-icon-wrap {
  position: relative;
  width: 90px; height: 90px;
  margin: 0 auto 20px;
}
.lid-icon-bg {
  width: 90px; height: 90px;
  border-radius: 50%;
  background: rgba(77,184,122,.15);
  border: 2px solid rgba(77,184,122,.3);
  display: flex; align-items: center; justify-content: center;
  font-size: 38px;
  transition: all .5s ease;
}
.lid-icon-bg.open {
  background: rgba(77,184,122,.25);
  border-color: var(--leaf);
  box-shadow: 0 0 0 8px rgba(77,184,122,.1), 0 0 0 16px rgba(77,184,122,.05);
}
.lid-icon-bg.open .lid-emoji { animation: bounce .6s ease; }
@keyframes bounce {
  0%,100% { transform: translateY(0); }
  40%      { transform: translateY(-8px); }
}
.lid-pulse {
  position: absolute; inset: -8px;
  border-radius: 50%;
  border: 2px solid var(--leaf);
  opacity: 0;
  transition: all .3s;
}
.lid-icon-bg.open ~ .lid-pulse {
  animation: pulse-ring 1.4s ease infinite;
}
@keyframes pulse-ring {
  0%   { transform: scale(1);   opacity: .6; }
  100% { transform: scale(1.5); opacity: 0; }
}
.lid-status-label {
  font-size: 28px;
  font-weight: 600;
  color: var(--white);
  letter-spacing: -.5px;
}
.lid-status-label.open  { color: var(--leaf); }
.lid-status-label.closed { color: rgba(255,255,255,.7); }
.lid-sub { font-size: 13px; color: rgba(255,255,255,.4); margin-top: 6px; }

/* Stat cards */
.stats-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}
.stat-card {
  background: rgba(255,255,255,.05);
  border: 1px solid rgba(255,255,255,.07);
  border-radius: 14px;
  padding: 18px 20px;
}
.stat-card .label {
  font-size: 12px;
  color: rgba(255,255,255,.4);
  text-transform: uppercase;
  letter-spacing: .08em;
  margin-bottom: 8px;
}
.stat-card .value {
  font-size: 26px;
  font-weight: 600;
  color: var(--white);
}
.stat-card .value.green { color: var(--leaf); }
.stat-card .value.amber { color: #FBBF24; }

/* Activity feed */
.activity-section { flex: 1; }
.section-title {
  font-size: 11px;
  font-weight: 600;
  letter-spacing: .12em;
  text-transform: uppercase;
  color: rgba(255,255,255,.35);
  margin-bottom: 14px;
}
.feed {
  display: flex;
  flex-direction: column;
  gap: 10px;
  max-height: 280px;
  overflow-y: auto;
}
.feed::-webkit-scrollbar { width: 4px; }
.feed::-webkit-scrollbar-track { background: transparent; }
.feed::-webkit-scrollbar-thumb { background: rgba(255,255,255,.15); border-radius: 2px; }
.feed-item {
  display: flex;
  gap: 12px;
  align-items: flex-start;
  padding: 12px 14px;
  background: rgba(255,255,255,.04);
  border-radius: 10px;
  border-left: 3px solid transparent;
  animation: fadeIn .4s ease;
}
@keyframes fadeIn { from { opacity:0; transform:translateY(6px); } to { opacity:1; transform:none; } }
.feed-item.event-lid_open    { border-left-color: var(--leaf); }
.feed-item.event-done        { border-left-color: var(--gold); }
.feed-item.event-esp32_offline { border-left-color: var(--red); }
.feed-item.event-photo_received { border-left-color: #60A5FA; }
.feed-icon { font-size: 16px; flex-shrink: 0; margin-top: 1px; }
.feed-text { font-size: 13px; color: rgba(255,255,255,.7); line-height: 1.4; }
.feed-time { font-size: 11px; color: rgba(255,255,255,.3); margin-top: 2px; }

/* ESP32 status pill */
.esp-pill {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  padding: 6px 14px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 500;
  background: rgba(220,38,38,.15);
  color: #FCA5A5;
  border: 1px solid rgba(220,38,38,.3);
  transition: all .4s;
}
.esp-pill.online {
  background: rgba(77,184,122,.15);
  color: var(--leaf);
  border-color: rgba(77,184,122,.3);
}
.esp-dot {
  width: 7px; height: 7px;
  border-radius: 50%;
  background: #FCA5A5;
  transition: all .4s;
}
.esp-pill.online .esp-dot {
  background: var(--leaf);
  box-shadow: 0 0 0 3px rgba(77,184,122,.25);
  animation: blink 2s ease infinite;
}
@keyframes blink { 0%,100%{opacity:1} 50%{opacity:.3} }

/* ── RIGHT: Form panel ────────────────────── */
.form-panel {
  background: var(--white);
  display: flex;
  flex-direction: column;
  overflow-y: auto;
}
.form-inner {
  padding: 44px 36px;
  flex: 1;
  display: flex;
  flex-direction: column;
}
.form-header { margin-bottom: 32px; }
.form-header h2 {
  font-family: 'Playfair Display', serif;
  font-size: 26px;
  color: var(--forest);
  margin-bottom: 6px;
}
.form-header p { font-size: 13px; color: var(--gray); line-height: 1.5; }

.accepted-box {
  background: #F0FDF4;
  border: 1px solid #BBF7D0;
  border-radius: 12px;
  padding: 14px 16px;
  margin-bottom: 28px;
  font-size: 13px;
  color: #166534;
  line-height: 1.6;
}
.accepted-box strong { display: block; margin-bottom: 4px; font-weight: 600; }

.camera-box {
  background: var(--cream);
  border-radius: 14px;
  border: 2px dashed var(--lgray);
  height: 180px;
  display: flex; align-items: center; justify-content: center;
  overflow: hidden;
  position: relative;
  margin-bottom: 28px;
  transition: border-color .3s, background .3s;
}
.camera-box.active { border-color: var(--leaf); background: #F0FFF4; }
.camera-box video, .camera-box img {
  width: 100%; height: 100%; object-fit: cover; border-radius: 12px;
}
.camera-placeholder {
  text-align: center;
  color: var(--gray);
}
.camera-placeholder span { font-size: 36px; display: block; margin-bottom: 8px; }
.camera-placeholder p { font-size: 13px; }
.flash-overlay {
  position: absolute; inset: 0;
  background: white; opacity: 0; pointer-events: none;
  transition: opacity .1s;
}
.flash-overlay.active { opacity: .9; }

.form-group { margin-bottom: 20px; }
.form-group label {
  display: block;
  font-size: 13px;
  font-weight: 600;
  color: var(--forest);
  margin-bottom: 8px;
  letter-spacing: .02em;
}
.form-group input {
  width: 100%;
  padding: 13px 16px;
  border: 1.5px solid var(--lgray);
  border-radius: 10px;
  font-size: 15px;
  font-family: inherit;
  color: var(--ink);
  transition: border-color .2s, box-shadow .2s;
  background: var(--white);
}
.form-group input:focus {
  outline: none;
  border-color: var(--mid);
  box-shadow: 0 0 0 3px rgba(45,138,98,.1);
}
.form-group .hint { font-size: 12px; color: var(--gray); margin-top: 5px; }

.submit-btn {
  width: 100%;
  padding: 16px;
  background: var(--forest);
  color: var(--white);
  border: none;
  border-radius: 12px;
  font-size: 16px;
  font-weight: 600;
  font-family: inherit;
  cursor: pointer;
  display: flex; align-items: center; justify-content: center; gap: 10px;
  transition: background .2s, transform .15s, box-shadow .2s;
  margin-top: 6px;
  letter-spacing: .02em;
}
.submit-btn:hover:not(:disabled) {
  background: var(--green);
  transform: translateY(-2px);
  box-shadow: 0 8px 24px rgba(13,59,46,.25);
}
.submit-btn:active:not(:disabled) { transform: translateY(0); }
.submit-btn:disabled { background: var(--lgray); color: var(--gray); cursor: not-allowed; }

.status-bar {
  margin-top: 20px;
  padding: 14px 16px;
  border-radius: 10px;
  font-size: 14px;
  display: none;
  animation: fadeIn .3s ease;
}
.status-bar.loading { background: #FFF7ED; color: #92400E; border: 1px solid #FDE68A; display: flex; align-items: center; gap: 10px; }
.status-bar.success { background: #F0FDF4; color: #166534; border: 1px solid #BBF7D0; display: block; }
.status-bar.error   { background: #FEF2F2; color: #991B1B; border: 1px solid #FECACA; display: block; }
.status-bar.info    { background: #EFF6FF; color: #1E40AF; border: 1px solid #BFDBFE; display: block; }

.spinner {
  width: 18px; height: 18px;
  border: 2.5px solid #FDE68A;
  border-top-color: #92400E;
  border-radius: 50%;
  animation: spin .8s linear infinite;
  flex-shrink: 0;
}
@keyframes spin { to { transform: rotate(360deg); } }

.file-label {
  display: inline-block;
  margin-top: 14px;
  padding: 10px 18px;
  background: #EFF6FF;
  color: #1E40AF;
  border: 1.5px dashed #93C5FD;
  border-radius: 8px;
  cursor: pointer;
  font-size: 13px;
  font-weight: 500;
  transition: all .2s;
}
.file-label:hover { background: #1E40AF; color: white; }
.file-label input { display: none; }

@media (max-width: 860px) {
  body { grid-template-columns: 1fr; grid-template-rows: auto 1fr; }
  .dashboard { padding: 28px 20px; gap: 20px; }
  .form-inner { padding: 28px 24px; }
}
</style>
</head>
<body>

<!-- ══════════ LEFT: LIVE DASHBOARD ══════════ -->
<aside class="dashboard">

  <div class="brand">
    <div class="brand-icon">♻️</div>
    <div class="brand-text">
      <h1>Smart E-Waste Bin</h1>
      <p>IoT-Powered Collection System</p>
    </div>
  </div>

  <!-- ESP32 pill -->
  <div>
    <div class="esp-pill" id="espPill">
      <div class="esp-dot"></div>
      <span id="espLabel">ESP32 Offline</span>
    </div>
  </div>

  <!-- Lid Hero -->
  <div class="lid-hero">
    <div class="lid-icon-wrap">
      <div class="lid-icon-bg" id="lidIconBg">
        <span class="lid-emoji" id="lidEmoji">🗑️</span>
      </div>
      <div class="lid-pulse" id="lidPulse"></div>
    </div>
    <div class="lid-status-label closed" id="lidLabel">LID CLOSED</div>
    <div class="lid-sub" id="lidSub">Waiting for next submission</div>
    <div class="lid-sub" style="margin-top:8px;font-size:12px;color:rgba(255,255,255,.25)" id="lidUpdated">—</div>
  </div>

  <!-- Stats -->
  <div class="stats-row">
    <div class="stat-card">
      <div class="label">Today's Recycled</div>
      <div class="value green" id="statTotal">0</div>
    </div>
    <div class="stat-card">
      <div class="label">Last User</div>
      <div class="value" id="statUser" style="font-size:16px;margin-top:4px">—</div>
    </div>
    <div class="stat-card">
      <div class="label">Status</div>
      <div class="value amber" id="statProc" style="font-size:15px;margin-top:4px">Idle</div>
    </div>
    <div class="stat-card">
      <div class="label">Last Updated</div>
      <div class="value" id="statTime" style="font-size:15px;margin-top:4px">—</div>
    </div>
  </div>

  <!-- Activity Feed -->
  <div class="activity-section">
    <div class="section-title">Live Activity Feed</div>
    <div class="feed" id="actFeed">
      <div class="feed-item">
        <span class="feed-icon">🟢</span>
        <div>
          <div class="feed-text">System initialised — ready to accept e-waste</div>
          <div class="feed-time" id="initTime"></div>
        </div>
      </div>
    </div>
  </div>

</aside>

<!-- ══════════ RIGHT: FORM PANEL ══════════ -->
<main class="form-panel">
<div class="form-inner">

  <div class="form-header">
    <h2>Deposit E-Waste</h2>
    <p>Fill in your details below. Once submitted, the bin lid will open automatically and a certificate will be emailed to you.</p>
  </div>

  <div class="accepted-box">
    <strong> Accepted Items</strong>
    Mobile phones, batteries, chargers, cables, circuit boards, keyboards, mice, remotes, and other small electronic gadgets.
  </div>

  <div class="camera-box" id="cameraBox">
    <div class="flash-overlay" id="flash"></div>
    <div class="camera-placeholder" id="camPlaceholder">
      <span>📷</span>
      <p>Camera activates after submission</p>
    </div>
  </div>

  <form id="ewasteForm">
    <div class="form-group">
      <label for="inp_name">Full Name</label>
      <input type="text" id="inp_name" required placeholder="e.g. Priya Sharma">
    </div>
    <div class="form-group">
      <label for="inp_mobile">Mobile Number</label>
      <input type="tel" id="inp_mobile" required placeholder="+91 98765 43210" pattern="[0-9+\s\-]{10,15}">
    </div>
    <div class="form-group">
      <label for="inp_email">Email Address</label>
      <input type="email" id="inp_email" required placeholder="you@example.com">
      <p class="hint">Your certificate will be sent here</p>
    </div>
    <button type="submit" class="submit-btn" id="submitBtn">
      <span>Submit &amp; Open Bin</span>
    </button>
  </form>

  <div class="status-bar" id="statusBar"></div>
</div>
</main>

<script>
// ── SSE: subscribe to live updates ──────────────────────────
const evtSource = new EventSource('/api/stream');

const ICONS = {
  submission:      '📋',
  esp32_triggered: '📡',
  lid_open:        '🔓',
  lid_closed:      '🔒',
  photo_received:  '📸',
  generating:      '🖨️',
  sending_email:   '📧',
  done:            '🏆',
  timeout:         '⏱️',
  email_failed:    '⚠️',
  esp32_offline:   '🔴',
  init:            '🟢',
};

function applyStatus(s) {
  // Lid hero
  const isOpen = s.lid === 'open';
  const bg     = document.getElementById('lidIconBg');
  const emoji  = document.getElementById('lidEmoji');
  const label  = document.getElementById('lidLabel');
  const sub    = document.getElementById('lidSub');

  bg.className = 'lid-icon-bg ' + (isOpen ? 'open' : '');
  emoji.textContent  = isOpen ? '🔓' : '🗑️';
  label.textContent  = isOpen ? 'LID  OPEN' : 'LID  CLOSED';
  label.className    = 'lid-status-label ' + (isOpen ? 'open' : 'closed');
  sub.textContent    = s.last_event || '—';
  document.getElementById('lidUpdated').textContent = 'Updated ' + s.last_updated;

  // Stats
  document.getElementById('statTotal').textContent = s.total_today ?? 0;
  document.getElementById('statUser').textContent  = s.last_user  ?? '—';
  document.getElementById('statProc').textContent  = s.processing ? 'Processing…' : 'Idle';
  document.getElementById('statTime').textContent  = s.last_updated ?? '—';

  // ESP pill
  const pill = document.getElementById('espPill');
  const lbl  = document.getElementById('espLabel');
  if (s.esp32_online) {
    pill.classList.add('online');
    lbl.textContent = 'ESP32 Online';
  } else {
    pill.classList.remove('online');
    lbl.textContent = 'ESP32 Offline';
  }
}

function addFeedItem(type, text) {
  const feed = document.getElementById('actFeed');
  const now  = new Date().toLocaleTimeString('en-IN', {hour:'2-digit',minute:'2-digit',second:'2-digit'});
  const div  = document.createElement('div');
  div.className = `feed-item event-${type}`;
  div.innerHTML = `
    <span class="feed-icon">${ICONS[type] || '•'}</span>
    <div>
      <div class="feed-text">${text}</div>
      <div class="feed-time">${now}</div>
    </div>`;
  feed.insertBefore(div, feed.firstChild);
  // Keep max 20 items
  while (feed.children.length > 20) feed.removeChild(feed.lastChild);
}

evtSource.onmessage = (e) => {
  const msg = JSON.parse(e.data);
  if (msg.status) applyStatus(msg.status);

  const type = msg.type;
  const s    = msg.status || {};

  const messages = {
    init:            'System connected — dashboard live',
    submission:      `New submission from ${msg.name || s.last_user || '—'}`,
    esp32_triggered: `ESP32 triggered — opening bin lid for ${msg.name || '—'}`,
    lid_open:        'Bin lid opened — deposit e-waste now',
    lid_closed:      'Bin lid closed — deposit complete',
    photo_received:  'Photo captured and received by server',
    generating:      `Generating certificate for ${msg.name || '—'}`,
    sending_email:   `Sending certificate email to ${msg.email || '—'}`,
    done:            `✓ Certificate emailed to ${msg.name || '—'}`,
    timeout:         'Timeout — processing without photo',
    email_failed:    'Certificate generated but email delivery failed',
    esp32_offline:   'ESP32 unreachable — falling back to webcam',
  };

  if (messages[type]) addFeedItem(type, messages[type]);
};

evtSource.onerror = () => {
  addFeedItem('esp32_offline', 'SSE connection lost — retrying…');
};

// Init time
document.getElementById('initTime').textContent =
  new Date().toLocaleTimeString('en-IN', {hour:'2-digit',minute:'2-digit',second:'2-digit'});

// ── Form logic ───────────────────────────────────────────────
let currentReqId = null;
let videoStream  = null;

function reqId() {
  return Date.now().toString(36) + Math.random().toString(36).slice(2);
}

function showStatus(type, html) {
  const el = document.getElementById('statusBar');
  el.className   = 'status-bar ' + type;
  el.innerHTML   = html;
  el.style.display = '';
}

function resetForm() {
  document.getElementById('ewasteForm').reset();
  document.getElementById('submitBtn').disabled = false;
  document.getElementById('statusBar').style.display = 'none';
  if (videoStream) { videoStream.getTracks().forEach(t => t.stop()); videoStream = null; }
  const box = document.getElementById('cameraBox');
  box.className   = 'camera-box';
  box.innerHTML   = `<div class="flash-overlay" id="flash"></div>
    <div class="camera-placeholder" id="camPlaceholder">
      <span>📷</span><p>Camera activates after submission</p></div>`;
  const man = document.getElementById('manualWrap');
  if (man) man.remove();
}

async function captureWebcam(rid) {
  const box = document.getElementById('cameraBox');
  box.className = 'camera-box active';
  box.innerHTML = `<div class="flash-overlay" id="flash"></div>`;
  showStatus('loading', '<div class="spinner"></div><span>Starting webcam…</span>');

  try {
    videoStream = await navigator.mediaDevices.getUserMedia(
      { video: { facingMode:'user', width:640, height:480 } }
    );
    const vid = document.createElement('video');
    vid.srcObject = videoStream; vid.autoplay = true; vid.playsInline = true;
    box.appendChild(vid);
    await new Promise(r => { vid.onloadedmetadata = () => { vid.play(); r(); }; });

    for (let i = 3; i > 0; i--) {
      showStatus('info', `📸 Smile! Capturing in <strong>${i}</strong>…`);
      await new Promise(r => setTimeout(r, 1000));
    }

    document.getElementById('flash').classList.add('active');
    setTimeout(() => document.getElementById('flash').classList.remove('active'), 200);

    const canvas = document.createElement('canvas');
    canvas.width = vid.videoWidth; canvas.height = vid.videoHeight;
    canvas.getContext('2d').drawImage(vid, 0, 0);

    videoStream.getTracks().forEach(t => t.stop()); videoStream = null;

    box.innerHTML = '';
    const img = document.createElement('img');
    img.src = canvas.toDataURL('image/jpeg');
    box.appendChild(img);

    showStatus('loading', '<div class="spinner"></div><span>Uploading photo…</span>');

    const blob = await new Promise(r => canvas.toBlob(r, 'image/jpeg', .9));
    const fd   = new FormData();
    fd.append('photo', blob, 'webcam.jpg');
    fd.append('request_id', rid);

    const res  = await fetch('/api/test-upload', { method:'POST', body:fd });
    const data = await res.json();

    if (data.success) {
      showStatus('success', ' <strong>Photo captured!</strong> Certificate is being generated and will be emailed shortly.');
      setTimeout(resetForm, 9000);
    } else throw new Error(data.error || 'Upload failed');

  } catch (err) {
    if (videoStream) { videoStream.getTracks().forEach(t => t.stop()); videoStream = null; }
    showManualUpload(rid, err.message);
  }
}

function showManualUpload(rid, errMsg) {
  showStatus('error', ` <strong>Camera Error:</strong> ${errMsg} — please upload a photo manually.`);
  let wrap = document.getElementById('manualWrap');
  if (!wrap) {
    wrap = document.createElement('div');
    wrap.id = 'manualWrap';
    wrap.style.marginTop = '14px';
    wrap.innerHTML = `<label class="file-label">
      📎 Choose Photo
      <input type="file" id="manFile" accept="image/*" capture="user">
    </label>`;
    document.getElementById('ewasteForm').appendChild(wrap);

    document.getElementById('manFile').addEventListener('change', async e => {
      const file = e.target.files[0]; if (!file) return;
      const box  = document.getElementById('cameraBox');
      box.innerHTML = '';
      const img = document.createElement('img');
      img.src = URL.createObjectURL(file); box.appendChild(img);
      showStatus('loading', '<div class="spinner"></div><span>Uploading…</span>');
      const fd = new FormData();
      fd.append('photo', file); fd.append('request_id', rid);
      try {
        const r = await (await fetch('/api/test-upload', { method:'POST', body:fd })).json();
        if (r.success) {
          showStatus('success', ' <strong>Uploaded!</strong> Certificate will be emailed shortly.');
          wrap.remove(); setTimeout(resetForm, 9000);
        } else throw new Error(r.error);
      } catch(err) {
        showStatus('error', ` Upload failed: ${err.message}`);
      }
    });
  }
}

document.getElementById('ewasteForm').addEventListener('submit', async e => {
  e.preventDefault();
  const btn = document.getElementById('submitBtn');
  btn.disabled = true;
  currentReqId = reqId();

  const payload = {
    name:       document.getElementById('inp_name').value.trim(),
    mobile:     document.getElementById('inp_mobile').value.trim(),
    email:      document.getElementById('inp_email').value.trim(),
    request_id: currentReqId,
  };

  showStatus('loading', '<div class="spinner"></div><span>Submitting details…</span>');

  try {
    const res  = await fetch('/api/submit', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(payload)
    });
    const data = await res.json();

    if (!data.success) throw new Error(data.error || 'Server error');

    if (data.esp32_triggered) {
      showStatus('success', ' <strong>ESP32 triggered!</strong> Please stand in front of the camera and deposit your e-waste. The lid will close automatically.');
      setTimeout(resetForm, 12000);
    } else {
      await captureWebcam(currentReqId);
    }
  } catch (err) {
    showStatus('info', 'Server issue — switching to webcam fallback…');
    await captureWebcam(currentReqId);
  }
});
</script>
</body>
</html>
"""

# ==================== RUN ====================
if __name__ == '__main__':
    print(" Smart E-Waste Dustbin Server — Enhanced Edition")
    print(f" Email: {SENDER_EMAIL}")
    print(f" ESP32: {ESP32_TRIGGER_URL}")
    print(" Running at http://0.0.0.0:5000")
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)

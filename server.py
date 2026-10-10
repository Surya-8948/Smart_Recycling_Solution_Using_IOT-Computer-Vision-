

import os
import re
import json
import time
import queue
import secrets
import threading
import smtplib
import ssl
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders

from flask import Flask, request, jsonify, Response, stream_with_context, send_file
from PIL import Image
import qrcode
import paho.mqtt.client as mqtt
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import landscape, A4
from reportlab.lib.colors import HexColor

# ── Compatibility shim: pre-load the stdlib 'idna' codec in the MAIN thread ──
# Werkzeug encodes the server name with the 'idna' codec on EVERY request
# (werkzeug/routing/map.py -> Map.bind). On some Python builds (e.g. certain
# Render images) the codec's lazy import fails inside gunicorn worker threads,
# making every request fail with "LookupError: unknown encoding: idna"
# (cpython issue #29288). A no-op lookup here — in the main thread, before any
# worker starts — loads and caches the codec so worker threads never hit the
# broken path.
try:
    "".encode("idna")
except LookupError:
    # Last-resort fallback for builds where the stdlib codec is unusable:
    # register an ASCII passthrough codec (hostnames here are always ASCII,
    # e.g. *.onrender.com), which is all Werkzeug needs it for.
    import codecs as _codecs

    def _idna_fallback_search(name):
        norm = str(name).replace("-", "_").replace(".", "_").lower()
        if norm in ("idna", "idna_2003"):
            return _codecs.CodecInfo(
                name="idna",
                encode=lambda s, errors="strict": (s.encode("ascii"), len(s)),
                decode=lambda b, errors="strict": (b.decode("ascii"), len(b)),
            )
        return None

    _codecs.register(_idna_fallback_search)

# ==================== CONFIGURATION (env-driven → Render ready) ====================
MQTT_BROKER       = os.environ.get("MQTT_BROKER", "broker.emqx.io")   # free public test broker
MQTT_PORT         = int(os.environ.get("MQTT_PORT", "8883"))          # 8883 = TLS (secure)
MQTT_USERNAME     = os.environ.get("MQTT_USERNAME", "")
MQTT_PASSWORD     = os.environ.get("MQTT_PASSWORD", "")
MQTT_TLS          = os.environ.get("MQTT_TLS", "true").lower() in ("1", "true", "yes")
MQTT_CA_CERT      = os.environ.get("MQTT_CA_CERT", "")                # optional: custom CA file path
MQTT_TLS_VERIFY   = os.environ.get("MQTT_TLS_VERIFY", "true").lower() in ("1", "true", "yes")
MQTT_TOPIC_PREFIX = os.environ.get("MQTT_TOPIC_PREFIX", "smartbin")
MQTT_DEVICE_ID    = os.environ.get("MQTT_DEVICE_ID", "bin01")

SMTP_SERVER     = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT       = int(os.environ.get("SMTP_PORT", "587"))
SENDER_EMAIL    = os.environ.get("SENDER_EMAIL", "")
SENDER_PASSWORD = os.environ.get("SENDER_PASSWORD", "")   # Gmail App Password (env only!)

DEV_NAME     = os.environ.get("DEV_NAME", "Your Name")
DEV_ROLE     = os.environ.get("DEV_ROLE", "IoT Developer")
DEV_INITIALS = os.environ.get("DEV_INITIALS", "YN")
DEV_GITHUB   = os.environ.get("DEV_GITHUB", "https://github.com/your-handle")
DEV_LINKEDIN = os.environ.get("DEV_LINKEDIN", "https://www.linkedin.com/in/your-handle")
DEV_EMAIL    = os.environ.get("DEV_EMAIL", "you@example.com")

PORT = int(os.environ.get("PORT", "5000"))

# MQTT topics (server ↔ ESP32)
TOPIC_CMD    = f"{MQTT_TOPIC_PREFIX}/{MQTT_DEVICE_ID}/cmd"     # server → ESP32 (open lid)
TOPIC_ACK    = f"{MQTT_TOPIC_PREFIX}/{MQTT_DEVICE_ID}/ack"     # ESP32 → server (lid_opened / lid_closed)
TOPIC_STATUS = f"{MQTT_TOPIC_PREFIX}/{MQTT_DEVICE_ID}/status"  # ESP32 → server (heartbeat / LWT)

OUTPUT_DIR = "certificates"
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ==================== INPUT VALIDATION ====================
NAME_RE   = re.compile(r"^[A-Za-z][A-Za-z .'\-]{1,49}$")          # English letters (certificate font is Latin)
MOBILE_RE = re.compile(r"^\+?[0-9][0-9\s\-]{8,14}$")
EMAIL_RE  = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
RID_RE    = re.compile(r"^[a-f0-9]{16}$")                          # server-generated request ids

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024   # 5 MB max photo upload

# ==================== REAL-TIME STATE ====================
pending_requests = {}   # request_id → user details + photo path
request_events   = {}   # request_id → {"opened": Event, "closed": Event}

system_status = {
    "lid":            "closed",
    "last_event":     "System ready",
    "last_user":      None,
    "last_request_id": None,
    "last_updated":   datetime.now().strftime("%H:%M:%S"),
    "total_today":    0,
    "esp32_online":   False,
    "mqtt_connected": False,
    "processing":     False,
}

# SSE subscribers (one queue per browser tab)
sse_subscribers = []
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

# ==================== MQTT CLIENT ====================
def _new_mqtt_client():
    """Create a paho client compatible with both paho-mqtt v1 and v2."""
    cid = f"ewaste-server-{secrets.token_hex(4)}"
    try:
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=cid)   # paho v2
    except (AttributeError, TypeError):
        return mqtt.Client(client_id=cid)                                    # paho v1

def _rc_ok(rc):
    if hasattr(rc, "is_failure"):        # paho v2 ReasonCode
        return not rc.is_failure
    return rc == 0                        # paho v1 int

def handle_lid_event(event: str, request_id: str):
    """Handle lid_opened / lid_closed acknowledgements from the ESP32 (MQTT ack topic)."""
    system_status["esp32_online"] = True
    is_known = request_id in pending_requests
    if event == "lid_opened":
        system_status["lid"] = "open"
        if is_known:
            system_status["processing"] = True
            system_status["last_event"] = "Lid opened — waiting for e-waste deposit (30s window)"
        else:
            system_status["last_event"] = "Lid opened (standalone button — no certificate)"
        if request_id in request_events:
            request_events[request_id]["opened"].set()
        push_event("lid_open", {"request_id": request_id})
        print(f"🔓 Lid OPENED  ({'request ' + request_id if is_known else 'standalone button'})")
    elif event == "lid_closed":
        system_status["lid"] = "closed"
        if is_known:
            system_status["last_event"] = "Lid closed — generating certificate"
        else:
            system_status["last_event"] = "Lid closed (standalone mode)"
        if request_id in request_events:
            request_events[request_id]["closed"].set()
        push_event("lid_closed", {"request_id": request_id})
        print(f"🔒 Lid CLOSED  ({'request ' + request_id if is_known else 'standalone button'})")

def handle_status(payload: dict):
    """Handle ESP32 heartbeat / retained status messages."""
    was_online = system_status["esp32_online"]
    system_status["esp32_online"] = bool(payload.get("online", True))
    lid = payload.get("lid")
    if lid in ("open", "closed"):
        system_status["lid"] = lid
    if was_online != system_status["esp32_online"]:
        push_event("esp32_status", {"online": system_status["esp32_online"]})

def on_connect(client, userdata, flags, rc, properties=None):
    if _rc_ok(rc):
        system_status["mqtt_connected"] = True
        client.subscribe(TOPIC_ACK, qos=1)
        client.subscribe(TOPIC_STATUS, qos=1)
        push_event("mqtt_connected", {})
        print(f"✅ MQTT connected → {MQTT_BROKER}:{MQTT_PORT}" + (" 🔐 (TLS)" if MQTT_TLS else ""))
        print(f"   📤 publishes → {TOPIC_CMD}")
        print(f"   📥 subscribes → {TOPIC_ACK} , {TOPIC_STATUS}")
    else:
        print(f"❌ MQTT connect failed: {rc}")

def on_disconnect(client, userdata, *args):
    system_status["mqtt_connected"] = False
    push_event("mqtt_disconnected", {})
    print("⚠️  MQTT disconnected — paho will auto-reconnect")

def on_message(client, userdata, msg):
    try:
        payload = json.loads(msg.payload.decode("utf-8"))
    except Exception:
        print(f"⚠️  Unparseable MQTT message on {msg.topic}")
        return
    if msg.topic == TOPIC_ACK:
        handle_lid_event(payload.get("event", ""), payload.get("request_id", ""))
    elif msg.topic == TOPIC_STATUS:
        handle_status(payload)

mqtt_client = _new_mqtt_client()
_mqtt_started = False
_mqtt_lock = threading.Lock()

def start_mqtt():
    """Configure + start the MQTT client. Fully non-blocking: connect_async()
    only queues the connection, and loop_start() runs the whole network loop
    (TLS handshake included) in paho's own background thread."""
    if MQTT_USERNAME:
        mqtt_client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
    if MQTT_TLS:
        # 🔐 Secure MQTT (MQTTS): TLS-encrypted connection
        if MQTT_CA_CERT:
            # Custom/self-signed CA (e.g. your own Mosquitto broker)
            mqtt_client.tls_set(ca_certs=MQTT_CA_CERT,
                                cert_reqs=ssl.CERT_REQUIRED,
                                tls_version=ssl.PROTOCOL_TLS_CLIENT)
        else:
            # System default CAs (works with public brokers like broker.emqx.io)
            mqtt_client.tls_set_context(ssl.create_default_context())
        if not MQTT_TLS_VERIFY:
            mqtt_client.tls_insecure_set(True)
    mqtt_client.on_connect = on_connect
    mqtt_client.on_disconnect = on_disconnect
    mqtt_client.on_message = on_message
    mqtt_client.reconnect_delay_set(min_delay=1, max_delay=30)
    try:
        # connect_async = non-blocking; loop_start auto-reconnects forever
        mqtt_client.connect_async(MQTT_BROKER, MQTT_PORT, keepalive=30)
        mqtt_client.loop_start()
        tls_tag = " 🔐 TLS" if MQTT_TLS else ""
        print(f"🔌 MQTT connecting → {MQTT_BROKER}:{MQTT_PORT}{tls_tag} …")
    except Exception as e:
        print(f"⚠️  MQTT init failed: {e}")

def ensure_mqtt_started():
    """Start MQTT exactly once, LAZILY — on the first request the app serves.

    Why lazy: Gunicorn imports this module while booting a worker. Keeping the
    import free of network/thread side effects means the worker boots (and
    starts heart-beating) instantly — no WORKER TIMEOUT during boot. MQTT then
    connects in the background as soon as the app is actually serving (Render's
    /api/health health check triggers it immediately after boot)."""
    global _mqtt_started
    if _mqtt_started:
        return
    with _mqtt_lock:
        if _mqtt_started:
            return
        _mqtt_started = True
        start_mqtt()

@app.before_request
def _start_mqtt_when_app_is_ready():
    # First request (incl. Render's health check) → start MQTT once.
    ensure_mqtt_started()

# ==================== ROUTES ====================

@app.route("/")
def index():
    html = WEB_FORM_HTML
    for token, value in {
        "@@DEV_NAME@@":     DEV_NAME,
        "@@DEV_ROLE@@":     DEV_ROLE,
        "@@DEV_INITIALS@@": DEV_INITIALS,
        "@@DEV_GITHUB@@":   DEV_GITHUB,
        "@@DEV_LINKEDIN@@": DEV_LINKEDIN,
        "@@DEV_EMAIL@@":    DEV_EMAIL,
    }.items():
        html = html.replace(token, value)
    return Response(html, mimetype="text/html")

@app.route("/api/health")
def health():
    """Health check (used by Render)."""
    return jsonify({
        "status": "ok",
        "mqtt_connected": system_status["mqtt_connected"],
        "esp32_online": system_status["esp32_online"],
    })

@app.route("/api/submit", methods=["POST"])
def submit_details():
    """
    One-shot submit: user details + photo (multipart/form-data).
    Server saves the photo, then sends an MQTT `open_lid` command to the ESP32.
    """
    name   = (request.form.get("name") or "").strip()
    mobile = (request.form.get("mobile") or "").strip()
    email  = (request.form.get("email") or "").strip()
    photo  = request.files.get("photo")

    if not NAME_RE.fullmatch(name):
        return jsonify({"error": "Invalid name — 2–50 English letters, spaces, . ' - only"}), 400
    if not MOBILE_RE.fullmatch(mobile):
        return jsonify({"error": "Invalid mobile number"}), 400
    if not EMAIL_RE.fullmatch(email):
        return jsonify({"error": "Invalid email address"}), 400
    if photo is None or photo.filename == "":
        return jsonify({"error": "Photo is required — capture one or upload from gallery"}), 400

    # request_id is ALWAYS generated server-side (prevents path traversal)
    request_id = secrets.token_hex(8)

    # Validate + normalise the photo (must be a real image)
    try:
        img = Image.open(photo.stream)
        img.verify()                       # raises if not a real image
        photo.stream.seek(0)
        img = Image.open(photo.stream).convert("RGB")
        img.thumbnail((800, 800))
        photo_path = os.path.join(OUTPUT_DIR, f"{request_id}_photo.jpg")
        img.save(photo_path, "JPEG", quality=85)
    except Exception:
        return jsonify({"error": "Uploaded file is not a valid image"}), 400

    pending_requests[request_id] = {
        "name":       name,
        "mobile":     mobile,
        "email":      email,
        "timestamp":  datetime.now(),
        "photo_path": photo_path,
    }
    request_events[request_id] = {
        "opened": threading.Event(),
        "closed": threading.Event(),
    }

    system_status["last_user"]         = name
    system_status["last_request_id"]   = request_id
    system_status["processing"]        = True
    system_status["last_event"]        = f"New submission from {name} — sending MQTT command"
    push_event("submission", {"name": name, "request_id": request_id})

    # 📡 Send open-lid command to the ESP32 via MQTT
    command = {
        "cmd":        "open_lid",
        "request_id": request_id,
        "name":       name,
        "ts":         datetime.now().strftime("%H:%M:%S"),
    }
    if mqtt_client.is_connected():
        mqtt_client.publish(TOPIC_CMD, json.dumps(command), qos=1)
        system_status["last_event"] = f"MQTT command sent — opening lid for {name}"
        push_event("cmd_sent", {"name": name, "request_id": request_id})
        print(f"📡 MQTT → {TOPIC_CMD}: {json.dumps(command)}")
    else:
        system_status["last_event"] = "MQTT broker not connected — bin command could not be sent"
        push_event("mqtt_offline", {"request_id": request_id})
        print("⚠️  MQTT not connected — command NOT sent")

    # Watchdog: waits for ESP32 acks, then generates + emails the certificate
    threading.Thread(target=process_request, args=(request_id,), daemon=True).start()

    return jsonify({
        "success":    True,
        "request_id": request_id,
        "message":    "Details received — command sent to bin via MQTT.",
    })

@app.route("/api/certificate/<rid>")
def download_certificate(rid):
    """Download a generated certificate PDF."""
    if not RID_RE.fullmatch(rid):
        return jsonify({"error": "Invalid certificate id"}), 400
    path = os.path.join(OUTPUT_DIR, f"certificate_{rid}.pdf")
    if not os.path.exists(path):
        return jsonify({"error": "Certificate not ready or expired"}), 404
    return send_file(path, mimetype="application/pdf", as_attachment=True,
                     download_name=f"E-Waste_Certificate_{rid}.pdf")

@app.route("/api/stream")
def stream():
    """Server-Sent Events endpoint — browser subscribes here for live updates."""
    client_q = queue.Queue(maxsize=30)
    with sse_lock:
        sse_subscribers.append(client_q)
    client_q.put_nowait(json.dumps({"type": "init", "status": system_status}))

    def generate():
        try:
            while True:
                try:
                    msg = client_q.get(timeout=25)
                    yield f"data: {msg}\n\n"
                except queue.Empty:
                    yield ": heartbeat\n\n"
        except GeneratorExit:
            pass
        finally:
            with sse_lock:
                if client_q in sse_subscribers:
                    sse_subscribers.remove(client_q)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

# ==================== MAIN PROCESSING (watchdog) ====================

def process_request(request_id: str):
    """
    Certificate is generated ONLY after the deposit is confirmed by the ESP32:
    lid opens → user deposits e-waste → lid closes (lid_closed ack).
    If the deposit is never confirmed, NO certificate is issued — the user can retry.
    If the email fails, the certificate stays available for download on the dashboard.
    """
    ev = request_events.get(request_id)
    if ev is None:
        return
    try:
        # 1) Wait for the lid to open (ESP32 ack) — max 20s
        if not ev["opened"].wait(timeout=20):
            system_status["processing"]  = False
            system_status["last_event"] = "Bin did not respond — deposit not confirmed"
            push_event("no_deposit", {"request_id": request_id, "reason": "bin_offline"})
            print(f"⚠️  {request_id}: bin offline — deposit not confirmed, NO certificate issued")
            return

        # 2) Wait for the lid to close = waste deposited — max 45s (ESP32 auto-closes after its 30s window)
        if not ev["closed"].wait(timeout=45):
            system_status["processing"]  = False
            system_status["last_event"] = "Lid did not close — deposit not confirmed"
            push_event("no_deposit", {"request_id": request_id, "reason": "lid_not_closed"})
            print(f"⚠️  {request_id}: lid never closed — deposit not confirmed, NO certificate issued")
            return

        # 3) ✅ Deposit confirmed → NOW generate the certificate
        req = pending_requests.pop(request_id, None)
        if req is None:
            return

        system_status["last_event"] = f"Deposit confirmed — generating certificate for {req['name']}"
        push_event("generating", {"name": req["name"], "request_id": request_id})

        cert_path = generate_certificate(
            req["name"], req["mobile"], req["email"], req.get("photo_path"), request_id
        )

        system_status["last_event"] = f"Sending certificate email to {req['email']}"
        push_event("sending_email", {"email": req["email"], "request_id": request_id})

        email_status = send_certificate_email(req["email"], req["name"], cert_path)

        system_status["processing"]  = False
        system_status["total_today"] += 1     # only confirmed deposits are counted
        cert_url = f"/api/certificate/{request_id}"

        if email_status == "sent":
            system_status["last_event"] = f"Certificate emailed to {req['name']} ✓"
            push_event("done", {"name": req["name"], "email": req["email"], "cert_url": cert_url, "request_id": request_id})
            print(f"🏆 Done for {req['name']} — email sent, cert: {cert_path}")
        else:
            # Email failed / not configured → certificate stays downloadable on the dashboard
            system_status["last_event"] = f"Certificate ready — download for {req['name']}"
            push_event("email_failed", {"name": req["name"], "cert_url": cert_url, "reason": email_status, "request_id": request_id})
            print(f"🏆 Done for {req['name']} — email {email_status}, cert downloadable: {cert_path}")
    except Exception as e:
        print(f"❌ process_request error for {request_id}: {e}")
        system_status["processing"]  = False
        system_status["last_event"] = "Processing error"
        push_event("error", {"error": str(e)})
    finally:
        request_events.pop(request_id, None)
        pending_requests.pop(request_id, None)   # cleanup on every path

# ==================== CERTIFICATE GENERATION ====================

def generate_certificate(name, mobile, email, photo_path, request_id):
    waste_category   = "E-Waste"
    location         = "Smart Eco E-Waste Dustbin Facility"
    civic_score      = 95 + (int(time.time()) % 6)          # 95–100, deterministic per second
    certificate_no   = f"ECO-{int(time.time() * 1000) % 1000000:06d}"
    now              = datetime.now()
    date_text        = now.strftime("%d %B %Y")
    time_text        = now.strftime("%I:%M %p")

    # ── QR payload ──
    qr_text = (
        f"SMART ECO CERTIFICATE\n\nName: {name}\nCategory: {waste_category}\n"
        f"Location: {location}\nDate: {date_text}\nTime: {time_text}\n"
        f"Civic Score: {civic_score}\nCertificate No: {certificate_no}\n\nStatus: VERIFIED"
    )
    qr = qrcode.QRCode(version=1, box_size=8, border=2)
    qr.add_data(qr_text)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white")
    qr_path = os.path.join(OUTPUT_DIR, f"{request_id}_qr.png")
    qr_img.save(qr_path)

    output_pdf = os.path.join(OUTPUT_DIR, f"certificate_{request_id}.pdf")
    c = canvas.Canvas(output_pdf, pagesize=landscape(A4))
    width, height = landscape(A4)

    # ── Background & strips ──
    c.setFillColor(HexColor("#0B1F16")); c.rect(0, 0, width, height, fill=1)
    c.setFillColor(HexColor("#163D2B")); c.rect(0, height - 80, width, 80, fill=1, stroke=0)
    c.setFillColor(HexColor("#10281D")); c.rect(0, 0, width, 55, fill=1, stroke=0)

    # ── Borders ──
    c.setStrokeColor(HexColor("#4CAF50")); c.setLineWidth(5)
    c.roundRect(25, 25, width - 50, height - 50, 20)
    c.setStrokeColor(HexColor("#81C784")); c.setLineWidth(1.5)
    c.roundRect(40, 40, width - 80, height - 80, 15)

    # ── Header & title ──
    c.setFont("Helvetica-Bold", 15); c.setFillColor(HexColor("#A5D6A7"))
    c.drawCentredString(width / 2, height - 60,
                        "SMART E WASTE MANAGEMENT • SUSTAINABILITY RECOGNITION CERTIFICATE")
    c.setFont("Helvetica-Bold", 34); c.setFillColor(HexColor("#F6D607"))
    c.drawCentredString(width / 2, height - 120, "CERTIFICATE")
    c.setFont("Helvetica", 18); c.setFillColor(HexColor("#C8E6C9"))
    c.drawCentredString(width / 2, height - 155, "OF ECO-RESPONSIBILITY")

    # ── Name ──
    c.setFont("Helvetica", 19); c.setFillColor(HexColor("#E8F5E9"))
    c.drawCentredString(width / 2, height - 225, "This certifies that")
    c.setFont("Helvetica-Bold", 33); c.setFillColor(HexColor("#76FF03"))
    c.drawCentredString(width / 2, height - 285, name.upper())
    tw = c.stringWidth(name.upper(), "Helvetica-Bold", 33)
    c.setStrokeColor(HexColor("#76FF03"))
    c.line((width - tw) / 2, height - 295, (width + tw) / 2, height - 295)
    c.setFont("Helvetica", 17); c.setFillColor(HexColor("#F6D607"))
    c.drawCentredString(width / 2, height - 320,
                        "has responsibly deposited e-waste at our Smart Eco-Dustbin facility")

    # ── Details box ──
    box_x, box_y, box_w, box_h = 110, 100, width - 220, 160
    c.setFillColor(HexColor("#163D2B"))
    c.roundRect(box_x, box_y, box_w, box_h, 15, fill=1, stroke=0)
    c.setFont("Helvetica-Bold", 14); c.setFillColor(HexColor("#FFFFFF"))
    ly = box_y + 140
    c.drawString(box_x + 25, ly,        f"Waste Category: {waste_category}")
    c.drawString(box_x + 25, ly - 30,   f"Deposited At: {location}")
    c.drawString(box_x + 25, ly - 60,   f"Date & Time: {date_text} • {time_text}")
    c.drawString(box_x + 25, ly - 90,   f"Mobile: {mobile}")
    c.drawRightString(box_x + box_w - 25, ly,        f"Civic Score: {civic_score}")
    c.drawRightString(box_x + box_w - 25, ly - 30,   f"Certificate No: {certificate_no}")
    c.drawRightString(box_x + box_w - 25, ly - 60,   "Status: E-Waste Verified")
    c.drawRightString(box_x + box_w - 25, ly - 90,   f"Email: {email}")

    # ── QR + photo ──
    c.drawImage(qr_path, width - 190, height - 290, width=110, height=110)
    if photo_path and os.path.exists(photo_path):
        c.drawImage(photo_path, 70, height - 280, width=100, height=100, mask="auto")

    # ── Footer ──
    c.setFont("Helvetica-Oblique", 10); c.setFillColor(HexColor("#A5D6A7"))
    c.drawCentredString(width / 2, 70,
        "Powered by Smart E-Waste Management System • Generated with Hope to Inspire Responsible Waste Disposal")

    c.save()
    return output_pdf

# ==================== EMAIL ====================

def send_certificate_email(recipient_email, name, certificate_path):
    """Send the certificate PDF by email. Returns 'sent' | 'not_configured' | 'failed'."""
    if not SENDER_EMAIL or not SENDER_PASSWORD:
        print("⚠️  SMTP not configured (SENDER_EMAIL / SENDER_PASSWORD) — skipping email")
        return "not_configured"

    msg         = MIMEMultipart()
    msg["From"] = SENDER_EMAIL
    msg["To"]   = recipient_email
    msg["Subject"] = "Your E-Waste Contribution Certificate — Smart Dustbin System"

    body = f"""Dear {name},

Thank you for contributing to a cleaner and greener environment by using our Smart E-Waste Collection System.

Your official Certificate of Eco-Recycling is attached to this email.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Certificate Details
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Contributor  : {name}
  Date         : {datetime.now().strftime("%B %d, %Y")}
  Category     : E-Waste
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
    msg.attach(MIMEText(body, "plain"))

    with open(certificate_path, "rb") as f:
        att = MIMEBase("application", "octet-stream")
        att.set_payload(f.read())
        encoders.encode_base64(att)
        att.add_header("Content-Disposition",
                       "attachment",
                       filename=f"E-Waste_Certificate_{name.replace(' ', '_')}.pdf")
        msg.attach(att)

    try:
        srv = smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=15)
        srv.starttls()
        srv.login(SENDER_EMAIL, SENDER_PASSWORD)
        srv.send_message(msg)
        srv.quit()
        print(f"📧 Email sent to {recipient_email}")
        return "sent"
    except Exception as e:
        print(f"❌ Email failed: {e}")
        return "failed"

# ==================== WEB UI ====================

WEB_FORM_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Smart E-Waste Bin — MQTT Edition</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@300;400;500;600;700&family=Playfair+Display:wght@600;700&display=swap" rel="stylesheet">
<style>
:root {
  --forest:#0D3B2E; --green:#1A5C42; --mid:#2D8A62; --leaf:#4DB87A;
  --cream:#F7F3EC; --gold:#C9A84C; --ink:#1C1C1C; --gray:#6B7280;
  --lgray:#E5E7EB; --white:#FFFFFF; --red:#DC2626; --blue:#2563EB;
}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
[hidden]{display:none !important}
body{font-family:'DM Sans',sans-serif;background:var(--forest);min-height:100vh;display:grid;grid-template-columns:1fr 420px;grid-template-rows:1fr;gap:0}

/* ── LEFT: Live Dashboard ── */
.dashboard{background:linear-gradient(160deg,#0a2e22 0%,#0D3B2E 60%,#112e24 100%);padding:40px 40px 28px;display:flex;flex-direction:column;gap:22px;overflow-y:auto}
.brand{display:flex;align-items:center;gap:16px}
.brand-icon{width:52px;height:52px;background:var(--leaf);border-radius:14px;display:flex;align-items:center;justify-content:center;font-size:26px;flex-shrink:0}
.brand-text h1{font-family:'Playfair Display',serif;font-size:22px;color:var(--white);line-height:1.2}
.brand-text p{font-size:12px;color:rgba(255,255,255,.45);margin-top:3px}

.pills-row{display:flex;gap:10px;flex-wrap:wrap}
.pill{display:inline-flex;align-items:center;gap:7px;padding:6px 14px;border-radius:999px;font-size:12px;font-weight:600;border:1px solid;transition:all .4s}
.pill .dot{width:7px;height:7px;border-radius:50%;transition:all .4s}
.pill.off{background:rgba(220,38,38,.12);color:#FCA5A5;border-color:rgba(220,38,38,.3)}
.pill.off .dot{background:#FCA5A5}
.pill.on{background:rgba(77,184,122,.15);color:var(--leaf);border-color:rgba(77,184,122,.35)}
.pill.on .dot{background:var(--leaf);box-shadow:0 0 0 3px rgba(77,184,122,.25);animation:blink 2s ease infinite}
.pill.mqtt.on{background:rgba(96,165,250,.15);color:#93C5FD;border-color:rgba(96,165,250,.4)}
.pill.mqtt.on .dot{background:#60A5FA;box-shadow:0 0 0 3px rgba(96,165,250,.25)}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.3}}

.lid-hero{background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.08);border-radius:20px;padding:28px 24px;text-align:center;position:relative;overflow:hidden}
.lid-hero::before{content:'';position:absolute;inset:0;background:radial-gradient(ellipse at 50% 0%,rgba(77,184,122,.12) 0%,transparent 70%)}
.lid-icon-wrap{position:relative;width:90px;height:90px;margin:0 auto 18px}
.lid-icon-bg{width:90px;height:90px;border-radius:50%;background:rgba(77,184,122,.15);border:2px solid rgba(77,184,122,.3);display:flex;align-items:center;justify-content:center;font-size:38px;transition:all .5s ease}
.lid-icon-bg.open{background:rgba(77,184,122,.25);border-color:var(--leaf);box-shadow:0 0 0 8px rgba(77,184,122,.1),0 0 0 16px rgba(77,184,122,.05)}
.lid-icon-bg.open .lid-emoji{animation:bounce .6s ease}
@keyframes bounce{0%,100%{transform:translateY(0)}40%{transform:translateY(-8px)}}
.lid-pulse{position:absolute;inset:-8px;border-radius:50%;border:2px solid var(--leaf);opacity:0}
.lid-icon-bg.open ~ .lid-pulse{animation:pulse-ring 1.4s ease infinite}
@keyframes pulse-ring{0%{transform:scale(1);opacity:.6}100%{transform:scale(1.5);opacity:0}}
.lid-status-label{font-size:26px;font-weight:700;color:rgba(255,255,255,.7);letter-spacing:-.5px;position:relative}
.lid-status-label.open{color:var(--leaf)}
.lid-sub{font-size:13px;color:rgba(255,255,255,.4);margin-top:6px;position:relative}
.standalone-hint{font-size:12px;color:rgba(255,255,255,.45);text-align:center;background:rgba(255,255,255,.04);border:1px dashed rgba(255,255,255,.14);border-radius:10px;padding:10px 14px;line-height:1.5}

.stats-row{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.stat-card{background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.07);border-radius:14px;padding:16px 18px}
.stat-card .label{font-size:11px;color:rgba(255,255,255,.4);text-transform:uppercase;letter-spacing:.08em;margin-bottom:7px}
.stat-card .value{font-size:24px;font-weight:700;color:var(--white)}
.stat-card .value.green{color:var(--leaf)}
.stat-card .value.amber{color:#FBBF24}

.section-title{font-size:11px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:rgba(255,255,255,.35);margin-bottom:12px}
.feed{display:flex;flex-direction:column;gap:10px;max-height:240px;overflow-y:auto}
.feed::-webkit-scrollbar{width:4px}
.feed::-webkit-scrollbar-thumb{background:rgba(255,255,255,.15);border-radius:2px}
.feed-item{display:flex;gap:12px;align-items:flex-start;padding:11px 14px;background:rgba(255,255,255,.04);border-radius:10px;border-left:3px solid transparent;animation:fadeIn .4s ease}
@keyframes fadeIn{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
.feed-item.event-lid_open{border-left-color:var(--leaf)}
.feed-item.event-done{border-left-color:var(--gold)}
.feed-item.event-esp32_offline,.feed-item.event-mqtt_disconnected,.feed-item.event-mqtt_offline{border-left-color:var(--red)}
.feed-item.event-cmd_sent{border-left-color:#60A5FA}
.feed-icon{font-size:15px;flex-shrink:0;margin-top:1px}
.feed-text{font-size:13px;color:rgba(255,255,255,.7);line-height:1.4;word-break:break-word}
.feed-time{font-size:11px;color:rgba(255,255,255,.3);margin-top:2px}


.dev-card{background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.08);border-radius:16px;padding:18px;display:flex;gap:14px;align-items:center}
.dev-avatar{width:50px;height:50px;border-radius:50%;background:var(--leaf);color:#0B1F16;font-weight:700;font-size:16px;display:flex;align-items:center;justify-content:center;flex-shrink:0}
.dev-info h3{color:#fff;font-size:15px;font-weight:700}
.dev-info .role{font-size:12px;color:rgba(255,255,255,.5);margin-top:1px}
.dev-links{display:flex;gap:8px;margin-top:8px;flex-wrap:wrap}
.dev-links a{font-size:11px;font-weight:600;color:var(--leaf);text-decoration:none;border:1px solid rgba(77,184,122,.35);padding:3px 11px;border-radius:999px;transition:.2s}
.dev-links a:hover{background:var(--leaf);color:#0B1F16}
.dev-tag{font-size:11px;color:rgba(255,255,255,.3);margin-top:8px}

/* ── RIGHT: Form panel ── */
.form-panel{background:var(--white);display:flex;flex-direction:column;overflow-y:auto}
.form-inner{padding:36px 32px 24px;flex:1;display:flex;flex-direction:column}
.form-header h2{font-family:'Playfair Display',serif;font-size:26px;color:var(--forest);margin-bottom:6px}
.form-header p{font-size:13px;color:var(--gray);line-height:1.55}
.steps{display:flex;gap:8px;margin-top:16px}
.step{flex:1;text-align:center;font-size:11px;font-weight:700;padding:8px 4px;border-radius:8px;background:#F3F4F6;color:var(--gray);transition:.3s}
.step.active{background:var(--forest);color:#fff}
.step.done{background:#BBF7D0;color:#166534}

.accepted-box{background:#F0FDF4;border:1px solid #BBF7D0;border-radius:12px;padding:13px 16px;margin:20px 0;font-size:13px;color:#166534;line-height:1.6}
.accepted-box strong{display:block;margin-bottom:3px}

.form-group{margin-bottom:16px}
.form-group label{display:block;font-size:13px;font-weight:700;color:var(--forest);margin-bottom:7px}
.form-group input{width:100%;padding:12px 15px;border:1.5px solid var(--lgray);border-radius:10px;font-size:15px;font-family:inherit;color:var(--ink);background:#fff;transition:.2s}
.form-group input:focus{outline:none;border-color:var(--mid);box-shadow:0 0 0 3px rgba(45,138,98,.1)}
.form-group .hint{font-size:12px;color:var(--gray);margin-top:5px}

.photo-label{display:block;font-size:13px;font-weight:700;color:var(--forest);margin:4px 0 10px}
.camera-box{background:var(--cream);border:2px dashed var(--lgray);border-radius:14px;height:190px;display:flex;align-items:center;justify-content:center;overflow:hidden;position:relative;transition:.3s}
.camera-box.active{border-color:var(--leaf);background:#F0FFF4}
.camera-box video,.camera-box img{width:100%;height:100%;object-fit:cover;border-radius:12px;background:#000}
.camera-placeholder{text-align:center;color:var(--gray);padding:10px}
.camera-placeholder span{font-size:34px;display:block;margin-bottom:6px}
.camera-placeholder p{font-size:13px}
.flash-overlay{position:absolute;inset:0;background:#fff;opacity:0;pointer-events:none;transition:opacity .1s;z-index:2}
.flash-overlay.active{opacity:.9}

.photo-actions{display:flex;gap:10px;margin-top:12px;flex-wrap:wrap}
.btn-ghost{flex:1;min-width:140px;padding:11px 14px;border-radius:10px;border:1.5px dashed #93C5FD;background:#EFF6FF;color:#1E40AF;font-weight:600;font-size:13.5px;cursor:pointer;font-family:inherit;transition:.2s;display:flex;align-items:center;justify-content:center;gap:8px}
.btn-ghost:hover{background:#1E40AF;border-color:#1E40AF;color:#fff}
.btn-ghost.cam{border-color:#86EFAC;background:#F0FDF4;color:#166534}
.btn-ghost.cam:hover{background:#16A34A;border-color:#16A34A;color:#fff}
.capture-controls,.retake-controls{display:flex;gap:10px;margin-top:12px}
.btn-solid{flex:1;padding:12px;border-radius:10px;border:none;background:var(--forest);color:#fff;font-weight:700;font-size:14px;cursor:pointer;font-family:inherit;transition:.2s}
.btn-solid:hover{background:var(--green)}
.btn-warn{flex:1;padding:12px;border-radius:10px;border:1.5px solid var(--lgray);background:#fff;color:var(--gray);font-weight:600;font-size:14px;cursor:pointer;font-family:inherit;transition:.2s}
.btn-warn:hover{border-color:var(--red);color:var(--red)}

.submit-btn{width:100%;padding:16px;background:var(--forest);color:#fff;border:none;border-radius:12px;font-size:16px;font-weight:700;font-family:inherit;cursor:pointer;transition:.2s;margin-top:18px;letter-spacing:.02em}
.submit-btn:hover:not(:disabled){background:var(--green);transform:translateY(-2px);box-shadow:0 8px 24px rgba(13,59,46,.25)}
.submit-btn:disabled{background:var(--lgray);color:var(--gray);cursor:not-allowed}

.status-bar{margin-top:18px;padding:14px 16px;border-radius:10px;font-size:14px;line-height:1.5;animation:fadeIn .3s ease}
.status-bar.loading{background:#FFF7ED;color:#92400E;border:1px solid #FDE68A;display:flex;align-items:center;gap:10px}
.status-bar.success{background:#F0FDF4;color:#166534;border:1px solid #BBF7D0}
.status-bar.error{background:#FEF2F2;color:#991B1B;border:1px solid #FECACA}
.status-bar.info{background:#EFF6FF;color:#1E40AF;border:1px solid #BFDBFE}
.spinner{width:18px;height:18px;border:2.5px solid #FDE68A;border-top-color:#92400E;border-radius:50%;animation:spin .8s linear infinite;flex-shrink:0}
@keyframes spin{to{transform:rotate(360deg)}}

.done-box{margin-top:18px;background:#F0FDF4;border:1px solid #BBF7D0;border-radius:12px;padding:20px;text-align:center;animation:fadeIn .4s ease}
.done-box .big{font-size:34px}
.done-box h3{color:#166534;font-size:17px;margin:8px 0 4px}
.done-box p{font-size:13px;color:#166534;margin-bottom:14px}
.download-btn{display:inline-block;padding:12px 26px;background:#16A34A;color:#fff;border-radius:10px;font-weight:700;font-size:14px;text-decoration:none;font-family:inherit;transition:.2s}
.download-btn:hover{background:#15803D;transform:translateY(-2px)}
.again-btn{display:inline-block;margin-left:10px;padding:12px 20px;background:#fff;color:#166534;border:1.5px solid #BBF7D0;border-radius:10px;font-weight:600;font-size:14px;cursor:pointer;font-family:inherit;transition:.2s}
.again-btn:hover{border-color:#16A34A}

.footer-note{text-align:center;font-size:11.5px;color:var(--gray);padding-top:18px}
.footer-note a{color:var(--mid);text-decoration:none;font-weight:600}

@media (max-width:860px){
  body{grid-template-columns:1fr;grid-template-rows:auto 1fr}
  .dashboard{padding:24px 18px;gap:18px}
  .form-inner{padding:26px 20px}
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
      <p>MQTT · IoT · Eco-Certify ☁️</p>
    </div>
  </div>

  <div class="pills-row">
    <div class="pill mqtt off" id="mqttPill"><div class="dot"></div><span id="mqttLabel">MQTT Offline</span></div>
    <div class="pill off" id="espPill"><div class="dot"></div><span id="espLabel">ESP32 Offline</span></div>
  </div>

  <div class="lid-hero">
    <div class="lid-icon-wrap">
      <div class="lid-icon-bg" id="lidIconBg"><span class="lid-emoji" id="lidEmoji">🗑️</span></div>
      <div class="lid-pulse"></div>
    </div>
    <div class="lid-status-label" id="lidLabel">LID CLOSED</div>
    <div class="lid-sub" id="lidSub">Waiting for next submission</div>
    <div class="lid-sub" style="margin-top:8px;font-size:12px;color:rgba(255,255,255,.25)" id="lidUpdated">—</div>
  </div>

  <div class="standalone-hint">🔘 The button on the bin opens it anytime — even without the server (standalone mode)</div>

  <div class="stats-row">
    <div class="stat-card"><div class="label">Today's Recycled</div><div class="value green" id="statTotal">0</div></div>
    <div class="stat-card"><div class="label">Last User</div><div class="value" id="statUser" style="font-size:16px;margin-top:4px">—</div></div>
    <div class="stat-card"><div class="label">Bin Status</div><div class="value amber" id="statProc" style="font-size:15px;margin-top:4px">Idle</div></div>
    <div class="stat-card"><div class="label">Last Updated</div><div class="value" id="statTime" style="font-size:15px;margin-top:4px">—</div></div>
  </div>

  <div>
    <div class="section-title">Live Activity Feed</div>
    <div class="feed" id="actFeed">
      <div class="feed-item">
        <span class="feed-icon">🟢</span>
        <div><div class="feed-text">System initialised — dashboard live</div><div class="feed-time" id="initTime"></div></div>
      </div>
    </div>
  </div>

  <div class="dev-card">
    <div class="dev-avatar">@@DEV_INITIALS@@</div>
    <div class="dev-info">
      <h3>@@DEV_NAME@@</h3>
      <div class="role">@@DEV_ROLE@@ · Smart E-Waste Bin</div>
      <div class="dev-links">
        <a href="@@DEV_GITHUB@@" target="_blank" rel="noopener">GitHub</a>
        <a href="@@DEV_LINKEDIN@@" target="_blank" rel="noopener">LinkedIn</a>
        <a href="mailto:@@DEV_EMAIL@@">Email</a>
      </div>
      <div class="dev-tag">Made with 💚 for a greener planet</div>
    </div>
  </div>

</aside>

<!-- ══════════ RIGHT: FORM PANEL ══════════ -->
<main class="form-panel">
<div class="form-inner">

  <div class="form-header">
    <h2>Deposit E-Waste</h2>
    <p>Fill in your details, take a photo, and the bin lid will open automatically via MQTT. You have <strong>30 seconds</strong> to deposit your e-waste — then the lid closes and your certificate is generated and emailed to you.</p>
    <div class="steps">
      <div class="step active">1 · Details</div>
      <div class="step">2 · Photo</div>
      <div class="step">3 · Submit</div>
    </div>
  </div>

  <div class="accepted-box">
    <strong>✅ Accepted Items</strong>
    Mobile phones, batteries, chargers, cables, circuit boards, keyboards, mice, remotes, and other small electronic gadgets.
  </div>

  <form id="ewasteForm">
    <div class="form-group">
      <label for="inp_name">Full Name</label>
      <input type="text" id="inp_name" required placeholder="e.g. Priya Sharma" maxlength="50">
    </div>
    <div class="form-group">
      <label for="inp_mobile">Mobile Number</label>
      <input type="tel" id="inp_mobile" required placeholder="+91 98765 43210" maxlength="16">
    </div>
    <div class="form-group">
      <label for="inp_email">Email Address</label>
      <input type="email" id="inp_email" required placeholder="you@example.com">
      <p class="hint">Your certificate will be sent here</p>
    </div>

    <label class="photo-label">📸 Your Photo (for the certificate)</label>
    <div class="camera-box" id="cameraBox">
      <div class="flash-overlay" id="flash"></div>
      <div class="camera-placeholder" id="camPlaceholder">
        <span>📷</span><p>Capture a live photo or upload from gallery</p>
      </div>
      <video id="camVideo" autoplay playsinline hidden></video>
      <img id="camPreview" alt="Your photo preview" hidden>
    </div>

    <div class="photo-actions" id="photoActions">
      <button type="button" class="btn-ghost cam" id="btnCam">📷 Live Camera</button>
      <button type="button" class="btn-ghost" id="btnGallery">🖼️ Upload from Gallery</button>
      <input type="file" id="galleryInput" accept="image/*" hidden>
    </div>

    <div class="capture-controls" id="captureControls" hidden>
      <button type="button" class="btn-solid" id="btnCapture">📸 Capture Photo</button>
      <button type="button" class="btn-warn" id="btnCancel">Cancel</button>
    </div>

    <div class="retake-controls" id="retakeControls" hidden>
      <button type="button" class="btn-warn" id="btnRetake">🔁 Retake Photo</button>
    </div>

    <button type="submit" class="submit-btn" id="submitBtn" disabled>Capture a Photo to Continue</button>
  </form>

  <div class="status-bar" id="statusBar" hidden></div>

  <div class="done-box" id="doneBox" hidden>
    <div class="big">🏆</div>
    <h3>Certificate Ready!</h3>
    <p id="doneText">Your certificate has been emailed to you.</p>
    <a class="download-btn" id="downloadBtn" href="#">📥 Download Certificate (PDF)</a>
    <button class="again-btn" id="resetBtn">Submit Another</button>
  </div>

  <div class="footer-note">© 2026 Smart E-Waste Bin · Secured &amp; hosted on <a href="https://render.com" target="_blank" rel="noopener">Render</a> ☁️</div>
</div>
</main>

<script>
// ── Helpers ──────────────────────────────────────────────────
const $ = id => document.getElementById(id);
const show = el => el.hidden = false;
const hide = el => el.hidden = true;
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function nowTime() {
  return new Date().toLocaleTimeString('en-IN', {hour:'2-digit',minute:'2-digit',second:'2-digit'});
}

// ── SSE: live dashboard updates ─────────────────────────────
const evtSource = new EventSource('/api/stream');

const ICONS = {
  init:'🟢', submission:'📋', cmd_sent:'📡', mqtt_connected:'☁️',
  mqtt_disconnected:'☁️', mqtt_offline:'🔴', esp32_status:'💓',
  lid_open:'🔓', lid_closed:'🔒', no_deposit:'⚠️',
  generating:'🖨️', sending_email:'📧', done:'🏆', email_failed:'⚠️', error:'❌',
};

function applyStatus(s) {
  const isOpen = s.lid === 'open';
  $('lidIconBg').className = 'lid-icon-bg' + (isOpen ? ' open' : '');
  $('lidEmoji').textContent  = isOpen ? '🔓' : '🗑️';
  $('lidLabel').textContent  = isOpen ? 'LID  OPEN' : 'LID  CLOSED';
  $('lidLabel').className    = 'lid-status-label' + (isOpen ? ' open' : '');
  $('lidSub').textContent    = s.last_event || '—';
  $('lidUpdated').textContent = 'Updated ' + (s.last_updated || '—');

  $('statTotal').textContent = s.total_today ?? 0;
  $('statUser').textContent  = s.last_user ?? '—';
  $('statProc').textContent  = s.processing ? 'Processing…' : 'Idle';
  $('statTime').textContent  = s.last_updated ?? '—';

  if (s.mqtt_connected) { $('mqttPill').classList.add('on'); $('mqttPill').classList.remove('off'); $('mqttLabel').textContent = 'MQTT Connected'; }
  else { $('mqttPill').classList.add('off'); $('mqttPill').classList.remove('on'); $('mqttLabel').textContent = 'MQTT Offline'; }

  if (s.esp32_online) { $('espPill').classList.add('on'); $('espPill').classList.remove('off'); $('espLabel').textContent = 'ESP32 Online'; }
  else { $('espPill').classList.add('off'); $('espPill').classList.remove('on'); $('espLabel').textContent = 'ESP32 Offline'; }
}

function addFeedItem(type, text) {
  const feed = $('actFeed');
  const div  = document.createElement('div');
  div.className = 'feed-item event-' + type;
  const icon = document.createElement('span');
  icon.className = 'feed-icon';
  icon.textContent = ICONS[type] || '•';
  const body = document.createElement('div');
  const txt  = document.createElement('div');
  txt.className = 'feed-text';
  txt.textContent = text;                 // ✅ textContent — no XSS
  const tm = document.createElement('div');
  tm.className = 'feed-time';
  tm.textContent = nowTime();
  body.appendChild(txt); body.appendChild(tm);
  div.appendChild(icon); div.appendChild(body);
  feed.insertBefore(div, feed.firstChild);
  while (feed.children.length > 20) feed.removeChild(feed.lastChild);
}

function showStatus(type, html) {
  const el = $('statusBar');
  el.className = 'status-bar ' + type;
  el.innerHTML = html;
  show(el);
}

// ── Step indicator ───────────────────────────────────────────
function setStep(n) {
  document.querySelectorAll('.step').forEach((s, i) => {
    s.classList.toggle('done',   i < n);
    s.classList.toggle('active', i === n);
  });
}

// ── Photo capture (webcam / gallery) ─────────────────────────
let capturedPhoto = null;   // File
let videoStream   = null;
let currentReqId  = null;
let submitted     = false;

function resetPhotoUI() {
  stopCamera();
  hide($('camVideo')); hide($('camPreview'));
  hide($('captureControls')); hide($('retakeControls'));
  show($('camPlaceholder')); show($('photoActions'));
  $('cameraBox').classList.remove('active');
  capturedPhoto = null;
  $('submitBtn').disabled = true;
  $('submitBtn').textContent = 'Capture a Photo to Continue';
}

function stopCamera() {
  if (videoStream) { videoStream.getTracks().forEach(t => t.stop()); videoStream = null; }
  hide($('camVideo'));
}

async function startCamera() {
  hide($('camPlaceholder')); hide($('photoActions'));
  show($('captureControls'));
  $('cameraBox').classList.add('active');
  setStep(1);
  showStatus('loading', '<div class="spinner"></div><span>Starting camera…</span>');
  try {
    videoStream = await navigator.mediaDevices.getUserMedia(
      { video: { facingMode: 'user', width: 640, height: 480 }, audio: false });
    $('camVideo').srcObject = videoStream;
    show($('camVideo'));
    await $('camVideo').play();
    $('statusBar').hidden = true;
  } catch (err) {
    showStatus('error', '❌ <strong>Camera error:</strong> ' + escapeHtml(err.message) + ' — please upload from gallery instead.');
    resetPhotoUI();
  }
}

async function capturePhoto() {
  let count = 3;
  showStatus('info', '📸 Smile! Capturing in <strong>' + count + '</strong>…');
  const timer = setInterval(() => {
    count--;
    if (count > 0) {
      showStatus('info', '📸 Smile! Capturing in <strong>' + count + '</strong>…');
    } else {
      clearInterval(timer);
      doCapture();
    }
  }, 1000);
}

function doCapture() {
  const flash = $('flash');
  flash.classList.add('active');
  setTimeout(() => flash.classList.remove('active'), 200);
  const vid = $('camVideo');
  const canvas = document.createElement('canvas');
  canvas.width  = vid.videoWidth  || 640;
  canvas.height = vid.videoHeight || 480;
  canvas.getContext('2d').drawImage(vid, 0, 0);
  stopCamera();
  canvas.toBlob(blob => {
    capturedPhoto = new File([blob], 'webcam-photo.jpg', { type: 'image/jpeg' });
    showPreview(URL.createObjectURL(blob));
  }, 'image/jpeg', 0.9);
}

function showPreview(src) {
  hide($('camPlaceholder')); hide($('captureControls'));
  $('camPreview').src = src;
  show($('camPreview'));
  show($('retakeControls'));
  $('cameraBox').classList.add('active');
  $('submitBtn').disabled = false;
  $('submitBtn').textContent = 'Submit & Open Bin 🔓';
  setStep(2);
  showStatus('success', '✅ <strong>Photo ready!</strong> Click <strong>Submit &amp; Open Bin</strong> when you are ready.');
}

$('btnCam').addEventListener('click', startCamera);
$('btnCancel').addEventListener('click', () => { resetPhotoUI(); setStep(0); });
$('btnCapture').addEventListener('click', capturePhoto);
$('btnRetake').addEventListener('click', () => { resetPhotoUI(); setStep(1); });
$('btnGallery').addEventListener('click', () => $('galleryInput').click());
$('galleryInput').addEventListener('change', e => {
  const file = e.target.files[0];
  if (!file) return;
  if (!file.type.startsWith('image/')) {
    showStatus('error', '❌ Please choose an image file.');
    return;
  }
  capturedPhoto = file;
  showPreview(URL.createObjectURL(file));
  $('galleryInput').value = '';
});

// ── Submit ───────────────────────────────────────────────────
$('ewasteForm').addEventListener('submit', async e => {
  e.preventDefault();
  if (submitted) return;
  const name   = $('inp_name').value.trim();
  const mobile = $('inp_mobile').value.trim();
  const email  = $('inp_email').value.trim();
  if (!name || !mobile || !email) { showStatus('error', '❌ Please fill in all your details.'); return; }
  if (!capturedPhoto)               { showStatus('error', '❌ Please capture or upload a photo first.'); return; }

  submitted = true;
  $('submitBtn').disabled = true;
  $('submitBtn').textContent = '⏳ Sending…';
  showStatus('loading', '<div class="spinner"></div><span>Uploading photo &amp; sending MQTT command to the bin…</span>');

  const fd = new FormData();
  fd.append('name', name);
  fd.append('mobile', mobile);
  fd.append('email', email);
  fd.append('photo', capturedPhoto);

  try {
    const res  = await fetch('/api/submit', { method: 'POST', body: fd });
    const data = await res.json();
    if (!data.success) throw new Error(data.error || 'Server error');
    currentReqId = data.request_id;
    showStatus('loading', '<div class="spinner"></div><span>📡 Command sent to bin — waiting for the lid to open…</span>');
  } catch (err) {
    showStatus('error', '❌ ' + escapeHtml(err.message));
    submitted = false;
    $('submitBtn').disabled = false;
    $('submitBtn').textContent = 'Submit & Open Bin 🔓';
  }
});

// ── 30-second lid countdown (deposit window) ────────────────
let lidCountdownTimer = null;
function startLidCountdown() {
  stopLidCountdown();
  let secs = 30;
  const tick = () => {
    if (secs > 0) {
      showStatus('success', '🔓 <strong>Lid is OPEN!</strong> Deposit your e-waste — lid closes in <strong>' + secs + 's</strong>…');
      secs--;
    } else {
      stopLidCountdown();
      showStatus('loading', '<div class="spinner"></div><span>Deposit window over — closing lid…</span>');
    }
  };
  tick();
  lidCountdownTimer = setInterval(tick, 1000);
}
function stopLidCountdown() {
  if (lidCountdownTimer) { clearInterval(lidCountdownTimer); lidCountdownTimer = null; }
}

// ── SSE event handling ───────────────────────────────────────
const FEED_MESSAGES = {
  init:              'Dashboard connected — live',
  mqtt_connected:    '☁️ MQTT broker connected',
  mqtt_disconnected: '☁️ MQTT broker disconnected — retrying…',
  mqtt_offline:      '🔴 MQTT offline — bin command could not be sent',
  submission:        '📋 New submission received',
  cmd_sent:          '📡 Open-lid command sent to bin via MQTT',
  lid_open:          '🔓 Lid opened — deposit your e-waste now',
  lid_closed:        '🔒 Lid closed — deposit confirmed, generating certificate',
  generating:        '🖨️ Generating your certificate…',
  sending_email:     '📧 Sending certificate to your email…',
  done:              '🏆 Certificate ready!',
  email_failed:      '⚠️ Certificate ready — email not configured, download below',
  error:             '❌ Processing error — please try again',
};

evtSource.onmessage = e => {
  const msg = JSON.parse(e.data);
  if (msg.status) applyStatus(msg.status);
  const s = msg.status || {};

  let text = FEED_MESSAGES[msg.type];
  if (msg.type === 'no_deposit') {
    text = msg.reason === 'bin_offline'
      ? '🔴 Bin did not respond — deposit not confirmed, NO certificate issued'
      : '🔴 Lid did not close — deposit not confirmed, NO certificate issued';
  } else if (msg.type === 'lid_open') {
    text = msg.request_id === 'standalone'
      ? '🔓 Lid opened via button (standalone — no certificate)'
      : FEED_MESSAGES.lid_open;
  } else if (msg.type === 'lid_closed') {
    text = msg.request_id === 'standalone'
      ? '🔒 Lid closed (standalone mode)'
      : FEED_MESSAGES.lid_closed;
  }
  if (!text && msg.type === 'esp32_status') {
    text = s.esp32_online ? '💓 ESP32 bin online (heartbeat)' : '🔴 ESP32 bin offline';
  }
  if (text) addFeedItem(msg.type, text);

  // Form-side progress — only for THIS session's request
  // (standalone button events are dashboard-only, they never issue certificates)
  const mine = currentReqId && (!msg.request_id || msg.request_id === currentReqId);
  if (!mine) return;
  switch (msg.type) {
    case 'lid_open':
      startLidCountdown(); break;
    case 'lid_closed':
      stopLidCountdown();
      showStatus('loading', '<div class="spinner"></div><span>✅ Deposit window complete — generating your certificate…</span>'); break;
    case 'no_deposit':
      stopLidCountdown();
      showStatus('error', msg.reason === 'bin_offline'
        ? '❌ <strong>Bin did not respond.</strong> Check ESP32 power &amp; WiFi. No certificate was issued — please try again.'
        : '❌ <strong>Lid did not close.</strong> Deposit not confirmed — no certificate was issued. Please try again.');
      submitted = false;
      $('submitBtn').disabled = false;
      $('submitBtn').textContent = 'Submit & Open Bin 🔓'; break;
    case 'generating':
      showStatus('loading', '<div class="spinner"></div><span>🖨️ Generating certificate…</span>'); break;
    case 'sending_email':
      showStatus('loading', '<div class="spinner"></div><span>📧 Sending certificate to your email…</span>'); break;
    case 'done':
      stopLidCountdown();
      showStatus('success', '🏆 <strong>Done!</strong> Certificate sent to your email.');
      showDone(msg.cert_url, true); break;
    case 'email_failed':
      stopLidCountdown();
      showStatus('info', msg.reason === 'not_configured'
        ? '⚠️ <strong>Certificate generated!</strong> Email is not configured on the server — download it below.'
        : '⚠️ <strong>Certificate generated!</strong> Email delivery failed — download it below.');
      showDone(msg.cert_url, false); break;
    case 'error':
      stopLidCountdown();
      showStatus('error', '❌ Processing error — please try again.');
      submitted = false;
      $('submitBtn').disabled = false;
      $('submitBtn').textContent = 'Submit & Open Bin 🔓'; break;
  }
};

evtSource.onerror = () => addFeedItem('mqtt_disconnected', 'SSE connection lost — retrying…');

function showDone(certUrl, emailed) {
  $('downloadBtn').href = certUrl || '#';
  $('doneText').textContent = emailed
    ? 'Your certificate has been emailed to you and is ready to download.'
    : 'Your certificate could not be emailed — download it here instead.';
  show($('doneBox'));
}

$('resetBtn').addEventListener('click', () => location.reload());

// ── Init ─────────────────────────────────────────────────────
$('initTime').textContent = nowTime();
setStep(0);
</script>
</body>
</html>
"""

# ==================== RUN ====================
if __name__ == "__main__":
    print("♻️  Smart E-Waste Dustbin Server — MQTT Edition")
    print(f"   🌐 Web UI      : http://0.0.0.0:{PORT}")
    print(f"   ☁️  MQTT broker : {MQTT_BROKER}:{MQTT_PORT}" + (" 🔐 TLS" if MQTT_TLS else "") + "  (lazy under Gunicorn — starts on first request)")
    print(f"   📤 cmd topic   : {TOPIC_CMD}")
    print(f"   📥 ack topic   : {TOPIC_ACK}")
    print(f"   📥 status topic: {TOPIC_STATUS}")
    print(f"   📧 Email       : {SENDER_EMAIL or '(not configured)'}")
    print(f"   👨‍💻 Developer   : {DEV_NAME}")
    ensure_mqtt_started()   # dev server: connect immediately (Gunicorn does it lazily)
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)

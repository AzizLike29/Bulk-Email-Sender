import os
import sqlite3
import time
import secrets
import requests
from urllib.parse import urlencode, urlparse
from werkzeug.utils import secure_filename
from flask import Flask, render_template, request, redirect, url_for, flash
from dotenv import load_dotenv

# Load config dari .env
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(override=True)

DB_PATH = os.getenv("DB_PATH", "/tmp/audience.sqlite3")

SENDER_EMAIL = os.getenv("SENDER_EMAIL", "")
SENDER_NAME = os.getenv("SENDER_NAME", "No-Reply")
REPLY_TO = os.getenv("REPLY_TO", "")
BASE_URL = (os.getenv("BASE_URL", "https://broadcast-email.up.railway.app")).rstrip("/")
BATCH_DELAY_SEC = float(os.getenv("BATCH_DELAY_SEC", "0.5"))
FLASK_SECRET = os.getenv("FLASK_SECRET", secrets.token_hex(16))

app = Flask(__name__)
app.config["SECRET_KEY"] = FLASK_SECRET

# Untuk membangun URL file upload statis
PUBLIC_BASE_URL = BASE_URL
app.config["PREFERRED_URL_SCHEME"] = "https"

# DB (SQLite)
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS subscribers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            name TEXT,
            token TEXT UNIQUE NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
    print("DB ready at", DB_PATH)

init_db()

def upsert_subscriber(email, name=None):
    email = (email or "").strip().lower()
    if not email:
        return False, "Email Kosong"
    token = secrets.token_urlsafe(24)
    with get_db() as con:
        try:
            con.execute(
                "INSERT OR IGNORE INTO subscribers (email, name, token, status) VALUES (?, ?, ?, 'active')",
                (email, name, token),
            )
            con.execute("UPDATE subscribers SET status='active' WHERE email=?", (email,))
            return True, None
        except sqlite3.Error as e:
            return False, str(e)

def unsubscribe_by_token(token):
    with get_db() as con:
        cur = con.execute("SELECT id FROM subscribers WHERE token=? LIMIT 1", (token,))
        row = cur.fetchone()
        if not row:
            return False
        con.execute("UPDATE subscribers SET status='unsubscribed' WHERE id=?", (row["id"],))
        return True

def get_active_emails():
    with get_db() as con:
        cur = con.execute("SELECT email, name, token FROM subscribers WHERE status='active' ORDER BY id DESC")
        return cur.fetchall()

# Helpers
def build_unsub_link(token):
    return f"{BASE_URL}{url_for('unsubscribe')}?{urlencode({'token': token})}"

UPLOAD_DIR = os.path.join(BASE_DIR, "static", "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

ALLOWED_EXTS = {"png", "jpg", "jpeg", "gif"}

def allowed_ext(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTS

def _local_path_from_image_url(image_url: str):
    """
    Jika image_url mengarah ke /static/uploads/... kembalikan path lokal absolut.
    Kalau bukan (mis. URL eksternal), kembalikan None.
    """
    try:
        p = urlparse(image_url or "")
        path = p.path or ""
        if path.startswith("/static/uploads/"):
            return os.path.join(BASE_DIR, path.lstrip("/"))
    except Exception:
        pass
    return None

# Email via SendGrid Web API
def send_one_email(to_email, subject, html_body, unsub_http_link,
                   inline_image_path=None, image_cid="promoimg"):
    """
    Kirim email via SendGrid Web API (tanpa SMTP).
    - API key ambil dari env: SMTP_PASS
    - Jika ada file upload lokal, kirim sebagai inline attachment (CID) → pakai src="cid:promoimg" di template.
    """
    SENDGRID_API_KEY = os.getenv("SMTP_PASS")
    if not SENDGRID_API_KEY:
        raise RuntimeError("SendGrid API key tidak ditemukan di SMTP_PASS")
    if not SENDER_EMAIL:
        raise RuntimeError("SENDER_EMAIL tidak diset (dan harus cocok dengan verified sender di SendGrid)")

    url = "https://api.sendgrid.com/v3/mail/send"
    headers = {
        "Authorization": f"Bearer {SENDGRID_API_KEY}",
        "Content-Type": "application/json",
    }

    data = {
        "personalizations": [{
            "to": [{"email": to_email}],
            "subject": subject,
            "headers": {"List-Unsubscribe": f"<{unsub_http_link}>"}
        }],
        "from": {"email": SENDER_EMAIL, "name": SENDER_NAME},
        "content": [{
            "type": "text/html",
            "value": html_body
        }]
    }

    # Reply-To opsional
    if REPLY_TO:
        data["reply_to"] = {"email": REPLY_TO}

    resp = requests.post(url, headers=headers, json=data, timeout=30)
    # SendGrid sukses → 202
    if resp.status_code not in (200, 202):
        raise RuntimeError(f"SendGrid error {resp.status_code}: {resp.text}")

# Routes
@app.get("/")
def index():
    active_count = len(get_active_emails())
    return render_template(
        "index.html",
        active_count=active_count,
        sender_email=SENDER_EMAIL,
        sender_name=SENDER_NAME
    )

@app.get("/subscribe")
def subscribe_form():
    return render_template("subscribe.html")

@app.get("/healthz")
def healthz():
    return "ok", 200

@app.post("/subscribe")
def subscribe_submit():
    name = request.form.get("name", "").strip() or None
    email = request.form.get("email", "").strip()
    ok, err = upsert_subscriber(email, name)
    if not ok:
        flash(f"Gagal: {err}", "error")
        return redirect(url_for("subscribe_form"))
    flash("Berhasil subscribe / diperbarui", "success")
    return redirect(url_for("subscribe_form"))

@app.get("/unsubscribe")
def unsubscribe():
    token = request.args.get("token", "")
    if not token:
        return render_template("unsubscribed.html", ok=False)
    ok = unsubscribe_by_token(token)
    return render_template("unsubscribed.html", ok=ok)

@app.post("/send")
def send_route():
    subject = (request.form.get("subject") or "").strip()
    raw_body = request.form.get("body_html") or ""
    raw_recipients = request.form.get("recipients") or ""
    use_audience = request.form.get("use_audience") == "on"
    mode = request.form.get("mode", "send")  # 'test' atau 'send'
    test_email = (request.form.get("test_email") or "").strip()

    # Validasi env penting sebelum kirim
    if not os.getenv("SMTP_PASS"):
        flash("Gagal: SMTP_PASS (SendGrid API Key) belum diset di Variables.", "error")
        return redirect(url_for("index"))
    if not SENDER_EMAIL:
        flash("Gagal: SENDER_EMAIL belum diset (harus verified di SendGrid).", "error")
        return redirect(url_for("index"))

    # Kumpulkan penerima manual
    recipients = set()
    for part in raw_recipients.replace(";", ",").split(","):
        e = part.strip()
        if e:
            recipients.add(e.lower())

    # Tambahkan subscriber aktif jika dipilih
    audience = []
    if use_audience:
        audience = get_active_emails()
        for row in audience:
            recipients.add(row["email"].lower())

    # Mode test
    if mode == "test":
        if not test_email:
            flash("Masukkan alamat 'Test to' dulu.", "error")
            return redirect(url_for("index"))
        recipients = {test_email.lower()}

    if not recipients:
        flash("Tidak ada penerima.", "error")
        return redirect(url_for("index"))

    # Ambil image_url dari form (hasil upload ke /static/uploads/...)
    image_url_form = request.form.get("image_url")
    local_img_path = _local_path_from_image_url(image_url_form) if image_url_form else None

    sent_ok, sent_fail = [], []
    token_map = {row["email"].lower(): row["token"] for row in audience}

    for to_email in sorted(recipients):
        token = token_map.get(to_email, secrets.token_urlsafe(16))
        unsub_link = build_unsub_link(token)

        image_src = image_url_form

        html = render_template(
            "email_templates/promo.html",
            body_html=raw_body,
            unsub_link=unsub_link,
            image_src=image_src,
        )

        try:
            send_one_email(
                to_email,
                subject,
                html,
                unsub_link,
                inline_image_path=local_img_path,
                image_cid="promoimg",
            )
            sent_ok.append(to_email)
        except Exception as e:
            sent_fail.append((to_email, str(e)))

        time.sleep(BATCH_DELAY_SEC)

    return render_template(
        "success.html",
        subject=subject,
        sent_ok=sent_ok,
        sent_fail=sent_fail
    )

@app.post("/upload")
def upload():
    file = request.files.get("image")
    if not file or file.filename == "":
        return {"error": "No file"}, 400

    if not allowed_ext(file.filename):
        return {"error": "Invalid file type"}, 400

    # nama unik agar tidak bentrok
    name, ext = os.path.splitext(secure_filename(file.filename))
    uniq = f"{int(time.time())}_{secrets.token_hex(4)}"
    filename = f"{name[:40]}_{uniq}{ext.lower()}"

    path = os.path.join(UPLOAD_DIR, filename)
    file.save(path)

    # URL publik ke static
    rel = url_for("static", filename=f"uploads/{filename}")
    url = f"{PUBLIC_BASE_URL}{rel}" if PUBLIC_BASE_URL else url_for(
        "static", filename=f"uploads/{filename}", _external=True
    )
    return {"url": url}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    print(f"Starting Flask on 0.0.0.0:{port}", flush=True)
    app.run(host="0.0.0.0", port=port)
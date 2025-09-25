import os
import sqlite3
import time
import secrets
import requests
from urllib.parse import urlencode
from werkzeug.utils import secure_filename
from flask import Flask, render_template, request, redirect, url_for, flash
from dotenv import load_dotenv
import base64, mimetypes

# Email
import smtplib, ssl
from email.message import EmailMessage
from email.utils import formataddr

# Config
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(override=True)

# /tmp/audience.sqlite3
DB_PATH = os.getenv("DB_PATH", "/tmp/audience.sqlite3")

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "0") or "0")
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")

SENDER_EMAIL = os.getenv("SENDER_EMAIL", "")
SENDER_NAME  = os.getenv("SENDER_NAME", "No-Reply")
REPLY_TO     = os.getenv("REPLY_TO", "")
BASE_URL     = (os.getenv("BASE_URL", "https://broadcast-email.up.railway.app")).rstrip("/")
BATCH_DELAY_SEC = float(os.getenv("BATCH_DELAY_SEC", "0.5"))
FLASK_SECRET    = os.getenv("FLASK_SECRET", secrets.token_hex(16))

app = Flask(__name__)
app.config["SECRET_KEY"] = FLASK_SECRET
PUBLIC_BASE_URL = BASE_URL
app.config["PREFERRED_URL_SCHEME"] = "https"

# Cloudinary
CLOUDINARY_CLOUD_NAME = os.getenv("CLOUDINARY_CLOUD_NAME", "")
CLOUDINARY_API_KEY    = os.getenv("CLOUDINARY_API_KEY", "")
CLOUDINARY_API_SECRET = os.getenv("CLOUDINARY_API_SECRET", "")

CLOUDINARY_ENABLED = bool(CLOUDINARY_CLOUD_NAME and CLOUDINARY_API_KEY and CLOUDINARY_API_SECRET)
if CLOUDINARY_ENABLED:
    import cloudinary
    import cloudinary.uploader
    cloudinary.config(
        cloud_name=CLOUDINARY_CLOUD_NAME,
        api_key=CLOUDINARY_API_KEY,
        api_secret=CLOUDINARY_API_SECRET,
        secure=True,
    )

# Database
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS subscribers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                name TEXT,
                token TEXT UNIQUE NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
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
        cur = con.execute(
            "SELECT email, name, token FROM subscribers WHERE status='active' ORDER BY id DESC"
        )
        return cur.fetchall()

# Helpers
def build_unsub_link(token):
    return f"{BASE_URL}{url_for('unsubscribe')}?{urlencode({'token': token})}"

UPLOAD_DIR = os.path.join(BASE_DIR, "static", "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

ALLOWED_EXTS = {"png", "jpg", "jpeg", "gif"}
def allowed_ext(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTS

# Fetch image dari URL untuk embed inline (CID)
def _fetch_image_for_inline(url: str):
    try:
        head = requests.head(url, timeout=10, allow_redirects=True)
        if head.status_code == 200 and head.headers.get("Content-Type","").startswith("image/"):
            ct = head.headers["Content-Type"]
            get = requests.get(url, timeout=20)
            if get.status_code == 200 and get.content:
                b64 = base64.b64encode(get.content).decode("ascii")
                ext = mimetypes.guess_extension(ct.split(";")[0]) or ".jpg"
                return {"content": b64, "type": ct, "filename": f"hero{ext}"}
    except Exception:
        pass
    return None

# Kirim email via SMTP
def send_one_email(to_email, subject, html_body, unsub_http_link, attachments=None):
    if not (SMTP_HOST and SMTP_PORT and SMTP_USER and SMTP_PASS):
        raise RuntimeError("Konfigurasi SMTP tidak lengkap: isi SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS.")

    msg = EmailMessage()
    msg["From"] = formataddr((SENDER_NAME, SENDER_EMAIL or SMTP_USER))
    msg["To"] = to_email
    msg["Subject"] = subject
    if REPLY_TO:
        msg["Reply-To"] = REPLY_TO
    if unsub_http_link:
        msg["List-Unsubscribe"] = f"<{unsub_http_link}>"

    msg.set_content("Versi teks. Gunakan klien email yang mendukung HTML.")
    msg.add_alternative(html_body, subtype="html")

    if attachments:
        for att in attachments:
            ctype = att.get("type", "application/octet-stream")
            maintype, subtype = ctype.split("/", 1)
            raw = base64.b64decode(att["content"])
            filename = att.get("filename", "file")
            cid = att.get("content_id") or att.get("cid")
            if att.get("disposition") == "inline" and cid:
                msg.get_payload()[-1].add_related(
                    raw, maintype=maintype, subtype=subtype, cid=cid, filename=filename
                )
            else:
                msg.add_attachment(raw, maintype=maintype, subtype=subtype, filename=filename)

    if SMTP_PORT == 465:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=context) as server:
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg, from_addr=(SENDER_EMAIL or SMTP_USER), to_addrs=[to_email])
    else:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.ehlo()
            try:
                server.starttls()
                server.ehlo()
            except Exception:
                pass
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg, from_addr=(SENDER_EMAIL or SMTP_USER), to_addrs=[to_email])

# Routes
@app.get("/")
def index():
    active_count = len(get_active_emails())
    return render_template(
        "index.html",
        active_count=active_count,
        sender_email=SENDER_EMAIL,
        sender_name=SENDER_NAME,
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
    subject        = (request.form.get("subject") or "").strip()
    raw_body       = request.form.get("body_html") or ""
    raw_recipients = request.form.get("recipients") or ""
    use_audience   = request.form.get("use_audience") == "on"
    mode           = request.form.get("mode", "send")
    test_email     = (request.form.get("test_email") or "").strip()

    if not SMTP_PASS:
        flash("Gagal: SMTP_PASS (password email) belum diset.", "error")
        return redirect(url_for("index"))
    if not SENDER_EMAIL:
        flash("Gagal: SENDER_EMAIL belum diset.", "error")
        return redirect(url_for("index"))

    recipients = set()
    for part in raw_recipients.replace(";", ",").split(","):
        e = part.strip()
        if e:
            recipients.add(e.lower())

    audience = []
    if use_audience:
        audience = get_active_emails()
        for row in audience:
            recipients.add(row["email"].lower())

    if mode == "test":
        if not test_email:
            flash("Masukkan alamat 'Test to' dulu.", "error")
            return redirect(url_for("index"))
        recipients = {test_email.lower()}

    if not recipients:
        flash("Tidak ada penerima.", "error")
        return redirect(url_for("index"))

    image_url_form = (request.form.get("image_url") or "").strip()
    image_src = image_url_form if image_url_form else None

    # Cache gambar inline sekali
    inline_cache = _fetch_image_for_inline(image_src) if image_src else None
    cid_id = "heroimg"

    sent_ok, sent_fail = [], []
    token_map = {row["email"].lower(): row["token"] for row in audience}

    for to_email in sorted(recipients):
        token = token_map.get(to_email, secrets.token_urlsafe(16))
        unsub_link = build_unsub_link(token)

        attachments = None
        use_cid = False
        if inline_cache:
            attachments = [{
                "content": inline_cache["content"],
                "type": inline_cache["type"],
                "filename": inline_cache["filename"],
                "disposition": "inline",
                "content_id": cid_id,
            }]
            use_cid = True

        html = render_template(
            "email_templates/promo.html",
            body_html=raw_body,
            unsub_link=unsub_link,
            image_src=(f"cid:{cid_id}" if use_cid else image_src)
        )

        try:
            send_one_email(to_email, subject, html, unsub_link, attachments=attachments)
            sent_ok.append(to_email)
        except Exception as e:
            sent_fail.append((to_email, str(e)))

        time.sleep(BATCH_DELAY_SEC)

    return render_template(
        "success.html", subject=subject, sent_ok=sent_ok, sent_fail=sent_fail
    )

@app.post("/upload")
def upload():
    file = request.files.get("image")
    if not file or file.filename == "":
        return {"error": "No file"}, 400

    if not allowed_ext(file.filename):
        return {"error": "Invalid file type"}, 400

    if CLOUDINARY_ENABLED:
        try:
            res = cloudinary.uploader.upload(
                file,
                folder="email-assets",
                resource_type="image",
                use_filename=True,
                unique_filename=True,
                overwrite=False,
            )
            url = res.get("secure_url")
            if not url:
                return {"error": "Upload gagal"}, 500
            return {"url": url}
        except Exception as e:
            return {"error": f"Cloudinary error: {e}"}, 500

    name, ext = os.path.splitext(secure_filename(file.filename))
    uniq = f"{int(time.time())}_{secrets.token_hex(4)}"
    filename = f"{name[:40]}_{uniq}{ext.lower()}"
    path = os.path.join(UPLOAD_DIR, filename)
    file.save(path)
    rel = url_for("static", filename=f"uploads/{filename}")
    url = f"{PUBLIC_BASE_URL}{rel}" if PUBLIC_BASE_URL else url_for(
        "static", filename=f"uploads/{filename}", _external=True
    )
    return {"url": url}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    print(f"Starting Flask on 0.0.0.0:{port}", flush=True)
    app.run(host="0.0.0.0", port=port)
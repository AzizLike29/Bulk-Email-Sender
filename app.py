import os
import sqlite3
import smtplib
import ssl
import time
import secrets
from email.message import EmailMessage
from urllib.parse import urlencode
from werkzeug.utils import secure_filename
from flask import Flask, render_template, request, redirect, url_for, flash
from dotenv import load_dotenv

# Load config dari .env
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "audience.sqlite3")

load_dotenv(override=True)

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")

SENDER_EMAIL = os.getenv("SENDER_EMAIL", "")
SENDER_NAME = os.getenv("SENDER_NAME", "No-Reply")
BASE_URL = os.getenv("BASE_URL", "http://127.0.0.1:5000")
BATCH_DELAY_SEC = float(os.getenv("BATCH_DELAY_SEC", "0.5"))
FLASK_SECRET = os.getenv("FLASK_SECRET", secrets.token_hex(16))

app = Flask(__name__)
app.config["SECRET_KEY"] = FLASK_SECRET

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

# Email helpers
def build_unsub_link(token):
    return f"{BASE_URL}{url_for('unsubscribe')}?{urlencode({'token': token})}"

def send_one_email(smtp, to_email, subject, html_body, unsub_http_link):
    msg = EmailMessage()
    msg["From"] = f"{SENDER_NAME} <{SENDER_EMAIL}>"
    msg["To"] = to_email
    msg["Subject"] = subject
    msg["List-Unsubscribe"] = f"<{unsub_http_link}>"
    msg.set_content("This email requires an HTML-capable client to display properly.")
    msg.add_alternative(html_body, subtype="html")
    smtp.send_message(msg)

def connect_smtp():
    context = ssl.create_default_context()
    server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30)
    server.ehlo()
    server.starttls(context=context)
    server.ehlo()
    if SMTP_USER and SMTP_PASS:
        server.login(SMTP_USER, SMTP_PASS)
    return server

# Routes
@app.get("/")
def index():
    active_count = len(get_active_emails())
    return render_template("index.html",
                           active_count=active_count,
                           sender_email=SENDER_EMAIL,
                           sender_name=SENDER_NAME)

@app.get("/subscribe")
def subscribe_form():
    return render_template("subscribe.html")

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

    # Kumpulkan penerima
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

    sent_ok, sent_fail = [], []
    try:
        with connect_smtp() as smtp:
            token_map = {row["email"].lower(): row["token"] for row in audience}
            for to_email in sorted(recipients):
                token = token_map.get(to_email, secrets.token_urlsafe(16))
                unsub_link = build_unsub_link(token)

                html = render_template("email_templates/promo.html",
                                       body_html=raw_body, unsub_link=unsub_link)
                try:
                    send_one_email(smtp, to_email, subject, html, unsub_link)
                    sent_ok.append(to_email)
                except Exception as e:
                    sent_fail.append((to_email, str(e)))
                time.sleep(BATCH_DELAY_SEC)
    except Exception as e:
        flash(f"Gagal koneksi SMTP: {e}", "error")
        return redirect(url_for("index"))

    return render_template("success.html",
                           subject=subject,
                           sent_ok=sent_ok,
                           sent_fail=sent_fail)

@app.post("/upload")
def upload():
    file = request.files["image"]
    filename = secure_filename(file.filename)
    path = os.path.join("static/uploads", filename)
    file.save(path)
    return {"url": url_for("static", filename=f"uploads/{filename}", _external=True)}

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
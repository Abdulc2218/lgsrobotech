import csv
import io
import json
import os
import re
import smtplib
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

from flask import (Flask, Response, abort, flash, g, redirect,
                   render_template, request, send_from_directory, session,
                   url_for)

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "robotech.db"
UPLOAD_DIR = BASE_DIR / "uploads"   # legacy receipts stored on disk pre-cloud

ALLOWED_RECEIPT_EXT = {".png", ".jpg", ".jpeg", ".webp", ".pdf"}
RECEIPT_MIME = {".png": "image/png", ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg", ".webp": "image/webp",
                ".pdf": "application/pdf"}
MAX_RECEIPT_MB = 4   # keep under Vercel's ~4.5 MB request-body limit


def _load_local_secrets():
    """Local development: load secrets.local.json (git-ignored) into the
    environment. In production (Render) the real environment variables are
    already set and take precedence."""
    path = BASE_DIR / "secrets.local.json"
    if path.exists():
        for key, value in json.loads(path.read_text(encoding="utf-8")).items():
            os.environ.setdefault(key, str(value))


_load_local_secrets()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_RECEIPT_MB * 1024 * 1024

def _env(name, default=""):
    """Read an env var and strip stray whitespace/newlines — a trailing newline
    pasted into a hosting dashboard would otherwise break HTTP headers, etc."""
    return os.environ.get(name, default).strip()


# All secrets come from environment variables — never commit them to git.
ADMIN_PASSWORD = _env("ADMIN_PASSWORD", "change-me")
app.secret_key = _env("SECRET_KEY", "dev-only-secret")

# Public URL of the live site (used in emails for the status-check link).
SITE_URL = _env("SITE_URL", "https://lgsrobotech.vercel.app").rstrip("/")

# Email: Brevo HTTP API in production (BREVO_API_KEY), Gmail SMTP locally.
BREVO_API_KEY = _env("BREVO_API_KEY")
SENDER_EMAIL = _env("SENDER_EMAIL")
SMTP_HOST = _env("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(_env("SMTP_PORT", "587") or "587")
SMTP_USER = _env("SMTP_USER")
SMTP_PASSWORD = _env("SMTP_PASSWORD")

# Database: Postgres when DATABASE_URL is set (Render + Neon), else SQLite.
DATABASE_URL = _env("DATABASE_URL")
IS_POSTGRES = bool(DATABASE_URL)
if IS_POSTGRES:
    import psycopg
    from psycopg.rows import dict_row

# ---------------------------------------------------------------------------
# Event data (from the official Robotech 6.0 invitation)
# ---------------------------------------------------------------------------

CHALLENGES = {
    "game-dev": {
        "name": "Game Development Challenge",
        "fee": 3000,
        "icon": "gamepad",
        "desc": "Design and develop an original playable digital game combining "
                "programming, storytelling, graphics and problem-solving.",
        "specs": ["Original game by the team", "Presented within competition requirements"],
    },
    "robo-war": {
        "name": "Robo War",
        "fee": 3500,
        "icon": "bot",
        "desc": "Build a custom remote-controlled fighting robot and battle to disable, "
                "push out, or outlast opponents in an enclosed arena.",
        "specs": ["Optimus Prime: max 8 kg, 10×10×10 in", "Jet Fire: max 5 kg, 8×8×8 in",
                  "Store-bought devices disqualified", "No sharp blades, flames or explosives"],
    },
    "goal-bot": {
        "name": "Goal-bot Challenge (Robot Soccer)",
        "fee": 3500,
        "icon": "ball",
        "desc": "Design a robot that chases a ball and scores goals against opponents "
                "on a marked field in timed rounds.",
        "specs": ["Max 2.5–3 kg, 30×30×25 cm", "Simple front scoop/pusher",
                  "Wireless remote control"],
    },
    "bot-prix": {
        "name": "Bot Prix",
        "fee": 3000,
        "icon": "flag",
        "desc": "A high-speed robotics race — complete the designated track in the "
                "shortest possible time.",
        "specs": ["Max 2.5–3 kg, 30×30×25 cm", "6V–12V battery, DC geared motors",
                  "IR/ultrasonic sensors allowed"],
    },
    "tin-can-titan": {
        "name": "Tin Can Titan — Exhibition of IoT",
        "fee": 3000,
        "icon": "radio",
        "desc": "Showcase a working IoT device built from recycled materials. "
                "Theme: “Recycle to Upcycle — Smart Solutions from Waste”.",
        "specs": ["Working IoT concept or prototype", "Body/structure from recycled materials",
                  "Be ready to explain working & innovation"],
    },
    "app-dev": {
        "name": "App Development Challenge",
        "fee": 3000,
        "icon": "smartphone",
        "desc": "Design and develop a useful, innovative application that addresses "
                "a real-world need.",
        "specs": ["Original work by the team", "Present the working app and its features",
                  "Bring your own laptop, software & internet"],
    },
    "100-code": {
        "name": "100 Minutes of Code",
        "fee": 3000,
        "icon": "timer",
        "desc": "A fast-paced programming sprint — solve coding problems within 100 minutes. "
                "Languages: Python and C.",
        "specs": ["Optimus Prime: moderate–advanced problems",
                  "Jet Fire: beginner-friendly (loops, conditionals)",
                  "Bring your own laptop & internet"],
    },
    "ai-entrepreneurship": {
        "name": "AI-Powered Entrepreneurship",
        "fee": 3000,
        "icon": "lightbulb",
        "desc": "Pitch an innovative business idea that turns recycled materials into "
                "something valuable, with AI meaningfully involved.",
        "specs": ["5-minute pitch per team", "Prototype required (video, images or sample)",
                  "Pitch must cover problem, solution, features, competition, audience, feasibility"],
    },
    "digital-art": {
        "name": "Digital Art — Pen Tablet Challenge",
        "fee": 3000,
        "icon": "palette",
        "desc": "Create an original digital illustration live at the event using a pen tablet.",
        "specs": ["Artwork created during the competition", "No copied or pre-made work",
                  "Bring your own laptop & software"],
    },
    "web-dev": {
        "name": "Web Development Challenge",
        "fee": 3000,
        "icon": "globe",
        "desc": "Design and build an attractive, functional, user-friendly website "
                "based on the given requirements.",
        "specs": ["Tools: Canva, HTML, Google Sites, WordPress",
                  "Original, functional work", "Bring your own laptop & internet"],
    },
    "tug-of-bots": {
        "name": "Tug of Bots",
        "fee": 3000,
        "icon": "link",
        "desc": "A head-to-head battle of strength — pull the opposing robot beyond "
                "the boundary line.",
        "specs": ["Max 3 kg, 30×30×25 cm", "6V–12V battery",
                  "Strong hook/loop for the rope", "Forward & backward movement"],
    },
}

CATEGORIES = {
    "jet-fire": "Jet Fire (Grades V–VII)",
    "optimus-prime": "Optimus Prime (Grades VIII–X M / XI O)",
}

EVENT_INFO = {
    "date": "Saturday, 3 October 2026",
    "time": "12:00 pm – 6:00 pm",
    "venue": "LGS Wapda Town, Punjab Society Branch, 19-C-II PGECHS, Lahore",
    "deadline": "20 September 2026",
    "email": "lgswtrobotech@gmail.com",
    "instagram": "robotechlgs",
    "bank": "Askari Bank — Account No. 03201650001721",
    "contact_person": "M Usman (0323 8409150)",
    "contact_info": "Miss Arooba Khalid (0321-7114141)",
}

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def _q(sql):
    """Translate '?' placeholders to Postgres '%s' when needed."""
    return sql.replace("?", "%s") if IS_POSTGRES else sql


class DBConn:
    """Thin adapter so every route works identically on SQLite and Postgres."""

    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, params=()):
        cur = self.conn.cursor()
        cur.execute(_q(sql), params)
        return cur

    def executemany(self, sql, seq_of_params):
        cur = self.conn.cursor()
        cur.executemany(_q(sql), seq_of_params)
        return cur

    def commit(self):
        self.conn.commit()

    def close(self):
        self.conn.close()


def get_db():
    if "db" not in g:
        if IS_POSTGRES:
            conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
        else:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
        g.db = DBConn(conn)
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


SCHEMA_SQLITE = """
    CREATE TABLE IF NOT EXISTS registrations (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        school      TEXT NOT NULL,
        focal_name  TEXT NOT NULL,
        phone       TEXT NOT NULL,
        email       TEXT NOT NULL,
        total_fee   INTEGER NOT NULL,
        txn_ref     TEXT,
        receipt_file TEXT,
        receipt_mime TEXT,
        receipt_data BLOB,
        status      TEXT NOT NULL DEFAULT 'pending',
        confirm_email_sent TEXT,
        created_at  TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS teams (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        registration_id INTEGER NOT NULL REFERENCES registrations(id) ON DELETE CASCADE,
        challenge       TEXT NOT NULL,
        category        TEXT NOT NULL,
        member1         TEXT NOT NULL,
        member2         TEXT,
        fee             INTEGER NOT NULL
    );
"""

SCHEMA_POSTGRES = """
    CREATE TABLE IF NOT EXISTS registrations (
        id          SERIAL PRIMARY KEY,
        school      TEXT NOT NULL,
        focal_name  TEXT NOT NULL,
        phone       TEXT NOT NULL,
        email       TEXT NOT NULL,
        total_fee   INTEGER NOT NULL,
        txn_ref     TEXT,
        receipt_file TEXT,
        receipt_mime TEXT,
        receipt_data BYTEA,
        status      TEXT NOT NULL DEFAULT 'pending',
        confirm_email_sent TEXT,
        created_at  TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS teams (
        id              SERIAL PRIMARY KEY,
        registration_id INTEGER NOT NULL REFERENCES registrations(id) ON DELETE CASCADE,
        challenge       TEXT NOT NULL,
        category        TEXT NOT NULL,
        member1         TEXT NOT NULL,
        member2         TEXT,
        fee             INTEGER NOT NULL
    );
"""


def init_db():
    if IS_POSTGRES:
        with psycopg.connect(DATABASE_URL) as conn:
            conn.execute(SCHEMA_POSTGRES)
            # Migrate cloud databases created before newer columns existed.
            conn.execute("ALTER TABLE registrations ADD COLUMN IF NOT EXISTS txn_ref TEXT")
            conn.commit()
        return
    with sqlite3.connect(DB_PATH) as db:
        db.executescript(SCHEMA_SQLITE)
        # Migrate local databases created before newer features.
        cols = {row[1] for row in db.execute("PRAGMA table_info(registrations)")}
        for col, decl in [("txn_ref", "TEXT"),
                          ("receipt_file", "TEXT"), ("receipt_mime", "TEXT"),
                          ("receipt_data", "BLOB"),
                          ("status", "TEXT NOT NULL DEFAULT 'pending'"),
                          ("confirm_email_sent", "TEXT")]:
            if col not in cols:
                db.execute(f"ALTER TABLE registrations ADD COLUMN {col} {decl}")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html", challenges=CHALLENGES, info=EVENT_INFO,
                           categories=CATEGORIES)


@app.route("/register", methods=["GET", "POST"])
def register():
    errors = []
    form = request.form

    if request.method == "POST":
        school = form.get("school", "").strip()
        focal_name = form.get("focal_name", "").strip()
        phone = form.get("phone", "").strip()
        email = form.get("email", "").strip()
        txn_ref = form.get("txn_ref", "").strip()

        if not school:
            errors.append("School name is required.")
        if not focal_name:
            errors.append("Focal person's name is required.")
        phone_digits = re.sub(r"\D", "", phone)
        if not re.fullmatch(r"(03\d{9}|923\d{9}|00923\d{9})", phone_digits):
            errors.append("Please enter a valid mobile number, e.g. "
                          "0300 1234567 or +92 300 1234567.")
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}", email or ""):
            errors.append("Please enter a valid email address.")
        if len(txn_ref) < 4:
            errors.append("Please enter the Transaction ID / Reference Number "
                          "from your bank transfer (found on the receipt).")

        challenges = form.getlist("challenge[]")
        categories = form.getlist("category[]")
        members1 = form.getlist("member1[]")
        members2 = form.getlist("member2[]")

        teams = []
        for i, (ch, cat, m1, m2) in enumerate(
                zip(challenges, categories, members1, members2), start=1):
            ch, cat, m1, m2 = ch.strip(), cat.strip(), m1.strip(), m2.strip()
            if not (ch or m1 or m2):
                continue  # fully empty row — ignore
            if ch not in CHALLENGES:
                errors.append(f"Team {i}: please select a challenge.")
                continue
            if cat not in CATEGORIES:
                errors.append(f"Team {i}: please select a category.")
                continue
            if not m1:
                errors.append(f"Team {i}: at least one participant name is required.")
                continue
            teams.append({"challenge": ch, "category": cat, "member1": m1,
                          "member2": m2 or None, "fee": CHALLENGES[ch]["fee"]})

        if not teams and not errors:
            errors.append("Please add at least one team.")

        receipt = request.files.get("receipt")
        receipt_ext = None
        if receipt is None or not receipt.filename:
            errors.append("Please upload a screenshot of your payment transaction — "
                          "registration is only complete with proof of payment.")
        else:
            receipt_ext = Path(receipt.filename).suffix.lower()
            if receipt_ext not in ALLOWED_RECEIPT_EXT:
                errors.append("The payment screenshot must be a PNG, JPG, WEBP image or a PDF.")

        if not errors:
            total = sum(t["fee"] for t in teams)
            receipt_bytes = receipt.read()
            db = get_db()
            insert_sql = (
                "INSERT INTO registrations (school, focal_name, phone, email, "
                "total_fee, txn_ref, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)")
            params = (school, focal_name, phone, email, total, txn_ref,
                      datetime.now().isoformat(timespec="seconds"))
            if IS_POSTGRES:
                reg_id = db.execute(insert_sql + " RETURNING id",
                                    params).fetchone()["id"]
            else:
                reg_id = db.execute(insert_sql, params).lastrowid
            # The receipt lives in the database so it survives redeploys on
            # hosts with ephemeral disks.
            receipt_name = f"WTR-{reg_id:04d}{receipt_ext}"
            db.execute("UPDATE registrations SET receipt_file = ?, "
                       "receipt_mime = ?, receipt_data = ? WHERE id = ?",
                       (receipt_name,
                        RECEIPT_MIME.get(receipt_ext, "application/octet-stream"),
                        receipt_bytes, reg_id))
            db.executemany(
                "INSERT INTO teams (registration_id, challenge, category, member1, member2, fee) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [(reg_id, t["challenge"], t["category"], t["member1"], t["member2"], t["fee"])
                 for t in teams],
            )
            db.commit()
            return redirect(url_for("success", reg_id=reg_id))

    return render_template("register.html", challenges=CHALLENGES,
                           categories=CATEGORIES, info=EVENT_INFO,
                           errors=errors, form=form)


@app.route("/success/<int:reg_id>")
def success(reg_id):
    db = get_db()
    reg = db.execute("SELECT * FROM registrations WHERE id = ?", (reg_id,)).fetchone()
    if reg is None:
        return redirect(url_for("index"))
    teams = db.execute(
        "SELECT * FROM teams WHERE registration_id = ? ORDER BY id", (reg_id,)
    ).fetchall()
    return render_template("success.html", reg=reg, teams=teams,
                           challenges=CHALLENGES, categories=CATEGORIES,
                           info=EVENT_INFO)


@app.route("/status")
def check_status():
    """Public status checker — a school enters its WTR-#### id and sees whether
    the registration is pending, accepted, or rejected."""
    raw = request.args.get("ref", "").strip()
    if not raw:
        return render_template("check_status.html", info=EVENT_INFO)

    # Accept "WTR-0001", "wtr 1", "0001", or "1".
    digits = re.sub(r"\D", "", raw)
    reg = None
    if digits:
        db = get_db()
        reg = db.execute("SELECT * FROM registrations WHERE id = ?",
                         (int(digits),)).fetchone()
    if reg is None:
        return render_template(
            "check_status.html", info=EVENT_INFO, query=raw,
            not_found=True)

    teams = get_db().execute(
        "SELECT * FROM teams WHERE registration_id = ? ORDER BY id",
        (reg["id"],)).fetchall()
    return render_template("check_status.html", info=EVENT_INFO, query=raw,
                           reg=reg, teams=teams, challenges=CHALLENGES,
                           categories=CATEGORIES)


@app.route("/receipt/<int:reg_id>")
def receipt(reg_id):
    db = get_db()
    row = db.execute(
        "SELECT receipt_file, receipt_mime, receipt_data FROM registrations "
        "WHERE id = ?", (reg_id,)).fetchone()
    if row is None or not row["receipt_file"]:
        abort(404)
    if row["receipt_data"]:
        return Response(
            bytes(row["receipt_data"]),
            mimetype=row["receipt_mime"] or "application/octet-stream",
            headers={"Content-Disposition":
                     f'inline; filename="{row["receipt_file"]}"'})
    # Legacy receipts saved to disk before database storage existed.
    if (UPLOAD_DIR / row["receipt_file"]).exists():
        return send_from_directory(UPLOAD_DIR, row["receipt_file"])
    abort(404)


# ---------------------------------------------------------------------------
# Organizer admin panel
# ---------------------------------------------------------------------------

def admin_logged_in():
    return session.get("admin") is True


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    error = None
    if request.method == "POST":
        if request.form.get("password", "") == ADMIN_PASSWORD:
            session["admin"] = True
            return redirect(url_for("admin"))
        error = "Incorrect password."
    return render_template("admin_login.html", error=error, info=EVENT_INFO)


@app.route("/admin/logout")
def admin_logout():
    session.pop("admin", None)
    return redirect(url_for("index"))


@app.route("/admin")
def admin():
    if not admin_logged_in():
        return redirect(url_for("admin_login"))
    db = get_db()
    regs = db.execute("SELECT * FROM registrations ORDER BY id DESC").fetchall()
    teams_by_reg = {}
    for t in db.execute("SELECT * FROM teams ORDER BY id").fetchall():
        teams_by_reg.setdefault(t["registration_id"], []).append(t)

    challenge_counts = {key: 0 for key in CHALLENGES}
    for row in db.execute(
            "SELECT challenge, COUNT(*) AS n FROM teams GROUP BY challenge"):
        challenge_counts[row["challenge"]] = row["n"]

    stats = {
        "registrations": len(regs),
        "teams": sum(len(v) for v in teams_by_reg.values()),
        "fees_total": sum(r["total_fee"] for r in regs),
        "fees_verified": sum(r["total_fee"] for r in regs if r["status"] == "verified"),
        "pending": sum(1 for r in regs if r["status"] == "pending"),
    }
    return render_template("admin.html", regs=regs, teams_by_reg=teams_by_reg,
                           stats=stats, challenge_counts=challenge_counts,
                           challenges=CHALLENGES, categories=CATEGORIES,
                           info=EVENT_INFO)


def _send_via_smtp(to_addr, subject, body):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"Robotech 6.0 — LGS Wapda Town <{SENDER_EMAIL or SMTP_USER}>"
    msg["To"] = to_addr
    msg.set_content(body)
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as smtp:
            smtp.starttls()
            smtp.login(SMTP_USER, SMTP_PASSWORD)
            smtp.send_message(msg)
        return True, None
    except Exception as exc:
        return False, str(exc)


def _send_via_brevo(to_addr, subject, body):
    """Send via the Brevo HTTP API — works on hosts that block SMTP ports."""
    payload = {
        "sender": {"name": "Robotech 6.0 — LGS Wapda Town",
                   "email": SENDER_EMAIL or SMTP_USER},
        "to": [{"email": to_addr}],
        "subject": subject,
        "textContent": body,
    }
    req = urllib.request.Request(
        "https://api.brevo.com/v3/smtp/email",
        data=json.dumps(payload).encode(),
        headers={"api-key": BREVO_API_KEY,
                 "content-type": "application/json",
                 "accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            resp.read()
        return True, None
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        return False, f"Brevo API error {exc.code}: {detail}"
    except Exception as exc:
        return False, str(exc)


def send_confirmation_email(reg, teams, sent_at):
    """Send the registration-confirmed email. Returns (ok, error_message)."""
    if not BREVO_API_KEY and not (SMTP_USER and SMTP_PASSWORD):
        return False, ("email sending is not configured — set BREVO_API_KEY "
                       "or SMTP_USER/SMTP_PASSWORD in the environment")

    team_lines = []
    for i, t in enumerate(teams, 1):
        members = t["member1"] + (f", {t['member2']}" if t["member2"] else "")
        team_lines.append(f"  {i}. {CHALLENGES[t['challenge']]['name']} "
                          f"[{CATEGORIES[t['category']]}] — {members}")

    body = f"""Dear {reg['focal_name']},

Great news! Your payment has been verified and your registration for
ROBOTECH CHALLENGE 6.0 is now CONFIRMED.

Your Registration ID is WTR-{reg['id']:04d}.
You can check the live status of your registration anytime on our website:
  {SITE_URL}/status
Just open that link and enter your Registration ID: WTR-{reg['id']:04d}

------------------------------------------------------------
Registration ID : WTR-{reg['id']:04d}
School          : {reg['school']}
Fee received    : Rs {reg['total_fee']:,}
Transaction ref : {reg['txn_ref'] or 'N/A'}
Confirmed on    : {sent_at.replace("T", " at ")}

Registered teams:
{chr(10).join(team_lines)}

Event details:
  Date  : {EVENT_INFO['date']}
  Time  : {EVENT_INFO['time']}
  Venue : {EVENT_INFO['venue']}

Important reminders:
  - Teams must bring their own laptops/devices with all required software
    (Arduino IDE, Photoshop, Unity, etc.) pre-installed.
  - Every participant and faculty advisor must submit a signed liability
    waiver at the registration desk before the opening ceremony.
  - Formal attire (Eastern or Western) is mandatory.
  - Each school must send at least one chaperone.

Follow @{EVENT_INFO['instagram']} on Instagram for updates and announcements.
Questions? Contact {EVENT_INFO['contact_person']} or {EVENT_INFO['contact_info']}.

We look forward to seeing your teams on event day!

Team ROBOTECH
LGS Wapda Town
"""
    subject = f"Robotech 6.0 — Registration WTR-{reg['id']:04d} Confirmed"
    if BREVO_API_KEY:
        return _send_via_brevo(reg["email"], subject, body)
    return _send_via_smtp(reg["email"], subject, body)


def send_rejection_email(reg):
    """Send the registration-declined email. Returns (ok, error_message)."""
    if not BREVO_API_KEY and not (SMTP_USER and SMTP_PASSWORD):
        return False, ("email sending is not configured — set BREVO_API_KEY "
                       "or SMTP_USER/SMTP_PASSWORD in the environment")
    body = f"""Dear {reg['focal_name']},

Thank you for registering {reg['school']} for ROBOTECH CHALLENGE 6.0.

After reviewing your submission, we are sorry to inform you that your
registration could NOT be accepted at this time.

Your Registration ID is WTR-{reg['id']:04d}.
You can check the live status of your registration anytime on our website:
  {SITE_URL}/status
Just open that link and enter your Registration ID: WTR-{reg['id']:04d}

------------------------------------------------------------
Registration ID : WTR-{reg['id']:04d}
School          : {reg['school']}
Transaction ref : {reg['txn_ref'] or 'N/A'}

This usually happens when the payment could not be verified against our
bank records, the transaction reference / screenshot did not match, or the
required details were incomplete.

Please do not worry — this may be fixable. Contact us to find out the exact
reason and how to correct it:

  {EVENT_INFO['contact_person']}
  {EVENT_INFO['contact_info']}
  Email: {EVENT_INFO['email']}

If you believe this was a mistake, reply to this email with your payment
proof and transaction reference number and we will review it again.

Team ROBOTECH
LGS Wapda Town
"""
    subject = f"Robotech 6.0 — Registration WTR-{reg['id']:04d} Not Accepted"
    if BREVO_API_KEY:
        return _send_via_brevo(reg["email"], subject, body)
    return _send_via_smtp(reg["email"], subject, body)


def deliver_confirmation(db, reg):
    """Send the confirmation email for a registration and stamp/flash the result."""
    teams = db.execute(
        "SELECT * FROM teams WHERE registration_id = ? ORDER BY id",
        (reg["id"],)).fetchall()
    sent_at = datetime.now().isoformat(timespec="seconds")
    ok, err = send_confirmation_email(reg, teams, sent_at)
    if ok:
        db.execute("UPDATE registrations SET confirm_email_sent = ? WHERE id = ?",
                   (sent_at, reg["id"]))
        db.commit()
        flash(f"WTR-{reg['id']:04d} — confirmation email sent to {reg['email']}.",
              "ok")
    else:
        flash(f"WTR-{reg['id']:04d} — confirmation email was NOT sent: {err}",
              "warn")


def deliver_rejection(db, reg):
    """Send the declined email and flash the result."""
    ok, err = send_rejection_email(reg)
    if ok:
        flash(f"WTR-{reg['id']:04d} — declined; a notification email was sent "
              f"to {reg['email']}.", "ok")
    else:
        flash(f"WTR-{reg['id']:04d} — declined, but the email was NOT sent: {err}",
              "warn")


@app.route("/admin/status/<int:reg_id>", methods=["POST"])
def admin_set_status(reg_id):
    if not admin_logged_in():
        return redirect(url_for("admin_login"))
    status = request.form.get("status")
    if status in ("pending", "verified", "rejected"):
        db = get_db()
        reg = db.execute("SELECT * FROM registrations WHERE id = ?",
                         (reg_id,)).fetchone()
        if reg is None:
            return redirect(url_for("admin"))
        old_status = reg["status"]
        db.execute("UPDATE registrations SET status = ? WHERE id = ?",
                   (status, reg_id))
        db.commit()
        if status == "verified":
            if reg["confirm_email_sent"]:
                # Re-sending an identical email gets blocked by Gmail as spam;
                # the organizer can use "Resend Email" deliberately instead.
                flash(f"WTR-{reg_id:04d} verified — confirmation email was "
                      f"already sent on "
                      f"{reg['confirm_email_sent'].replace('T', ' at ')}, so it "
                      f"was not sent again. Use “Resend Email” if the school "
                      f"needs another copy.", "ok")
            else:
                deliver_confirmation(db, reg)
        elif status == "rejected" and old_status != "rejected":
            # Notify the school only on the transition into "rejected",
            # so repeated clicks don't spam them.
            deliver_rejection(db, reg)
    return redirect(url_for("admin"))


@app.route("/admin/resend/<int:reg_id>", methods=["POST"])
def admin_resend_email(reg_id):
    if not admin_logged_in():
        return redirect(url_for("admin_login"))
    db = get_db()
    reg = db.execute("SELECT * FROM registrations WHERE id = ?",
                     (reg_id,)).fetchone()
    if reg is None or reg["status"] != "verified":
        flash("Only verified registrations can receive a confirmation email.",
              "warn")
    else:
        deliver_confirmation(db, reg)
    return redirect(url_for("admin"))


@app.route("/admin/export.csv")
def admin_export():
    if not admin_logged_in():
        return redirect(url_for("admin_login"))
    db = get_db()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Registration ID", "School", "Focal Person", "Phone", "Email",
                     "Challenge", "Category", "Participant 1", "Participant 2",
                     "Team Fee", "Transaction Ref", "Payment Status",
                     "Receipt File", "Submitted"])
    rows = db.execute(
        "SELECT r.*, t.challenge, t.category, t.member1, t.member2, t.fee AS team_fee "
        "FROM registrations r JOIN teams t ON t.registration_id = r.id "
        "ORDER BY r.id, t.id").fetchall()
    for row in rows:
        writer.writerow([
            f"WTR-{row['id']:04d}", row["school"], row["focal_name"], row["phone"],
            row["email"], CHALLENGES[row["challenge"]]["name"],
            CATEGORIES[row["category"]], row["member1"], row["member2"] or "",
            row["team_fee"], row["txn_ref"] or "", row["status"],
            row["receipt_file"] or "MISSING", row["created_at"],
        ])
    return Response(
        "\ufeff" + buf.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition":
                 "attachment; filename=robotech6_registrations.csv"})


@app.errorhandler(413)
def file_too_large(e):
    return render_template(
        "register.html", challenges=CHALLENGES, categories=CATEGORIES,
        info=EVENT_INFO, form={},
        errors=[f"The uploaded screenshot is too large — maximum size is "
                f"{MAX_RECEIPT_MB} MB. Please compress it and try again."]), 413


_db_ready = False


def ensure_db():
    """Initialize the schema once, lazily, so a database hiccup can't crash
    the whole app at import time (which on serverless hosts 500s every page)."""
    global _db_ready
    if _db_ready:
        return
    try:
        init_db()
        _db_ready = True
    except Exception as exc:
        app.logger.error("init_db failed: %s", exc)


@app.before_request
def _init_db_before_request():
    ensure_db()


try:
    ensure_db()
except Exception:
    pass


if __name__ == "__main__":
    app.run(debug=True)

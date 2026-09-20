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
SITE_URL = _env("SITE_URL", "https://lgs-sportsfest.vercel.app").rstrip("/")

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
# Event data (from the official LGS WT Sports Fest 2026 invitation)
# ---------------------------------------------------------------------------

# Fees are PER PLAYER, by how many games that player plays (max 2 games each).
FEE_ONE_GAME = 1500
FEE_TWO_GAMES = 2000
MAX_GAMES_PER_PLAYER = 2
REG_PREFIX = "SF"   # registration ids look like SF-0001


def reg_code(reg_id):
    return f"{REG_PREFIX}-{reg_id:04d}"

SPORTS = {
    "volleyball": {
        "name": "Volleyball",
        "team_size": 6,
        "icon": "volleyball",
        "desc": "6-a-side. Send the ball over the net within 3 touches; a point is "
                "scored on every rally.",
        "rules": ["Each team has 6 players", "Up to 3 touches before returning the ball",
                  "Matches to 25 points, win by 2", "Rally scoring — a point every rally",
                  "Faults: out of bounds, touching the net, or more than 3 touches"],
    },
    "throwball": {
        "name": "Throwball",
        "team_size": 7,
        "icon": "throwball",
        "desc": "7-a-side. Throw the ball over the net into the opponent's court using "
                "your hands.",
        "rules": ["Each team has 7 players", "Throw with hands; brief catch & hold allowed",
                  "Up to 3 touches before returning", "Serve from behind the baseline",
                  "Matches to 15 or 21 points, win by 2"],
    },
    "futsal": {
        "name": "Futsal",
        "team_size": 5,
        "icon": "ball",
        "desc": "5-a-side indoor football with a smaller low-bounce ball — fast passing "
                "and ball control.",
        "rules": ["5 players including a goalkeeper", "Smaller, low-bounce ball",
                  "Two 20-minute halves", "No offside; kick-ins replace throw-ins",
                  "Unlimited substitutions during play"],
    },
    "dodgeball": {
        "name": "Dodgeball",
        "team_size": 6,
        "icon": "dodgeball",
        "desc": "6-a-side. Eliminate opponents by hitting them (below the shoulders) or "
                "catching their throw.",
        "rules": ["Each team has 6 players", "Soft rubber balls; hit below the shoulders",
                  "A hit player is eliminated", "Catch a throw → thrower is out & revive a teammate",
                  "Eliminate all opponents to win"],
    },
    "tug-of-war": {
        "name": "Tug of War",
        "team_size": 8,
        "icon": "tug",
        "desc": "Two teams pull the rope from opposite ends — drag the opposing team "
                "across the line.",
        "rules": ["Two teams pull from opposite ends", "Pull the opponents across the line",
                  "Game begins on the official signal", "No illegal moves"],
    },
    "arm-wrestling": {
        "name": "Arm Wrestling",
        "team_size": 1,
        "icon": "arm",
        "desc": "One-on-one. Pin your opponent's hand to the pad, elbows staying on the "
                "designated pads.",
        "rules": ["Individual event (1 player)", "Pin the opponent's hand to the pad",
                  "Elbows stay on the pads throughout", "Match starts on the referee's signal",
                  "Win the required number of rounds to win"],
    },
    "table-tennis-singles": {
        "name": "Table Tennis — Singles",
        "team_size": 1,
        "icon": "ping-pong",
        "desc": "One-on-one. Serve diagonally and outplay your opponent across the net.",
        "rules": ["Individual event (1 player)", "Serve diagonally into the service court",
                  "Let the ball bounce once before returning", "Point on a failed legal return",
                  "First to the required score wins"],
    },
    "table-tennis-doubles": {
        "name": "Table Tennis — Doubles",
        "team_size": 2,
        "icon": "table-tennis-double",
        "desc": "2-a-side. Partners hit alternately, serving diagonally in the correct "
                "rotation.",
        "rules": ["Each team has 2 players", "Serve diagonally into the service court",
                  "Partners hit the ball alternately", "Follow the correct serve & rotation order",
                  "First to the required score wins"],
    },
}

EVENT_INFO = {
    "name": "LGS WT Sports Fest 2026",
    "subtitle": "Inter-School Sports Fest 2026",
    "date": "Saturday, 26 September 2026",
    "venue": "LGS Wapda Town — Punjab Campus, Lahore",
    "deadline": "24 September 2026",
    "email": "lgswtrobotech@gmail.com",
    "instagram": "",
    "bank": "Askari Bank — Account No. 03201650001721",
    "contact_person": "Muhammad Usman (0323 8409150)",
    "contact_info": "Abdul Qayyum (0321 4091541)",
    "fee_one": FEE_ONE_GAME,
    "fee_two": FEE_TWO_GAMES,
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
    CREATE TABLE IF NOT EXISTS players (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        registration_id INTEGER NOT NULL REFERENCES registrations(id) ON DELETE CASCADE,
        sport           TEXT NOT NULL,
        name            TEXT NOT NULL,
        father_name     TEXT,
        dob             TEXT,
        cnic            TEXT
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
    CREATE TABLE IF NOT EXISTS players (
        id              SERIAL PRIMARY KEY,
        registration_id INTEGER NOT NULL REFERENCES registrations(id) ON DELETE CASCADE,
        sport           TEXT NOT NULL,
        name            TEXT NOT NULL,
        father_name     TEXT,
        dob             TEXT,
        cnic            TEXT
    );
"""


def init_db():
    if IS_POSTGRES:
        with psycopg.connect(DATABASE_URL) as conn:
            conn.execute(SCHEMA_POSTGRES)
            conn.commit()
        return
    with sqlite3.connect(DB_PATH) as db:
        db.executescript(SCHEMA_SQLITE)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html", sports=SPORTS, info=EVENT_INFO)


def player_key(p):
    """A person is identified by their CNIC/B-Form (digits) if given, else name."""
    digits = re.sub(r"\D", "", p["cnic"])
    return digits if digits else "name:" + p["name"].lower()


def compute_fees(players):
    """Return (total_fee, per_player_games) — one player pays by how many
    distinct sports (games) they are entered in, capped at MAX_GAMES."""
    games_by_person = {}
    for p in players:
        games_by_person.setdefault(player_key(p), set()).add(p["sport"])
    total = 0
    for key, sports in games_by_person.items():
        total += FEE_TWO_GAMES if len(sports) >= 2 else FEE_ONE_GAME
    return total, games_by_person


def sport_names(sport_keys):
    """'Volleyball & Futsal' — sport display names in SPORTS order."""
    return " & ".join(SPORTS[k]["name"] for k in SPORTS if k in sport_keys)


def earlier_games_by_person(db, keys):
    """Sports each person (by CNIC digits) is already entered in through
    registrations received earlier. Rejected registrations don't count."""
    rows = db.execute(
        "SELECT p.sport, p.cnic FROM players p "
        "JOIN registrations r ON r.id = p.registration_id "
        "WHERE r.status != 'rejected'").fetchall()
    earlier = {}
    for row in rows:
        key = re.sub(r"\D", "", row["cnic"] or "")
        if key in keys:
            earlier.setdefault(key, set()).add(row["sport"])
    return earlier


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
            errors.append("Institution name is required.")
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

        p_sport = form.getlist("player_sport[]")
        p_name = form.getlist("player_name[]")
        p_father = form.getlist("player_father[]")
        p_dob = form.getlist("player_dob[]")
        p_cnic = form.getlist("player_cnic[]")

        players = []
        for i, (sp, nm, fa, db_, cn) in enumerate(
                zip(p_sport, p_name, p_father, p_dob, p_cnic), start=1):
            sp, nm, fa, db_, cn = (sp.strip(), nm.strip(), fa.strip(),
                                   db_.strip(), cn.strip())
            if not (nm or fa or cn):
                continue  # blank row — ignore
            if sp not in SPORTS:
                errors.append(f"Player {i}: please choose a valid sport.")
                continue
            if not nm:
                errors.append(f"Player {i} ({SPORTS[sp]['name']}): name is required.")
                continue
            if not fa:
                errors.append(f"{nm}: father's name is required.")
            if not db_:
                errors.append(f"{nm}: date of birth is required.")
            cnic_digits = re.sub(r"\D", "", cn)
            if len(cnic_digits) != 13:
                errors.append(f"{nm}: CNIC / B-Form must be 13 digits "
                              f"(e.g. 35201-1234567-8).")
            players.append({"sport": sp, "name": nm, "father_name": fa,
                            "dob": db_, "cnic": cn})

        if not players and not errors:
            errors.append("Please add at least one player to at least one sport.")

        # Enforce each sport's team size (max players).
        per_sport = {}
        for p in players:
            per_sport[p["sport"]] = per_sport.get(p["sport"], 0) + 1
        for sp, n in per_sport.items():
            limit = SPORTS[sp]["team_size"]
            if n > limit:
                errors.append(f"{SPORTS[sp]['name']}: a maximum of {limit} "
                              f"player{'' if limit == 1 else 's'} is allowed.")

        # Max 2 games per player (identified by CNIC / B-Form) — within this
        # submission and together with registrations already received.
        total, games_by_person = compute_fees(players)
        name_by_key = {}
        for p in players:
            name_by_key.setdefault(player_key(p), p["name"])
        for key, games in games_by_person.items():
            if len(games) > MAX_GAMES_PER_PLAYER:
                errors.append(f"{name_by_key[key]} is entered in {len(games)} sports "
                              f"({sport_names(games)}) — each player may play at "
                              f"most {MAX_GAMES_PER_PLAYER}.")
        cnic_keys = {k for k in games_by_person if k.isdigit()}
        if cnic_keys:
            earlier = earlier_games_by_person(get_db(), cnic_keys)
            for key, old in earlier.items():
                games = games_by_person[key]
                if (len(games) <= MAX_GAMES_PER_PLAYER
                        and len(old | games) > MAX_GAMES_PER_PLAYER):
                    errors.append(f"{name_by_key[key]} is already registered for "
                                  f"{sport_names(old)} in an earlier registration — "
                                  f"adding {sport_names(games - old)} would exceed "
                                  f"the limit of {MAX_GAMES_PER_PLAYER} sports per "
                                  f"player.")

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
            receipt_name = f"{reg_code(reg_id)}{receipt_ext}"
            db.execute("UPDATE registrations SET receipt_file = ?, "
                       "receipt_mime = ?, receipt_data = ? WHERE id = ?",
                       (receipt_name,
                        RECEIPT_MIME.get(receipt_ext, "application/octet-stream"),
                        receipt_bytes, reg_id))
            db.executemany(
                "INSERT INTO players (registration_id, sport, name, father_name, "
                "dob, cnic) VALUES (?, ?, ?, ?, ?, ?)",
                [(reg_id, p["sport"], p["name"], p["father_name"], p["dob"], p["cnic"])
                 for p in players],
            )
            db.commit()
            return redirect(url_for("success", reg_id=reg_id))

    return render_template("register.html", sports=SPORTS, info=EVENT_INFO,
                           errors=errors, form=form)


def load_entries(reg_id):
    """Return the players of a registration grouped by sport, in SPORTS order."""
    players = get_db().execute(
        "SELECT * FROM players WHERE registration_id = ? ORDER BY id",
        (reg_id,)).fetchall()
    by_sport = {}
    for p in players:
        by_sport.setdefault(p["sport"], []).append(p)
    entries = [{"sport": key, "players": by_sport[key]}
               for key in SPORTS if key in by_sport]
    return entries, players


@app.route("/success/<int:reg_id>")
def success(reg_id):
    db = get_db()
    reg = db.execute("SELECT * FROM registrations WHERE id = ?", (reg_id,)).fetchone()
    if reg is None:
        return redirect(url_for("index"))
    entries, _ = load_entries(reg_id)
    return render_template("success.html", reg=reg, entries=entries,
                           reg_code=reg_code(reg["id"]), sports=SPORTS,
                           info=EVENT_INFO)


@app.route("/status")
def check_status():
    """Public status checker — a school enters its SF-#### id and sees whether
    the registration is pending, accepted, or rejected."""
    raw = request.args.get("ref", "").strip()
    if not raw:
        return render_template("check_status.html", info=EVENT_INFO)

    # Accept "SF-0001", "sf 1", "0001", or "1".
    digits = re.sub(r"\D", "", raw)
    reg = None
    if digits:
        db = get_db()
        reg = db.execute("SELECT * FROM registrations WHERE id = ?",
                         (int(digits),)).fetchone()
    if reg is None:
        return render_template(
            "check_status.html", info=EVENT_INFO, query=raw, not_found=True)

    entries, _ = load_entries(reg["id"])
    return render_template("check_status.html", info=EVENT_INFO, query=raw,
                           reg=reg, entries=entries,
                           reg_code=reg_code(reg["id"]), sports=SPORTS)


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
    all_regs = db.execute("SELECT * FROM registrations ORDER BY id DESC").fetchall()

    all_players = db.execute("SELECT * FROM players ORDER BY id").fetchall()
    entries_by_reg = {}          # reg_id -> [{sport, players}] in SPORTS order
    players_by_reg = {}          # reg_id -> flat list
    for p in all_players:
        players_by_reg.setdefault(p["registration_id"], []).append(p)
    for rid, plist in players_by_reg.items():
        grouped = {}
        for p in plist:
            grouped.setdefault(p["sport"], []).append(p)
        entries_by_reg[rid] = [{"sport": k, "players": grouped[k]}
                               for k in SPORTS if k in grouped]

    sport_counts = {key: 0 for key in SPORTS}
    for p in all_players:
        if p["sport"] in sport_counts:
            sport_counts[p["sport"]] += 1

    stats = {
        "registrations": len(all_regs),
        "players": len(all_players),
        "fees_total": sum(r["total_fee"] for r in all_regs),
        "fees_verified": sum(r["total_fee"] for r in all_regs if r["status"] == "verified"),
        "pending": sum(1 for r in all_regs if r["status"] == "pending"),
    }

    # Optional search. A pure-number or "SF-####" query is treated as an exact
    # registration-ID lookup; anything else is a case-insensitive substring
    # match across institution / focal person / email / phone / transaction ref
    # and player names.
    q = request.args.get("q", "").strip()
    if q:
        id_match = re.fullmatch(r"(?i)\s*(?:sf[-\s]?)?0*(\d+)\s*", q)
        if id_match:
            rid = int(id_match.group(1))
            regs = [r for r in all_regs if r["id"] == rid]
        else:
            ql = q.lower()
            regs = []
            for r in all_regs:
                fields = [r["school"], r["focal_name"], r["email"], r["phone"],
                          r["txn_ref"]]
                fields += [p["name"] for p in players_by_reg.get(r["id"], [])]
                if ql in " ".join(str(x or "").lower() for x in fields):
                    regs.append(r)
    else:
        regs = all_regs

    return render_template("admin.html", regs=regs, entries_by_reg=entries_by_reg,
                           stats=stats, sport_counts=sport_counts, sports=SPORTS,
                           reg_code=reg_code, info=EVENT_INFO, q=q,
                           total=len(all_regs))


EMAIL_FROM_NAME = "LGS WT Sports Fest 2026"


def _send_via_smtp(to_addr, subject, body):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{EMAIL_FROM_NAME} <{SENDER_EMAIL or SMTP_USER}>"
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
        "sender": {"name": EMAIL_FROM_NAME, "email": SENDER_EMAIL or SMTP_USER},
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


def send_confirmation_email(reg, entries, sent_at):
    """Send the registration-confirmed email. Returns (ok, error_message)."""
    if not BREVO_API_KEY and not (SMTP_USER and SMTP_PASSWORD):
        return False, ("email sending is not configured — set BREVO_API_KEY "
                       "or SMTP_USER/SMTP_PASSWORD in the environment")
    code = reg_code(reg["id"])
    sport_lines = []
    for e in entries:
        names = ", ".join(p["name"] for p in e["players"])
        sport_lines.append(f"  • {SPORTS[e['sport']]['name']} "
                           f"({len(e['players'])}): {names}")

    body = f"""Dear {reg['focal_name']},

Great news! Your payment has been verified and your registration for the
LGS WT SPORTS FEST 2026 is now CONFIRMED.

Your Registration ID is {code}.
You can check the live status of your registration anytime on our website:
  {SITE_URL}/status
Just open that link and enter your Registration ID: {code}

------------------------------------------------------------
Registration ID : {code}
Institution     : {reg['school']}
Fee received    : Rs {reg['total_fee']:,}
Transaction ref : {reg['txn_ref'] or 'N/A'}
Confirmed on    : {sent_at.replace("T", " at ")}

Registered sports & players:
{chr(10).join(sport_lines)}

Event details:
  Date  : {EVENT_INFO['date']}
  Venue : {EVENT_INFO['venue']}

Important reminders:
  - Every participant must submit a signed Waiver of Liability at the
    registration desk before playing.
  - Each player may participate in a maximum of {MAX_GAMES_PER_PLAYER} games.
  - Play fairly, respect referees and officials, and show good sportsmanship.

Questions? Contact {EVENT_INFO['contact_person']} or {EVENT_INFO['contact_info']}.

We look forward to seeing your players on event day!

Team Sports Fest
LGS Wapda Town
"""
    subject = f"Sports Fest 2026 — Registration {code} Confirmed"
    if BREVO_API_KEY:
        return _send_via_brevo(reg["email"], subject, body)
    return _send_via_smtp(reg["email"], subject, body)


def send_rejection_email(reg):
    """Send the registration-declined email. Returns (ok, error_message)."""
    if not BREVO_API_KEY and not (SMTP_USER and SMTP_PASSWORD):
        return False, ("email sending is not configured — set BREVO_API_KEY "
                       "or SMTP_USER/SMTP_PASSWORD in the environment")
    code = reg_code(reg["id"])
    body = f"""Dear {reg['focal_name']},

Thank you for registering {reg['school']} for the LGS WT SPORTS FEST 2026.

After reviewing your submission, we are sorry to inform you that your
registration could NOT be accepted at this time.

Your Registration ID is {code}.
You can check the live status of your registration anytime on our website:
  {SITE_URL}/status
Just open that link and enter your Registration ID: {code}

------------------------------------------------------------
Registration ID : {code}
Institution     : {reg['school']}
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

Team Sports Fest
LGS Wapda Town
"""
    subject = f"Sports Fest 2026 — Registration {code} Not Accepted"
    if BREVO_API_KEY:
        return _send_via_brevo(reg["email"], subject, body)
    return _send_via_smtp(reg["email"], subject, body)


def deliver_confirmation(db, reg):
    """Send the confirmation email for a registration and stamp/flash the result."""
    entries, _ = load_entries(reg["id"])
    sent_at = datetime.now().isoformat(timespec="seconds")
    ok, err = send_confirmation_email(reg, entries, sent_at)
    code = reg_code(reg["id"])
    if ok:
        db.execute("UPDATE registrations SET confirm_email_sent = ? WHERE id = ?",
                   (sent_at, reg["id"]))
        db.commit()
        flash(f"{code} — confirmation email sent to {reg['email']}.", "ok")
    else:
        flash(f"{code} — confirmation email was NOT sent: {err}", "warn")


def deliver_rejection(db, reg):
    """Send the declined email and flash the result."""
    ok, err = send_rejection_email(reg)
    code = reg_code(reg["id"])
    if ok:
        flash(f"{code} — declined; a notification email was sent to "
              f"{reg['email']}.", "ok")
    else:
        flash(f"{code} — declined, but the email was NOT sent: {err}", "warn")


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
                flash(f"{reg_code(reg_id)} verified — confirmation email was "
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
    writer.writerow(["Registration ID", "Institution", "Focal Person", "Phone",
                     "Email", "Sport", "Player Name", "Father's Name",
                     "Date of Birth", "CNIC / B-Form", "Total Fee (registration)",
                     "Transaction Ref", "Payment Status", "Receipt File",
                     "Submitted"])
    rows = db.execute(
        "SELECT r.*, p.sport, p.name AS pname, p.father_name, p.dob, p.cnic "
        "FROM registrations r JOIN players p ON p.registration_id = r.id "
        "ORDER BY r.id, p.id").fetchall()
    for row in rows:
        writer.writerow([
            reg_code(row["id"]), row["school"], row["focal_name"], row["phone"],
            row["email"],
            SPORTS[row["sport"]]["name"] if row["sport"] in SPORTS else row["sport"],
            row["pname"], row["father_name"] or "", row["dob"] or "",
            row["cnic"] or "", row["total_fee"], row["txn_ref"] or "",
            row["status"], row["receipt_file"] or "MISSING", row["created_at"],
        ])
    return Response(
        "\ufeff" + buf.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition":
                 "attachment; filename=sportsfest2026_registrations.csv"})


@app.errorhandler(413)
def file_too_large(e):
    return render_template(
        "register.html", sports=SPORTS, info=EVENT_INFO, form={},
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

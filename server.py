import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.abspath(__file__))
DATABASE = os.path.join(ROOT, "suraksha_rain.sqlite3")

POLICIES = [
    ("POL-20481", "9876543210", "Asha Devi", "VIL-NAG-01", "Nagar", "Millet", 2, 20, 3, "RISK_WATCH"),
    ("POL-20482", "9876543211", "Ramesh Kumar", "VIL-NAG-01", "Nagar", "Millet", 1.5, 20, 3, "RISK_WATCH"),
    ("POL-20483", "9876543212", "Meera Bai", "VIL-KOT-02", "Kotra", "Pigeon pea", 1, 15, 3, "DATA_CHECK"),
]

READINGS = [
    ("weather_station", "VIL-NAG-01", 3, 0.96),
    ("satellite", "VIL-NAG-01", 4, 0.91),
    ("weather_station", "VIL-KOT-02", 2, 0.95),
    ("satellite", "VIL-KOT-02", 22, 0.89),
]


def connect():
    connection = sqlite3.connect(DATABASE)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialise_database():
    with connect() as database:
        database.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                farmer_name TEXT NOT NULL,
                mobile TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                last_login_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS login_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id),
                logged_in_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS policies (
                policy_id TEXT PRIMARY KEY,
                mobile TEXT NOT NULL,
                farmer_name TEXT NOT NULL,
                village_id TEXT NOT NULL,
                village TEXT NOT NULL,
                crop_type TEXT NOT NULL,
                area REAL NOT NULL,
                rainfall_trigger_mm REAL NOT NULL,
                assessment_window_days INTEGER NOT NULL,
                status TEXT NOT NULL,
                total_rainfall REAL
            );
            CREATE TABLE IF NOT EXISTS readings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                village_id TEXT NOT NULL,
                rainfall_mm REAL NOT NULL,
                confidence_score REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS claims (
                claim_id TEXT PRIMARY KEY,
                policy_id TEXT NOT NULL REFERENCES policies(policy_id),
                mobile TEXT NOT NULL,
                payout REAL NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sms_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mobile TEXT NOT NULL,
                message TEXT NOT NULL,
                sent_at TEXT NOT NULL
            );
            """
        )
        if database.execute("SELECT COUNT(*) FROM policies").fetchone()[0] == 0:
            database.executemany(
                "INSERT INTO policies VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                POLICIES,
            )
        if database.execute("SELECT COUNT(*) FROM readings").fetchone()[0] == 0:
            database.executemany(
                "INSERT INTO readings (source, village_id, rainfall_mm, confidence_score) VALUES (?, ?, ?, ?)",
                READINGS,
            )


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def row_to_policy(row):
    return {
        "policyId": row["policy_id"],
        "mobile": row["mobile"],
        "farmerName": row["farmer_name"],
        "villageId": row["village_id"],
        "village": row["village"],
        "cropType": row["crop_type"],
        "area": row["area"],
        "rainfallTriggerMm": row["rainfall_trigger_mm"],
        "assessmentWindowDays": row["assessment_window_days"],
        "status": row["status"],
        "totalRainfall": row["total_rainfall"],
    }


def dashboard(database, mobile):
    policies = database.execute("SELECT * FROM policies WHERE mobile = ?", (mobile,)).fetchall()
    policy_ids = [policy["policy_id"] for policy in policies]
    claims = []
    if policy_ids:
        placeholders = ",".join("?" for _ in policy_ids)
        claims = database.execute(
            f"SELECT * FROM claims WHERE policy_id IN ({placeholders}) ORDER BY created_at DESC",
            policy_ids,
        ).fetchall()
    readings = database.execute("SELECT * FROM readings ORDER BY id").fetchall()
    sms = database.execute(
        "SELECT mobile, message, sent_at FROM sms_logs WHERE mobile = ? ORDER BY id DESC",
        (mobile,),
    ).fetchall()
    return {
        "policies": [row_to_policy(policy) for policy in policies],
        "readings": [
            {
                "source": reading["source"],
                "villageId": reading["village_id"],
                "rainfallMm": reading["rainfall_mm"],
                "confidenceScore": reading["confidence_score"],
            }
            for reading in readings
        ],
        "claims": [
            {
                "claimId": claim["claim_id"],
                "policyId": claim["policy_id"],
                "payout": claim["payout"],
                "status": claim["status"],
            }
            for claim in claims
        ],
        "sms": [
            {"mobile": item["mobile"], "message": item["message"], "time": item["sent_at"]}
            for item in sms
        ],
    }


class ApplicationHandler(SimpleHTTPRequestHandler):
    def send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length) or b"{}")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            self.send_json(200, {"ok": True, "database": os.path.basename(DATABASE)})
            return
        super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            payload = self.read_json()
            with connect() as database:
                if parsed.path == "/api/login":
                    self.login(database, payload)
                elif parsed.path == "/api/sms":
                    self.save_sms(database, payload)
                elif parsed.path == "/api/claims":
                    self.save_claim(database, payload)
                else:
                    self.send_json(404, {"error": "Endpoint not found"})
        except (ValueError, KeyError, sqlite3.Error, json.JSONDecodeError) as error:
            self.send_json(400, {"error": str(error)})

    def login(self, database, payload):
        name = str(payload.get("name", "")).strip()
        mobile = re.sub(r"\D", "", str(payload.get("mobile", "")))
        if not name or not re.fullmatch(r"\d{10}", mobile):
            self.send_json(400, {"error": "Enter a name and a valid 10-digit mobile number."})
            return
        timestamp = now()
        database.execute(
            """
            INSERT INTO users (farmer_name, mobile, created_at, last_login_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(mobile) DO UPDATE SET farmer_name = excluded.farmer_name, last_login_at = excluded.last_login_at
            """,
            (name, mobile, timestamp, timestamp),
        )
        user = database.execute("SELECT * FROM users WHERE mobile = ?", (mobile,)).fetchone()
        database.execute("INSERT INTO login_events (user_id, logged_in_at) VALUES (?, ?)", (user["id"], timestamp))
        result = dashboard(database, mobile)
        registered = result["policies"][0] if result["policies"] else None
        result["farmer"] = {"name": registered["farmerName"] if registered else name, "mobile": mobile}
        self.send_json(200, result)

    def save_sms(self, database, payload):
        mobile = re.sub(r"\D", "", str(payload.get("mobile", "")))
        message = str(payload.get("message", "")).strip()
        if not re.fullmatch(r"\d{10}", mobile) or not message:
            self.send_json(400, {"error": "A valid mobile number and message are required."})
            return
        database.execute(
            "INSERT INTO sms_logs (mobile, message, sent_at) VALUES (?, ?, ?)",
            (mobile, message, now()),
        )
        self.send_json(200, {"ok": True})

    def save_claim(self, database, payload):
        policy_id = str(payload.get("policyId", ""))
        mobile = re.sub(r"\D", "", str(payload.get("mobile", "")))
        payout = float(payload.get("payout", 0))
        policy = database.execute(
            "SELECT * FROM policies WHERE policy_id = ? AND mobile = ?",
            (policy_id, mobile),
        ).fetchone()
        if not policy or payout < 0:
            self.send_json(400, {"error": "Policy does not belong to this user."})
            return
        claim_number = database.execute("SELECT COUNT(*) FROM claims").fetchone()[0] + 3001
        claim_id = f"CLM-{claim_number}"
        database.execute(
            "INSERT INTO claims (claim_id, policy_id, mobile, payout, status, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (claim_id, policy_id, mobile, payout, "PAYMENT_PENDING", now()),
        )
        database.execute("UPDATE policies SET status = 'PAYMENT_PENDING' WHERE policy_id = ?", (policy_id,))
        self.send_json(200, {"claimId": claim_id})


if __name__ == "__main__":
    initialise_database()
    port = int(os.environ.get("PORT", "8765"))
    server = ThreadingHTTPServer(("0.0.0.0", port), ApplicationHandler)
    print(f"Suraksha Rain running at http://localhost:{port}")
    server.serve_forever()

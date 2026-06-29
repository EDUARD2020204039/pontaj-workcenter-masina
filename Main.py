import hashlib
import hmac
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pyodbc
import schedule
from flask import Flask, jsonify, render_template, request, send_from_directory


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", "/data" if Path("/data").exists() else BASE_DIR / "data"))
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", BASE_DIR / "downloads"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
REGISTRY_DB = DATA_DIR / "workcenter_registry.db"
VERSION_FILE = BASE_DIR / "VERSION"
APP_VERSION = VERSION_FILE.read_text(encoding="utf-8").strip() if VERSION_FILE.exists() else "0.0.0"
CLIENT_EXE_NAME = "WorkCenterPontaj.exe"
APP_TZ = ZoneInfo(os.getenv("APP_TIMEZONE", "Europe/Bucharest"))

app = Flask(__name__)
log_messages = []
log_lock = threading.Lock()
uid_last_scans = {}
uid_scan_lock = threading.Lock()
client_hash_cache = {"path": "", "mtime_ns": 0, "sha256": ""}
client_hash_lock = threading.Lock()

db_config = {
    "server": os.getenv("DB_SERVER", "192.168.2.6"),
    "database": os.getenv("DB_DATABASE", "Metal"),
    "username": os.getenv("DB_USERNAME", "bogdan"),
    "password": os.getenv("DB_PASSWORD", "HELPAN123$"),
    "driver": os.getenv("DB_DRIVER", "{ODBC Driver 18 for SQL Server}"),
}


def now_local():
    return datetime.now(APP_TZ)


def parse_local_datetime(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=APP_TZ)
    return parsed.astimezone(APP_TZ)


def log(message):
    line = f"{now_local().strftime('%H:%M:%S')} | {message}"
    with log_lock:
        log_messages.append(line)
        del log_messages[:-200]
    print(line, flush=True)


def get_db_connection(timeout=5):
    return pyodbc.connect(
        f"DRIVER={db_config['driver']};"
        f"SERVER={db_config['server']};"
        f"DATABASE={db_config['database']};"
        f"UID={db_config['username']};"
        f"PWD={db_config['password']};"
        "TrustServerCertificate=yes;",
        timeout=timeout,
    )


def get_registry_connection():
    conn = sqlite3.connect(REGISTRY_DB, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def initialize_registry():
    with get_registry_connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS clients (
                client_id TEXT PRIMARY KEY,
                hostname TEXT NOT NULL,
                workcenter_id TEXT,
                workcenter_name TEXT,
                version TEXT,
                ip_address TEXT,
                status TEXT,
                detail TEXT,
                last_seen TEXT NOT NULL
            );

            """
        )


initialize_registry()


def normalize_scan_value(value):
    if value is None:
        return ""
    return str(value).strip().upper().replace(" ", "").replace("-", "").replace(":", "")


def normalize_card_code(value):
    if not value:
        return ""
    return " ".join(str(value).replace("-", " ").replace(":", " ").strip().upper().split())


def request_payload():
    return request.get_json(silent=True) or request.form.to_dict() or {}


def api_authorized():
    configured = os.getenv("API_TOKEN", "")
    if not configured:
        return True
    supplied = request.headers.get("X-API-Token", "")
    return hmac.compare_digest(configured, supplied)


PIN_COLUMN_CANDIDATES = ("PIN", "PIN_PONTAJ", "COD_PIN", "PIN_ANGAJAT")


def detect_pin_column(cursor):
    placeholders = ",".join("?" for _ in PIN_COLUMN_CANDIDATES)
    rows = cursor.execute(
        f"""
        SELECT COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_NAME = 'Angajati' AND UPPER(COLUMN_NAME) IN ({placeholders})
        """,
        *PIN_COLUMN_CANDIDATES,
    ).fetchall()
    available = {str(row[0]).upper(): str(row[0]) for row in rows}
    for candidate in PIN_COLUMN_CANDIDATES:
        if candidate in available:
            return available[candidate]
    return None


def fetch_employee(identifier_type, identifier):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        if identifier_type == "pin":
            pin_column = detect_pin_column(cursor)
            if not pin_column:
                raise RuntimeError(
                    "Tabela Angajati nu conține o coloană PIN cunoscută "
                    f"({', '.join(PIN_COLUMN_CANDIDATES)})."
                )
            cursor.execute(
                f"SELECT TOP 1 ID, Nume, Prenume FROM Angajati WHERE LTRIM(RTRIM(CONVERT(NVARCHAR(50), [{pin_column}]))) = ?",
                str(identifier).strip(),
            )
        elif identifier_type == "uid":
            cursor.execute(
                "SELECT TOP 1 ID, Nume, Prenume FROM Angajati WHERE UPPER(ISNULL(COD_CARTELA, '')) = ?",
                normalize_card_code(identifier),
            )
        else:
            cursor.execute(
                """
                SELECT TOP 1 ID, Nume, Prenume
                FROM Angajati
                WHERE REPLACE(REPLACE(REPLACE(UPPER(ISNULL(TELEFON_UUID, '')), ' ', ''), '-', ''), ':', '') = ?
                """,
                normalize_scan_value(identifier),
            )
        return cursor.fetchone()
    finally:
        conn.close()


def process_pontaj_for_employee(employee_id, workcenter_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    now = now_local().replace(tzinfo=None)
    today = now.date()
    messages = []
    try:
        cursor.execute("SET XACT_ABORT ON; SET TRANSACTION ISOLATION LEVEL SERIALIZABLE;")
        cursor.execute(
            """
            SELECT WorkCenterID, OraCheckIn
            FROM PontajWorkCenter WITH (UPDLOCK, HOLDLOCK)
            WHERE ID = ? AND Data = ? AND OraCheckOut IS NULL
            """,
            employee_id,
            today,
        )
        rows = cursor.fetchall()
        current_is_open = any(str(row[0]) == str(workcenter_id) for row in rows)

        if rows:
            rows_to_close = [row for row in rows if str(row[0]) == str(workcenter_id)] if current_is_open else rows
            checkout = now.strftime("%H:%M:%S")
            for wc_id, checkin in rows_to_close:
                duration = str(now - datetime.combine(today, checkin)).split(".")[0]
                cursor.execute(
                    """
                    UPDATE PontajWorkCenter
                    SET OraCheckOut = ?, DurataTotala = ?
                    WHERE ID = ? AND Data = ? AND WorkCenterID = ? AND OraCheckIn = ? AND OraCheckOut IS NULL
                    """,
                    checkout,
                    duration,
                    employee_id,
                    today,
                    wc_id,
                    checkin,
                )
                messages.append(f"Check-out la WC {wc_id}, ora {checkout} (durata {duration})")

            cursor.execute(
                "UPDATE ProductieAngajati SET OraCheckOut = ? WHERE ID = ? AND Data = ? AND OraCheckOut IS NULL",
                checkout,
                employee_id,
                today,
            )

        if not rows or not current_is_open:
            checkin = now.strftime("%H:%M:%S")
            cursor.execute(
                "INSERT INTO PontajWorkCenter (ID, WorkCenterID, Data, OraCheckIn) VALUES (?, ?, ?, ?)",
                employee_id,
                workcenter_id,
                today,
                checkin,
            )
            cursor.execute(
                "INSERT INTO ProductieAngajati (ID, Data, OraCheckIn) VALUES (?, ?, ?)",
                employee_id,
                today,
                checkin,
            )
            messages.append(f"Check-in la WC {workcenter_id}, ora {checkin}")

        conn.commit()
        return "\n".join(messages)
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()


def client_exe_path():
    path = DOWNLOAD_DIR / CLIENT_EXE_NAME
    return path if path.is_file() else None


def client_exe_sha256():
    path = client_exe_path()
    if not path:
        return ""
    stat = path.stat()
    with client_hash_lock:
        if client_hash_cache["path"] == str(path) and client_hash_cache["mtime_ns"] == stat.st_mtime_ns:
            return client_hash_cache["sha256"]
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        client_hash_cache.update(path=str(path), mtime_ns=stat.st_mtime_ns, sha256=digest.hexdigest())
        return client_hash_cache["sha256"]


def version_payload():
    path = client_exe_path()
    return {
        "version": APP_VERSION,
        "download_available": bool(path),
        "download_url": request.url_root.rstrip("/") + "/download/client" if path else "",
        "sha256": client_exe_sha256(),
        "filename": CLIENT_EXE_NAME,
    }


@app.get("/")
def index():
    return render_template(
        "logs.html",
        version=APP_VERSION,
        download_available=bool(client_exe_path()),
        logs=list(reversed(log_messages)),
    )


@app.get("/api/health")
def health():
    try:
        conn = get_db_connection(timeout=3)
        conn.cursor().execute("SELECT 1").fetchone()
        conn.close()
        return jsonify({"ok": True, "database": "connected", "version": APP_VERSION})
    except Exception as exc:
        return jsonify({"ok": False, "database": "unavailable", "version": APP_VERSION, "error": str(exc)}), 503


@app.get("/api/version")
def version():
    return jsonify(version_payload())


@app.get("/download/client")
def download_client():
    if not client_exe_path():
        return jsonify({"error": "Executabilul nu este disponibil în această imagine."}), 404
    return send_from_directory(DOWNLOAD_DIR, CLIENT_EXE_NAME, as_attachment=True, download_name=CLIENT_EXE_NAME)


@app.get("/api/workcenters")
def workcenters():
    conn = get_db_connection()
    try:
        rows = conn.cursor().execute(
            "SELECT WorkCenterID, RTRIM(Denumire) FROM WorkCenter ORDER BY WorkCenterID"
        ).fetchall()
        return jsonify([{"id": row[0], "name": str(row[1])} for row in rows])
    finally:
        conn.close()


def handle_pontaj(data):
    if not api_authorized():
        return jsonify({"success": False, "message": "Token API invalid."}), 401
    workcenter_id = data.get("workcenter_id")
    identifier_type = str(data.get("identifier_type", "hce")).lower()
    identifier = data.get("identifier") or data.get("hce_id") or data.get("uid") or data.get("pin")
    if identifier_type not in {"hce", "uid", "pin"} or not identifier or not workcenter_id:
        return jsonify({"success": False, "message": "Identificatorul și WorkCenter-ul sunt obligatorii."}), 400

    dedup_key = f"{identifier_type}:{normalize_scan_value(identifier)}"
    now = time.time()
    with uid_scan_lock:
        if identifier_type != "pin" and now - uid_last_scans.get(dedup_key, 0) < 5:
            return jsonify({"success": False, "message": "Așteaptă 5 secunde între scanări."}), 429
        uid_last_scans[dedup_key] = now

    try:
        employee = fetch_employee(identifier_type, identifier)
        if not employee:
            return jsonify({"success": False, "message": "PIN, telefon sau cartelă nerecunoscută."}), 404
        employee_id, first_name, last_name = employee
        full_name = f"{first_name} {last_name}"
        message = process_pontaj_for_employee(employee_id, workcenter_id)
        log(f"{full_name} | {identifier_type.upper()} | WC {workcenter_id} | {message.replace(chr(10), '; ')}")
        return jsonify({"success": True, "name": full_name, "message": message})
    except Exception as exc:
        log(f"Eroare pontaj: {exc}")
        return jsonify({"success": False, "message": f"Eroare bază de date: {exc}"}), 500


@app.post("/api/pontaj")
def pontaj():
    return handle_pontaj(request_payload())


@app.post("/api/log_uid")
def log_uid_compatibility():
    data = request_payload()
    data["identifier_type"] = "hce"
    data["identifier"] = data.get("hce_id")
    return handle_pontaj(data)


@app.post("/api/log")
def log_data():
    if not api_authorized():
        return jsonify({"success": False, "message": "Token API invalid."}), 401
    data = request_payload()
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO PontajWorkCenter (ID, WorkCenterID, Data, OraCheckIn, OraCheckOut, DurataTotala)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            data.get("ID"), data.get("WorkCenterID"), data.get("Data"),
            data.get("OraCheckIn"), data.get("OraCheckOut"), data.get("DurataTotala"),
        )
        conn.commit()
        conn.close()
        return jsonify({"success": True})
    except Exception as exc:
        return jsonify({"success": False, "message": str(exc)}), 500


@app.post("/api/clients/heartbeat")
def client_heartbeat():
    data = request_payload()
    client_id = str(data.get("client_id", "")).strip()
    hostname = str(data.get("hostname", "")).strip()
    if not client_id or not hostname:
        return jsonify({"error": "client_id și hostname sunt obligatorii"}), 400
    now = now_local().isoformat(timespec="seconds")
    with get_registry_connection() as conn:
        conn.execute(
            """
            INSERT INTO clients
                (client_id, hostname, workcenter_id, workcenter_name, version, ip_address, status, detail, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(client_id) DO UPDATE SET
                hostname=excluded.hostname, workcenter_id=excluded.workcenter_id,
                workcenter_name=excluded.workcenter_name, version=excluded.version,
                ip_address=excluded.ip_address, status=excluded.status,
                detail=excluded.detail, last_seen=excluded.last_seen
            """,
            (
                client_id, hostname, str(data.get("workcenter_id", "")),
                str(data.get("workcenter_name", "")), str(data.get("version", "")),
                request.remote_addr, str(data.get("status", "online")),
                str(data.get("detail", ""))[:500], now,
            ),
        )
    return jsonify({"ok": True, "update": version_payload()})


@app.get("/api/clients")
def clients():
    cutoff = now_local() - timedelta(seconds=150)
    with get_registry_connection() as conn:
        rows = conn.execute("SELECT * FROM clients ORDER BY workcenter_name, hostname").fetchall()
    result = []
    for row in rows:
        item = dict(row)
        try:
            item["online"] = parse_local_datetime(item["last_seen"]) >= cutoff
        except (TypeError, ValueError):
            item["online"] = False
        item["update_available"] = bool(item.get("version") and item["version"] != APP_VERSION)
        result.append(item)
    return jsonify(result)


@app.get("/api/employees")
def employees():
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        pin_column = detect_pin_column(cursor)
        pin_expression = f"CASE WHEN [{pin_column}] IS NULL THEN 0 ELSE 1 END" if pin_column else "0"
        rows = cursor.execute(
            f"SELECT ID, Nume, Prenume, {pin_expression} FROM Angajati ORDER BY Nume, Prenume"
        ).fetchall()
    finally:
        conn.close()
    return jsonify({
        "pin_column": pin_column,
        "employees": [
            {"id": row[0], "name": f"{row[1]} {row[2]}", "pin_configured": bool(row[3])}
            for row in rows
        ],
    })


@app.get("/api/angajati_pontati")
def active_employees():
    conn = get_db_connection()
    try:
        rows = conn.cursor().execute(
            """
            SELECT A.Nume, A.Prenume, RTRIM(WC.Denumire)
            FROM PontajWorkCenter P
            INNER JOIN Angajati A ON P.ID = A.ID
            INNER JOIN WorkCenter WC ON P.WorkCenterID = WC.WorkCenterID
            WHERE P.OraCheckOut IS NULL
            ORDER BY WC.Denumire, A.Nume, A.Prenume
            """
        ).fetchall()
        return jsonify([f"{row[0]} {row[1]} — {row[2]}" for row in rows])
    finally:
        conn.close()


@app.get("/api/angajati_inactivi")
def inactive_employees():
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        active = {row[0] for row in cursor.execute(
            "SELECT DISTINCT ID FROM PontajWorkCenter WHERE OraCheckOut IS NULL"
        ).fetchall()}
        rows = cursor.execute("SELECT ID, Nume, Prenume FROM Angajati ORDER BY Nume, Prenume").fetchall()
        return jsonify([f"{row[1]} {row[2]}" for row in rows if row[0] not in active])
    finally:
        conn.close()


def close_abandoned_sessions():
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        today = now_local().date()
        rows = cursor.execute(
            "SELECT ID, WorkCenterID, Data, OraCheckIn FROM PontajWorkCenter WHERE Data < ? AND OraCheckOut IS NULL",
            today,
        ).fetchall()
        checkout = "23:59:00"
        for employee_id, workcenter_id, session_date, checkin in rows:
            end = datetime.combine(session_date, datetime.strptime(checkout, "%H:%M:%S").time())
            start = datetime.combine(session_date, checkin)
            if start > end:
                continue
            duration = str(end - start).split(".")[0]
            cursor.execute(
                """
                UPDATE PontajWorkCenter SET OraCheckOut = ?, DurataTotala = ?, Avertisment = 1
                WHERE ID = ? AND Data = ? AND WorkCenterID = ? AND OraCheckOut IS NULL
                """,
                checkout, duration, employee_id, session_date, workcenter_id,
            )
            cursor.execute(
                "UPDATE ProductieAngajati SET OraCheckOut = ? WHERE ID = ? AND Data = ? AND OraCheckOut IS NULL",
                checkout, employee_id, session_date,
            )
        conn.commit()
        log(f"Închidere automată: {len(rows)} sesiuni procesate.")
    except Exception as exc:
        if conn:
            conn.rollback()
        log(f"Eroare la închiderea automată: {exc}")
    finally:
        if conn:
            conn.close()


def scheduler_loop():
    while True:
        schedule.run_pending()
        time.sleep(30)


schedule.every().day.at("01:00").do(close_abandoned_sessions)
threading.Thread(target=scheduler_loop, daemon=True, name="scheduler").start()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "3490")))

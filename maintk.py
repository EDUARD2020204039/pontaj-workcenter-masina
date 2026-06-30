import ctypes
import hashlib
import json
import os
import platform
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import date, datetime
from pathlib import Path

APP_NAME = "WorkCenterPontaj"
DEFAULT_SERVER_URL = "http://192.168.2.23:3490"
LEGACY_SERVER_URLS = {"http://192.168.2.1:3490"}
SETTINGS_PASSWORD = "XXX"
HEARTBEAT_SECONDS = 60
REPEAT_SCAN_SECONDS = 5
PHONE_SCAN_WAIT_SECONDS = 2
PIN_COLUMN_CANDIDATES = ("PIN", "PIN_PONTAJ", "COD_PIN", "PIN_ANGAJAT")

DB_SERVER = "192.168.2.6"
DB_DATABASE = "Metal"
DB_USERNAME = "bogdan"
DB_PASSWORD = "HELPAN123$"


def resource_path(relative):
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / relative


def read_version():
    path = resource_path("VERSION")
    return path.read_text(encoding="utf-8").strip() if path.exists() else "0.0.0-dev"


APP_VERSION = read_version()

if "--smoke-test" in sys.argv:
    marker = os.getenv("WORKCENTER_SMOKE_MARKER", "")
    for index, argument in enumerate(sys.argv):
        if argument == "--smoke-test" and index + 1 < len(sys.argv):
            marker = sys.argv[index + 1]
            break
    if marker:
        Path(marker).write_text(f"{APP_NAME} {APP_VERSION}", encoding="utf-8")
    sys.exit(0)

import tkinter as tk
from tkinter import messagebox, scrolledtext, simpledialog, ttk
try:
    import winreg
except ImportError:
    winreg = None

import pyodbc
import pystray
import requests
from PIL import Image, ImageDraw

from nfc_reader import get_scan_data

APPDATA_DIR = Path(os.getenv("APPDATA", Path.home())) / APP_NAME
APPDATA_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_FILE = APPDATA_DIR / "config.json"
LOG_FILE = APPDATA_DIR / "client.log"


def default_config():
    return {
        "client_id": str(uuid.uuid4()),
        "server_url": DEFAULT_SERVER_URL,
        "workcenter_id": "",
        "workcenter_name": "",
    }


def load_config():
    config = default_config()
    try:
        if CONFIG_FILE.exists():
            config.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
        if str(config.get("server_url", "")).rstrip("/") in LEGACY_SERVER_URLS:
            config["server_url"] = DEFAULT_SERVER_URL
    except Exception:
        pass
    return config


config = load_config()
single_instance_mutex = None


def ensure_single_instance():
    global single_instance_mutex
    if platform.system() != "Windows":
        return
    try:
        single_instance_mutex = ctypes.windll.kernel32.CreateMutexW(None, False, APP_NAME)
        if ctypes.windll.kernel32.GetLastError() == 183:
            sys.exit(0)
    except Exception:
        pass


ensure_single_instance()


def save_config():
    CONFIG_FILE.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


debug_logs = []
debug_lock = threading.Lock()
debug_window = None
debug_widget = None
tray_icon = None
recent_scans = {}
update_started = False
last_preflight = {"driver": False, "database": False, "nfc": False, "server": False}


def debug(message):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}"
    with debug_lock:
        debug_logs.append(line)
        del debug_logs[:-400]
        try:
            with LOG_FILE.open("a", encoding="utf-8") as stream:
                stream.write(line + "\n")
        except Exception:
            pass
    if debug_widget and debug_widget.winfo_exists():
        root.after(0, refresh_debug)


def available_sql_driver():
    drivers = pyodbc.drivers()
    for preferred in ("ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server"):
        if preferred in drivers:
            return preferred
    return ""


def install_bundled_drivers():
    installer = resource_path("drivers/install_drivers.cmd")
    msi = resource_path("drivers/msodbcsql.msi")
    runtime = resource_path("drivers/vc_redist.x64.exe")
    if not all(path.exists() for path in (installer, msi, runtime)):
        messagebox.showerror(
            "Driver SQL lipsă",
            "Driverul Microsoft ODBC lipsește, iar această versiune nu conține installerul inclus. "
            "Descarcă din nou aplicația de pe pagina serverului.",
        )
        return False
    answer = messagebox.askyesno(
        "Instalare driver SQL",
        "Microsoft ODBC Driver nu este instalat. Aplicația îl poate instala acum.\n\n"
        "Windows va solicita drepturi de administrator. Continui?",
    )
    if not answer:
        return False
    result = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", "cmd.exe", f'/c "{installer}"', str(installer.parent), 0
    )
    if result <= 32:
        messagebox.showerror("Instalare eșuată", f"Installerul nu a pornit (cod {result}).")
        return False
    status_var.set("Se instalează driverul SQL…")
    root.update_idletasks()
    for _ in range(90):
        time.sleep(1)
        if available_sql_driver():
            return True
    messagebox.showwarning("Instalare", "Instalarea nu s-a confirmat în timp util. Repornește aplicația.")
    return False


def get_db_connection(timeout=5):
    driver = available_sql_driver()
    if not driver:
        raise RuntimeError("Microsoft ODBC Driver 17/18 nu este instalat.")
    return pyodbc.connect(
        f"DRIVER={{{driver}}};SERVER={DB_SERVER};DATABASE={DB_DATABASE};"
        f"UID={DB_USERNAME};PWD={DB_PASSWORD};TrustServerCertificate=yes;",
        timeout=timeout,
    )


def detect_pin_column(cursor):
    placeholders = ",".join("?" for _ in PIN_COLUMN_CANDIDATES)
    rows = cursor.execute(
        f"""
        SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_NAME = 'Angajati' AND UPPER(COLUMN_NAME) IN ({placeholders})
        """,
        *PIN_COLUMN_CANDIDATES,
    ).fetchall()
    found = {str(row[0]).upper(): str(row[0]) for row in rows}
    return next((found[name] for name in PIN_COLUMN_CANDIDATES if name in found), "")


def normalize_hce(value):
    return str(value or "").strip().upper().replace(" ", "").replace("-", "").replace(":", "")


def normalize_uid(value):
    return " ".join(str(value or "").replace("-", " ").replace(":", " ").strip().upper().split())


def resolve_employee(identifier_type, identifier):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        if identifier_type == "pin":
            column = detect_pin_column(cursor)
            if not column:
                raise RuntimeError(
                    "În Angajati nu există coloana PIN. Am verificat: " + ", ".join(PIN_COLUMN_CANDIDATES)
                )
            cursor.execute(
                f"SELECT TOP 1 ID, Nume, Prenume FROM Angajati "
                f"WHERE LTRIM(RTRIM(CONVERT(NVARCHAR(50), [{column}]))) = ?",
                str(identifier).strip(),
            )
        elif identifier_type == "uid":
            cursor.execute(
                "SELECT TOP 1 ID, Nume, Prenume FROM Angajati WHERE UPPER(ISNULL(COD_CARTELA, '')) = ?",
                normalize_uid(identifier),
            )
        else:
            cursor.execute(
                """
                SELECT TOP 1 ID, Nume, Prenume FROM Angajati
                WHERE REPLACE(REPLACE(REPLACE(UPPER(ISNULL(TELEFON_UUID, '')), ' ', ''), '-', ''), ':', '') = ?
                """,
                normalize_hce(identifier),
            )
        return cursor.fetchone()
    finally:
        conn.close()


def process_pontaj(employee_id, workcenter_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    today = date.today()
    now = datetime.now()
    messages = []
    try:
        cursor.execute("SET XACT_ABORT ON; SET TRANSACTION ISOLATION LEVEL SERIALIZABLE;")
        rows = cursor.execute(
            """
            SELECT WorkCenterID, OraCheckIn FROM PontajWorkCenter WITH (UPDLOCK, HOLDLOCK)
            WHERE ID = ? AND Data = ? AND OraCheckOut IS NULL
            """,
            employee_id, today,
        ).fetchall()
        current_open = any(str(row[0]) == str(workcenter_id) for row in rows)
        if rows:
            to_close = [row for row in rows if str(row[0]) == str(workcenter_id)] if current_open else rows
            checkout = now.strftime("%H:%M:%S")
            for wc_id, checkin in to_close:
                duration = str(now - datetime.combine(today, checkin)).split(".")[0]
                cursor.execute(
                    """
                    UPDATE PontajWorkCenter SET OraCheckOut = ?, DurataTotala = ?
                    WHERE ID = ? AND Data = ? AND WorkCenterID = ? AND OraCheckIn = ? AND OraCheckOut IS NULL
                    """,
                    checkout, duration, employee_id, today, wc_id, checkin,
                )
                messages.append(f"Check-out la WC {wc_id}, ora {checkout} (durata {duration})")
            cursor.execute(
                "UPDATE ProductieAngajati SET OraCheckOut = ? WHERE ID = ? AND Data = ? AND OraCheckOut IS NULL",
                checkout, employee_id, today,
            )
        if not rows or not current_open:
            checkin = now.strftime("%H:%M:%S")
            cursor.execute(
                "INSERT INTO PontajWorkCenter (ID, WorkCenterID, Data, OraCheckIn) VALUES (?, ?, ?, ?)",
                employee_id, workcenter_id, today, checkin,
            )
            cursor.execute(
                "INSERT INTO ProductieAngajati (ID, Data, OraCheckIn) VALUES (?, ?, ?)",
                employee_id, today, checkin,
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


def perform_pontaj(identifier_type, identifier):
    workcenter_id = config.get("workcenter_id")
    if not workcenter_id:
        show_result(False, "Selectează mai întâi WorkCenter-ul.", "")
        return
    key = f"{identifier_type}:{normalize_hce(identifier)}"
    now = time.time()
    if identifier_type != "pin" and now - recent_scans.get(key, 0) < REPEAT_SCAN_SECONDS:
        return
    recent_scans[key] = now
    set_busy(True, "Se procesează pontajul…")

    def worker():
        try:
            employee = resolve_employee(identifier_type, identifier)
            if not employee:
                raise LookupError("PIN, telefon sau cartelă nerecunoscută.")
            employee_id, first_name, last_name = employee
            name = f"{first_name} {last_name}"
            message = process_pontaj(employee_id, workcenter_id)
            debug(f"Pontaj {identifier_type} | {name} | WC {workcenter_id} | {message}")
            root.after(0, lambda: show_result(True, message, name))
        except Exception as exc:
            debug(f"Eroare pontaj {identifier_type}: {exc}")
            root.after(0, lambda: show_result(False, str(exc), ""))
    threading.Thread(target=worker, daemon=True).start()


def set_busy(busy, text=""):
    pin_button.config(state="disabled" if busy else "normal")
    if text:
        status_var.set(text)


def show_result(success, message, name):
    set_busy(False)
    name_var.set(name)
    result_var.set(message)
    result_label.config(foreground="#19734b" if success else "#b33232")
    status_var.set("Gata pentru următoarea pontare" if success else "Pontarea nu a fost înregistrată")
    pin_var.set("")
    pin_entry.focus_set()
    if success:
        try:
            ctypes.windll.user32.MessageBeep(0x00000040)
        except Exception:
            pass


def submit_pin(event=None):
    pin = pin_var.get().strip()
    if not pin.isdigit():
        show_result(False, "PIN-ul trebuie să conțină numai cifre.", "")
        return
    perform_pontaj("pin", pin)


def load_workcenters():
    try:
        conn = get_db_connection()
        rows = conn.cursor().execute("SELECT WorkCenterID, RTRIM(Denumire) FROM WorkCenter ORDER BY WorkCenterID").fetchall()
        conn.close()
        values = [f"{row[0]} - {row[1]}" for row in rows]
        root.after(0, lambda: apply_workcenters(values))
    except Exception as exc:
        debug(f"Nu pot încărca WorkCenter-ele: {exc}")
        root.after(0, lambda: status_var.set(f"Eroare SQL: {exc}"))


def apply_workcenters(values):
    workcenter_combo["values"] = values
    saved = str(config.get("workcenter_id", ""))
    selected = next((item for item in values if item.startswith(saved + " - ")), "")
    if selected:
        workcenter_var.set(selected)


def unlock_workcenter(event=None):
    if workcenter_combo["state"] == "readonly":
        return
    password = simpledialog.askstring("Configurare", "Parola pentru schimbarea WorkCenter-ului:", show="*")
    if password == SETTINGS_PASSWORD:
        workcenter_combo.config(state="readonly")
    elif password is not None:
        messagebox.showerror("Parolă", "Parolă incorectă.")


def workcenter_changed(event=None):
    value = workcenter_var.get()
    if " - " not in value:
        return
    wc_id, name = value.split(" - ", 1)
    config["workcenter_id"] = wc_id
    config["workcenter_name"] = name
    save_config()
    status_var.set(f"Stație configurată: {name}")


def change_server():
    password = simpledialog.askstring("Configurare", "Parola de configurare:", show="*")
    if password != SETTINGS_PASSWORD:
        return
    value = simpledialog.askstring("Server actualizări", "Adresa containerului:", initialvalue=config["server_url"])
    if value:
        config["server_url"] = value.strip().rstrip("/")
        save_config()
        threading.Thread(target=heartbeat, daemon=True).start()


def run_preflight():
    global last_preflight
    result = {"driver": False, "database": False, "nfc": False, "server": False}
    details = []
    driver = available_sql_driver()
    result["driver"] = bool(driver)
    details.append(f"ODBC: {driver or 'lipsește'}")
    if driver:
        try:
            conn = get_db_connection(timeout=3)
            cursor = conn.cursor()
            cursor.execute("SELECT 1").fetchone()
            pin_column = detect_pin_column(cursor)
            conn.close()
            result["database"] = True
            details.append(f"SQL: conectat; PIN: {pin_column or 'coloană negăsită'}")
        except Exception as exc:
            details.append(f"SQL: {exc}")
    try:
        from smartcard.System import readers
        reader_names = [str(item) for item in readers()]
        result["nfc"] = bool(reader_names)
        details.append("NFC: " + (", ".join(reader_names) if reader_names else "niciun cititor; PIN-ul rămâne disponibil"))
    except Exception as exc:
        details.append(f"NFC: {exc}")
    try:
        response = requests.get(config["server_url"] + "/api/version", timeout=3)
        response.raise_for_status()
        result["server"] = True
        details.append(f"Update server: online, v{response.json().get('version', '?')}")
    except Exception as exc:
        details.append(f"Update server: indisponibil ({exc})")
    last_preflight = result
    debug("Preflight | " + " | ".join(details))
    root.after(0, lambda: update_preflight_ui(result, details))


def update_preflight_ui(result, details):
    preflight_var.set("  ·  ".join([
        ("✓" if result["driver"] else "✕") + " Driver SQL",
        ("✓" if result["database"] else "✕") + " Bază de date",
        ("✓" if result["nfc"] else "!") + " Cititor NFC",
        ("✓" if result["server"] else "!") + " Server update",
    ]))
    status_var.set("Gata pentru pontare" if result["database"] else "Verifică diagnosticul; baza SQL nu este accesibilă")


def version_tuple(value):
    parts = []
    for part in str(value).split("."):
        digits = "".join(char for char in part if char.isdigit())
        parts.append(int(digits or 0))
    return tuple((parts + [0, 0, 0])[:3])


def heartbeat(status="online", detail=""):
    global update_started
    try:
        payload = {
            "client_id": config["client_id"],
            "hostname": socket.gethostname(),
            "workcenter_id": config.get("workcenter_id", ""),
            "workcenter_name": config.get("workcenter_name", ""),
            "version": APP_VERSION,
            "status": status,
            "detail": detail,
        }
        response = requests.post(config["server_url"] + "/api/clients/heartbeat", json=payload, timeout=8)
        response.raise_for_status()
        update = response.json().get("update", {})
        if (
            not update_started and getattr(sys, "frozen", False)
            and update.get("download_available")
            and version_tuple(update.get("version")) > version_tuple(APP_VERSION)
        ):
            update_started = True
            debug(f"Actualizare găsită: {APP_VERSION} -> {update.get('version')}")
            download_and_install_update(update)
    except Exception as exc:
        debug(f"Heartbeat eșuat: {exc}")


def download_and_install_update(update):
    global update_started
    try:
        root.after(0, lambda: status_var.set(f"Se descarcă actualizarea v{update['version']}…"))
        response = requests.get(update["download_url"], timeout=180, stream=True)
        response.raise_for_status()
        target = Path(tempfile.gettempdir()) / f"{APP_NAME}-{update['version']}.exe"
        digest = hashlib.sha256()
        with target.open("wb") as stream:
            for chunk in response.iter_content(1024 * 1024):
                if chunk:
                    stream.write(chunk)
                    digest.update(chunk)
        if update.get("sha256") and digest.hexdigest().lower() != update["sha256"].lower():
            raise RuntimeError("Semnătura SHA-256 a actualizării nu corespunde.")
        current = Path(sys.executable).resolve()
        script = Path(tempfile.gettempdir()) / f"{APP_NAME}-update.cmd"
        updater_log = APPDATA_DIR / "updater.log"
        current_pid = os.getpid()
        script.write_text(
            "@echo off\r\n"
            "setlocal enableextensions\r\n"
            f'set "SOURCE={str(target).replace("%", "%%")}"\r\n'
            f'set "TARGET={str(current).replace("%", "%%")}"\r\n'
            f'set "LOGFILE={str(updater_log).replace("%", "%%")}"\r\n'
            f'set "OLDPID={current_pid}"\r\n'
            'call :log "Updater pornit. Source=%SOURCE% Target=%TARGET% OldPid=%OLDPID%"\r\n'
            "timeout /t 2 /nobreak >nul\r\n"
            "for /l %%I in (1,1,15) do (\r\n"
            '    tasklist /fi "PID eq %OLDPID%" | find "%OLDPID%" >nul\r\n'
            "    if errorlevel 1 goto copy_update\r\n"
            '    call :log "Astept inchiderea aplicatiei vechi, incercarea %%I."\r\n'
            "    timeout /t 1 /nobreak >nul\r\n"
            ")\r\n"
            'call :log "Aplicatia veche inca ruleaza; o inchid fortat."\r\n'
            "taskkill /pid %OLDPID% /f >nul 2>nul\r\n"
            "timeout /t 2 /nobreak >nul\r\n"
            ":copy_update\r\n"
            "for /l %%I in (1,1,40) do (\r\n"
            '    copy /y "%SOURCE%" "%TARGET%" >nul 2>>"%LOGFILE%"\r\n'
            "    if not errorlevel 1 (\r\n"
            '        call :log "Copiere reusita la incercarea %%I."\r\n'
            '        start "" "%TARGET%"\r\n'
            '        del /q "%SOURCE%" >nul 2>nul\r\n'
            '        del /q "%~f0" >nul 2>nul\r\n'
            "        exit /b 0\r\n"
            "    )\r\n"
            '    call :log "Copiere esuata la incercarea %%I."\r\n'
            "    timeout /t 1 /nobreak >nul\r\n"
            ")\r\n"
            'call :log "Actualizarea a esuat dupa 40 de incercari; repornesc versiunea existenta."\r\n'
            'start "" "%TARGET%"\r\n'
            "exit /b 1\r\n"
            ":log\r\n"
            '>>"%LOGFILE%" echo [%date% %time%] %~1\r\n'
            "exit /b 0\r\n",
            encoding="utf-8",
        )
        heartbeat("updating", f"Actualizare la v{update['version']}")
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
        subprocess.Popen(
            ["cmd.exe", "/c", str(script)],
            creationflags=creationflags,
            close_fds=True,
        )
        os._exit(0)
    except Exception as exc:
        update_started = False
        debug(f"Actualizare eșuată: {exc}")
        root.after(0, lambda: status_var.set(f"Actualizarea a eșuat: {exc}"))


def heartbeat_loop():
    while True:
        heartbeat()
        time.sleep(HEARTBEAT_SECONDS)


def preferred_scan(initial):
    uid = initial.get("uid", "")
    hce = initial.get("hce_id", "")
    if hce:
        return "hce", hce
    deadline = time.time() + PHONE_SCAN_WAIT_SECONDS
    while uid and time.time() < deadline:
        time.sleep(.25)
        retry = get_scan_data()
        if retry and retry.get("hce_id"):
            return "hce", retry["hce_id"]
    return "uid", uid


def nfc_loop():
    last_key = ""
    last_seen = 0.0
    while True:
        try:
            scan = get_scan_data()
            if scan:
                identifier_type, identifier = preferred_scan(scan)
                key = f"{identifier_type}:{normalize_hce(identifier)}"
                if identifier and key != last_key:
                    root.after(0, lambda t=identifier_type, value=identifier: perform_pontaj(t, value))
                last_key = key
                last_seen = time.time()
            elif time.time() - last_seen > 1.2:
                last_key = ""
        except Exception as exc:
            debug(f"Cititor NFC: {exc}")
            time.sleep(4)
        time.sleep(.8)


def refresh_debug():
    if not debug_widget or not debug_widget.winfo_exists():
        return
    debug_widget.config(state="normal")
    debug_widget.delete("1.0", tk.END)
    with debug_lock:
        debug_widget.insert(tk.END, "\n".join(debug_logs))
    debug_widget.see(tk.END)
    debug_widget.config(state="disabled")


def show_debug():
    global debug_window, debug_widget
    password = simpledialog.askstring("Diagnostic", "Parola de configurare:", show="*")
    if password != SETTINGS_PASSWORD:
        return
    if debug_window and debug_window.winfo_exists():
        debug_window.deiconify()
        return
    debug_window = tk.Toplevel(root)
    debug_window.title("Diagnostic WorkCenter Pontaj")
    debug_window.geometry("900x500")
    debug_widget = scrolledtext.ScrolledText(debug_window, font=("Consolas", 10), state="disabled")
    debug_widget.pack(fill="both", expand=True, padx=10, pady=10)
    refresh_debug()


def tray_image():
    icon = resource_path("card.ico")
    if icon.exists():
        return Image.open(icon)
    image = Image.new("RGBA", (64, 64), "#176b46")
    ImageDraw.Draw(image).text((15, 22), "WC", fill="white")
    return image


def ensure_startup_registration():
    if platform.system() != "Windows" or winreg is None or not getattr(sys, "frozen", False):
        return
    try:
        exe_path = Path(sys.executable).resolve()
        command = f'"{exe_path}" --minimized'
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, command)
        startup_dir = (
            Path(os.getenv("APPDATA", ""))
            / "Microsoft"
            / "Windows"
            / "Start Menu"
            / "Programs"
            / "Startup"
        )
        startup_dir.mkdir(parents=True, exist_ok=True)
        startup_script = startup_dir / f"{APP_NAME}.cmd"
        startup_script.write_text(
            "@echo off\r\n"
            f'start "" "{str(exe_path).replace("%", "%%")}" --minimized\r\n',
            encoding="utf-8",
        )
        debug("Pornire automată configurată în Registry și Startup folder.")
    except Exception as exc:
        debug(f"Nu am putut configura pornirea automată: {exc}")


def set_tray_visible():
    if not tray_icon:
        return
    try:
        tray_icon.visible = True
    except Exception as exc:
        debug(f"Nu am putut afișa iconița în system tray: {exc}")


def ensure_tray_icon():
    global tray_icon
    if tray_icon is not None:
        set_tray_visible()
        return
    menu = pystray.Menu(
        pystray.MenuItem("Deschide", show_window),
        pystray.MenuItem("Ascunde", hide_window),
        pystray.MenuItem("Ieșire", quit_app),
    )
    tray_icon = pystray.Icon(APP_NAME, tray_image(), "WorkCenter Pontaj", menu)
    threading.Thread(target=tray_icon.run, daemon=True).start()
    root.after(500, set_tray_visible)


def show_window(icon=None, item=None):
    root.after(0, root.deiconify)
    root.after(0, root.lift)


def quit_app(icon=None, item=None):
    heartbeat("offline", "Aplicație închisă")
    if tray_icon:
        tray_icon.stop()
    root.after(0, root.destroy)


def hide_window():
    root.withdraw()
    ensure_tray_icon()


root = tk.Tk()
root.title(f"WorkCenter Pontaj · v{APP_VERSION}")
root.geometry("720x520")
root.minsize(680, 500)
root.protocol("WM_DELETE_WINDOW", hide_window)

style = ttk.Style(root)
style.configure("Title.TLabel", font=("Segoe UI", 22, "bold"))
style.configure("Name.TLabel", font=("Segoe UI", 17, "bold"))
style.configure("Result.TLabel", font=("Segoe UI", 12))

main = ttk.Frame(root, padding=24)
main.pack(fill="both", expand=True)
ttk.Label(main, text="WorkCenter Pontaj", style="Title.TLabel").pack(anchor="w")
ttk.Label(main, text=f"Stația {socket.gethostname()} · versiunea {APP_VERSION}").pack(anchor="w", pady=(0, 18))

workcenter_row = ttk.Frame(main)
workcenter_row.pack(fill="x", pady=(0, 16))
ttk.Label(workcenter_row, text="WorkCenter:").pack(side="left")
workcenter_var = tk.StringVar()
workcenter_combo = ttk.Combobox(workcenter_row, textvariable=workcenter_var, state="disabled", width=42)
workcenter_combo.pack(side="left", padx=10, fill="x", expand=True)
workcenter_combo.bind("<Button-1>", unlock_workcenter)
workcenter_combo.bind("<<ComboboxSelected>>", workcenter_changed)
ttk.Button(workcenter_row, text="Schimbă", command=unlock_workcenter).pack(side="left", padx=(0, 8))
ttk.Button(workcenter_row, text="Server", command=change_server).pack(side="left")

pin_box = ttk.LabelFrame(main, text="Pontare cu PIN", padding=18)
pin_box.pack(fill="x")
ttk.Label(pin_box, text="Introdu PIN-ul personal și apasă Enter:").pack(anchor="w")
pin_row = ttk.Frame(pin_box)
pin_row.pack(fill="x", pady=(8, 0))
pin_var = tk.StringVar()
pin_entry = ttk.Entry(pin_row, textvariable=pin_var, show="●", font=("Segoe UI", 20), justify="center")
pin_entry.pack(side="left", fill="x", expand=True)
pin_entry.bind("<Return>", submit_pin)
pin_button = ttk.Button(pin_row, text="Pontează", command=submit_pin)
pin_button.pack(side="left", padx=(10, 0))

name_var = tk.StringVar()
result_var = tk.StringVar(value="Poți folosi PIN-ul, cartela sau telefonul NFC.")
ttk.Label(main, textvariable=name_var, style="Name.TLabel").pack(pady=(22, 4))
result_label = ttk.Label(main, textvariable=result_var, style="Result.TLabel", wraplength=620, justify="center")
result_label.pack()

status_var = tk.StringVar(value="Se verifică sistemul…")
preflight_var = tk.StringVar(value="Driver SQL  ·  Bază de date  ·  Cititor NFC  ·  Server update")
ttk.Separator(main).pack(fill="x", pady=(22, 12))
ttk.Label(main, textvariable=status_var).pack()
ttk.Label(main, textvariable=preflight_var, foreground="#66736e").pack(pady=(4, 0))

actions = ttk.Frame(main)
actions.pack(fill="x", pady=(14, 0))
ttk.Button(actions, text="Reverifică", command=lambda: threading.Thread(target=run_preflight, daemon=True).start()).pack(side="left")
ttk.Button(actions, text="Diagnostic", command=show_debug).pack(side="right")

root.update_idletasks()
if not available_sql_driver():
    install_bundled_drivers()
ensure_startup_registration()
ensure_tray_icon()
threading.Thread(target=load_workcenters, daemon=True).start()
threading.Thread(target=run_preflight, daemon=True).start()
threading.Thread(target=heartbeat_loop, daemon=True).start()
threading.Thread(target=nfc_loop, daemon=True).start()
if "--minimized" in sys.argv:
    root.withdraw()
pin_entry.focus_set()
root.mainloop()

import json
import os
import sys
import threading
import time
import tkinter as tk
from datetime import date, datetime
from queue import Queue
from tkinter import scrolledtext, simpledialog, ttk

import pyodbc
import pystray
from PIL import Image, ImageDraw
from win10toast import ToastNotifier

from nfc_reader import get_scan_data

last_scan_key = None
last_scan_time = 0
selected_workcenter_id = None
tray_icon = None
tray_image = None
toaster = ToastNotifier()
recent_scan_keys = {}
PHONE_SCAN_WAIT_SECONDS = 2.0
PHONE_SCAN_POLL_SECONDS = 0.25
REPEAT_SCAN_DELAY_SECONDS = 5.0
DEBUG_PASSWORD = "XXX"
DEBUG_LOG_LIMIT = 300
debug_logs = []
debug_window = None
debug_text_widget = None
debug_lock = threading.Lock()

db_config = {
    "server": "192.168.2.6",
    "database": "Metal",
    "username": "bogdan",
    "password": "HELPAN123$",
    "driver": "{ODBC Driver 17 for SQL Server}",
}


def get_db_connection():
    return pyodbc.connect(
        f"DRIVER={db_config['driver']};"
        f"SERVER={db_config['server']};"
        f"DATABASE={db_config['database']};"
        f"UID={db_config['username']};"
        f"PWD={db_config['password']}"
    )


def normalize_scan_value(value):
    if value is None:
        return None
    return str(value).strip().upper().replace(" ", "").replace("-", "").replace(":", "")


def normalize_card_code(value):
    if not value:
        return ""
    return " ".join(str(value).replace("-", " ").replace(":", " ").strip().upper().split())


def get_base_dir():
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def get_config_file():
    appdata_dir = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "WorkCenterScanTEST")
    os.makedirs(appdata_dir, exist_ok=True)
    return os.path.join(appdata_dir, "config.json")


def get_icon_path():
    return os.path.join(get_base_dir(), "card.ico")


def load_tray_image():
    icon_path = get_icon_path()
    if os.path.exists(icon_path):
        try:
            return Image.open(icon_path)
        except Exception as exc:
            append_debug_log(f"Eroare incarcare icon tray: {exc}")

    image = Image.new("RGBA", (64, 64), "#1f6aa5")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((4, 4, 60, 60), radius=12, fill="#1f6aa5", outline="white", width=2)
    draw.text((18, 20), "WC", fill="white")
    return image


CONFIG_FILE = get_config_file()


def solicita_parola():
    if workcenter_dropdown["state"] == "readonly":
        return

    parola = simpledialog.askstring("Autentificare", "Introduceti parola:", show="*")
    if parola == DEBUG_PASSWORD:
        workcenter_dropdown.config(state="readonly")
        mesaj_label.config(text="Selecteaza WorkCenter", fg="green")
    else:
        mesaj_label.config(text="Parola gresita", fg="red")


def append_debug_log(message):
    timestamp = datetime.now().strftime("%H:%M:%S")
    line = f"[{timestamp}] {message}"
    with debug_lock:
        debug_logs.append(line)
        if len(debug_logs) > DEBUG_LOG_LIMIT:
            del debug_logs[:-DEBUG_LOG_LIMIT]

    if debug_text_widget and debug_text_widget.winfo_exists():
        root.after(0, refresh_debug_window)


def refresh_debug_window():
    if not debug_text_widget or not debug_text_widget.winfo_exists():
        return

    debug_text_widget.config(state="normal")
    debug_text_widget.delete("1.0", tk.END)
    with debug_lock:
        debug_text_widget.insert(tk.END, "\n".join(debug_logs))
    debug_text_widget.see(tk.END)
    debug_text_widget.config(state="disabled")


def close_debug_window():
    global debug_window, debug_text_widget
    if debug_window and debug_window.winfo_exists():
        debug_window.destroy()
    debug_window = None
    debug_text_widget = None


def show_debug_window():
    global debug_window, debug_text_widget

    parola = simpledialog.askstring("Debug", "Introduceti parola debug:", show="*")
    if parola != DEBUG_PASSWORD:
        mesaj_label.config(text="Parola debug gresita", fg="red")
        return

    if debug_window and debug_window.winfo_exists():
        debug_window.deiconify()
        debug_window.lift()
        refresh_debug_window()
        return

    debug_window = tk.Toplevel(root)
    debug_window.title("Debug NFC")
    debug_window.geometry("760x420")
    debug_window.protocol("WM_DELETE_WINDOW", close_debug_window)

    debug_text_widget = scrolledtext.ScrolledText(debug_window, wrap=tk.WORD, font=("Consolas", 10))
    debug_text_widget.pack(fill="both", expand=True, padx=10, pady=10)
    debug_text_widget.config(state="disabled")
    refresh_debug_window()


def save_workcenter_config(workcenter_id):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump({"selected_workcenter_id": workcenter_id}, f)


def load_workcenter_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            try:
                data = json.load(f)
                return data.get("selected_workcenter_id")
            except Exception:
                return None
    return None


def find_employee_by_column(column_name, scan_value, result_queue):
    try:
        normalized_value = (
            normalize_card_code(scan_value)
            if column_name == "COD_CARTELA"
            else normalize_scan_value(scan_value)
        )
        if not normalized_value:
            append_debug_log(f"DB skip {column_name}: valoare vida")
            return

        conn = get_db_connection()
        cursor = conn.cursor()
        if column_name == "COD_CARTELA":
            cursor.execute(
                """
                SELECT TOP 1 ID, Nume, Prenume
                FROM Angajati
                WHERE UPPER(ISNULL(COD_CARTELA, '')) = ?
            """,
                (normalized_value,),
            )
        else:
            cursor.execute(
                f"""
                SELECT TOP 1 ID, Nume, Prenume
                FROM Angajati
                WHERE REPLACE(REPLACE(REPLACE(UPPER(ISNULL({column_name}, '')), ' ', ''), '-', ''), ':', '') = ?
            """,
                (normalized_value,),
            )
        row = cursor.fetchone()
        cursor.close()
        conn.close()

        if row:
            append_debug_log(f"DB match {column_name}: {normalized_value} -> ID {row[0]}")
            result_queue.put((column_name, row))
        else:
            append_debug_log(f"DB fara match {column_name}: {normalized_value}")
    except Exception as exc:
        append_debug_log(f"Eroare DB lookup {column_name}: {exc}")
        return


def resolve_employee(hce_id, uid):
    result_queue = Queue()
    workers = []

    if hce_id:
        workers.append(threading.Thread(
            target=find_employee_by_column,
            args=("TELEFON_UUID", hce_id, result_queue),
            daemon=True,
        ))

    if uid:
        workers.append(threading.Thread(
            target=find_employee_by_column,
            args=("COD_CARTELA", uid, result_queue),
            daemon=True,
        ))

    for worker in workers:
        worker.start()

    for worker in workers:
        worker.join(timeout=3)

    results = []
    while not result_queue.empty():
        results.append(result_queue.get())

    for source, row in results:
        if source == "TELEFON_UUID":
            return source, row

    if results:
        return results[0]

    return None, None


def process_pontaj_for_employee(angajat_id, workcenter_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    azi = date.today()
    acum = datetime.now()
    mesaj_final = ""

    cursor.execute(
        """
        SELECT WorkCenterID, OraCheckIn
        FROM PontajWorkCenter
        WHERE ID = ? AND Data = ? AND OraCheckOut IS NULL
    """,
        (angajat_id, azi),
    )
    rows = cursor.fetchall()

    if rows:
        already_checked_in_current = any(str(wc_id) == str(workcenter_id) for wc_id, _ in rows)

        if already_checked_in_current:
            for wc_id, ora_checkin in rows:
                if str(wc_id) == str(workcenter_id):
                    datetime_checkin = datetime.combine(azi, ora_checkin)
                    durata = acum - datetime_checkin
                    durata_str = str(durata).split(".")[0]
                    ora_checkout_str = acum.strftime("%H:%M:%S")
                    cursor.execute(
                        """
                        UPDATE PontajWorkCenter
                        SET OraCheckOut = ?, DurataTotala = ?
                        WHERE ID = ? AND Data = ? AND WorkCenterID = ? AND OraCheckIn = ?
                    """,
                        (ora_checkout_str, durata_str, angajat_id, azi, wc_id, ora_checkin),
                    )
                    cursor.execute(
                        """
                        UPDATE ProductieAngajati
                        SET OraCheckOut = ?
                        WHERE ID = ? AND Data = ? AND OraCheckOut IS NULL
                    """,
                        (ora_checkout_str, angajat_id, azi),
                    )
                    mesaj_final += f"Check-out la WC {wc_id} la {ora_checkout_str} (durata {durata_str})\n"
        else:
            for wc_id, ora_checkin in rows:
                datetime_checkin = datetime.combine(azi, ora_checkin)
                durata = acum - datetime_checkin
                durata_str = str(durata).split(".")[0]
                ora_checkout_str = acum.strftime("%H:%M:%S")
                cursor.execute(
                    """
                    UPDATE PontajWorkCenter
                    SET OraCheckOut = ?, DurataTotala = ?
                    WHERE ID = ? AND Data = ? AND WorkCenterID = ? AND OraCheckIn = ?
                """,
                    (ora_checkout_str, durata_str, angajat_id, azi, wc_id, ora_checkin),
                )
                cursor.execute(
                    """
                    UPDATE ProductieAngajati
                    SET OraCheckOut = ?
                    WHERE ID = ? AND Data = ? AND OraCheckOut IS NULL
                """,
                    (ora_checkout_str, angajat_id, azi),
                )
                mesaj_final += f"Check-out la WC {wc_id} la {ora_checkout_str} (durata {durata_str})\n"

            ora_checkin_str = acum.strftime("%H:%M:%S")
            cursor.execute(
                """
                INSERT INTO PontajWorkCenter (ID, WorkCenterID, Data, OraCheckIn)
                VALUES (?, ?, ?, ?)
            """,
                (angajat_id, workcenter_id, azi, ora_checkin_str),
            )
            cursor.execute(
                """
                INSERT INTO ProductieAngajati (ID, Data, OraCheckIn)
                VALUES (?, ?, ?)
            """,
                (angajat_id, azi, ora_checkin_str),
            )
            mesaj_final += f"Check-in la WC {workcenter_id} la {ora_checkin_str}\n"
    else:
        ora_checkin_str = acum.strftime("%H:%M:%S")
        cursor.execute(
            """
            INSERT INTO PontajWorkCenter (ID, WorkCenterID, Data, OraCheckIn)
            VALUES (?, ?, ?, ?)
        """,
            (angajat_id, workcenter_id, azi, ora_checkin_str),
        )
        cursor.execute(
            """
            INSERT INTO ProductieAngajati (ID, Data, OraCheckIn)
            VALUES (?, ?, ?)
        """,
            (angajat_id, azi, ora_checkin_str),
        )
        mesaj_final += f"Check-in la WC {workcenter_id} la {ora_checkin_str}\n"

    conn.commit()
    cursor.close()
    conn.close()
    return mesaj_final.strip()


def direct_scan(hce_id, uid, workcenter_id):
    normalized_hce_id = normalize_scan_value(hce_id)
    normalized_uid = normalize_card_code(uid)
    dedup_key = normalized_hce_id or normalized_uid
    append_debug_log(
        f"Scan procesat: HCE raw='{hce_id}' | HCE norm='{normalized_hce_id}' | "
        f"UID raw='{uid}' | UID norm='{normalized_uid}' | WC='{workcenter_id}'"
    )

    if not dedup_key or not workcenter_id:
        append_debug_log("Scan invalid: lipseste HCE/UID sau WorkCenterID")
        return False, "HCE/UID sau WorkCenterID lipsa", ""

    now_timestamp = time.time()
    last_time = recent_scan_keys.get(dedup_key, 0)
    if now_timestamp - last_time < REPEAT_SCAN_DELAY_SECONDS:
        append_debug_log(f"Scan blocat de deduplicare pentru cheia {dedup_key}")
        return False, "Asteapta 5 secunde intre scanari cu acelasi dispozitiv.", ""

    recent_scan_keys[dedup_key] = now_timestamp

    try:
        source, row = resolve_employee(normalized_hce_id, normalized_uid)
        if not row:
            append_debug_log("Rezultat final: fara angajat gasit")
            return False, "Telefon sau cartela nerecunoscuta", ""

        angajat_id, nume, prenume = row
        nume_complet = f"{nume} {prenume}"
        append_debug_log(f"Rezultat final: match pe {source} -> {angajat_id} {nume_complet}")
        mesaj_final = process_pontaj_for_employee(angajat_id, workcenter_id)
        return True, mesaj_final, nume_complet
    except Exception as e:
        append_debug_log(f"Eroare in direct_scan: {e}")
        return False, f"Eroare DB: {e}", ""


root = tk.Tk()
root.title("Scanare Pontaj WorkCenter")
root.geometry("600x300")
root.resizable(False, False)

tk.Label(root, text="Selecteaza WorkCenter:", font=("Arial", 12)).pack(pady=10)
workcenter_var = tk.StringVar()
workcenter_dropdown = ttk.Combobox(root, textvariable=workcenter_var, state="disabled")
workcenter_dropdown.pack()
workcenter_dropdown.bind("<Button-1>", lambda event: solicita_parola())

workcenter_dropdown["values"] = [
    "1 - Laser",
    "2 - Abkant",
    "3 - Strung",
    "4 - Fierastrau",
    "5 - Sudura TIG",
    "6 - Sudura MIG-MAG",
    "7 - Asamblare",
    "8 - Lacatuserie",
    "9 - Prelucrari",
    "10 - Sudura prin puncte",
    "11 - Vopsire",
    "12 - Zincare electrolitica",
    "13 - Proiectare 3D",
    "14 - Colaborare",
    "15 - Gaurire",
    "16 - Control",
    "17 - Ambalare cu Palet",
    "18 - Zencuire",
    "19 - Satinare",
    "20 - Ambalare Colet",
    "21 - Sudura LASER",
    "22 - Anodizare natur",
    "23 - Sablare fina microblasting",
    "24 - Frezare 3 axe",
    "1024 - Ambalare Bulk",
]

saved_id = load_workcenter_config()
if saved_id:
    for value in workcenter_dropdown["values"]:
        if value.startswith(f"{saved_id} "):
            workcenter_var.set(value)
            selected_workcenter_id = saved_id
            break

nume_label = tk.Label(root, text="", font=("Arial", 14, "bold"))
nume_label.pack(pady=5)
mesaj_label = tk.Label(root, text="", font=("Arial", 12))
mesaj_label.pack(pady=5)

debug_button = tk.Button(root, text="Debug", command=show_debug_window, font=("Arial", 10))
debug_button.pack(pady=5)


def on_scan_detected(uid, hce_id):
    global selected_workcenter_id
    try:
        selected = workcenter_var.get()
        if not selected:
            mesaj_label.config(text="Selecteaza WorkCenter!", fg="red")
            return

        selected_workcenter_id = int(selected.split(" - ")[0])
        save_workcenter_config(selected_workcenter_id)
        append_debug_log(f"WorkCenter selectat: {selected_workcenter_id}")
        scan(uid, hce_id)
    except Exception as e:
        append_debug_log(f"Eroare scanare UI: {e}")
        mesaj_label.config(text=f"Eroare scanare: {e}", fg="red")


def get_preferred_scan_data(initial_scan_data):
    uid = initial_scan_data.get("uid", "")
    hce_id = initial_scan_data.get("hce_id", "")
    reader_name = initial_scan_data.get("reader", "")
    append_debug_log(f"Citire initiala reader: reader='{reader_name}' | uid='{uid}' | hce='{hce_id}'")

    if hce_id:
        append_debug_log("HCE detectat imediat, folosesc telefonul")
        return uid, hce_id

    if not uid:
        append_debug_log("Nicio valoare utila detectata la citirea initiala")
        return "", ""

    deadline = time.time() + PHONE_SCAN_WAIT_SECONDS
    while time.time() < deadline:
        time.sleep(PHONE_SCAN_POLL_SECONDS)
        retry_scan_data = get_scan_data()
        if not retry_scan_data:
            continue

        retry_uid = retry_scan_data.get("uid", "") or uid
        retry_hce_id = retry_scan_data.get("hce_id", "")
        retry_reader_name = retry_scan_data.get("reader", "")
        append_debug_log(
            f"Retry reader: reader='{retry_reader_name}' | uid='{retry_uid}' | hce='{retry_hce_id}'"
        )
        if retry_hce_id:
            append_debug_log("HCE detectat in fereastra de asteptare")
            return retry_uid, retry_hce_id

    append_debug_log("Nu a aparut HCE in timp util, folosesc UID-ul de cartela")
    return uid, ""


def nfc_thread():
    global last_scan_key, last_scan_time
    while True:
        scan_data = get_scan_data()
        if scan_data:
            uid, hce_id = get_preferred_scan_data(scan_data)
            scan_key = normalize_scan_value(hce_id) or normalize_scan_value(uid)
            current_time = time.time()

            if scan_key and (scan_key != last_scan_key or (current_time - last_scan_time) > 3):
                last_scan_key = scan_key
                last_scan_time = current_time
                append_debug_log(f"Scan acceptat de thread: uid='{uid}' | hce='{hce_id}' | key='{scan_key}'")
                on_scan_detected(uid, hce_id)

        time.sleep(1)


threading.Thread(target=nfc_thread, daemon=True).start()


def notificare_check(nume, tip_eveniment):
    toaster.show_toast(
        "Scanare Pontaj",
        f"{nume}\nEveniment: {tip_eveniment}",
        icon_path=get_icon_path(),
        duration=5,
        threaded=True,
    )


def scan(uid, hce_id):
    global selected_workcenter_id

    success, mesaj, nume_complet = direct_scan(hce_id, uid, selected_workcenter_id)
    append_debug_log(f"Mesaj final aplicatie: success={success} | mesaj='{mesaj}' | nume='{nume_complet}'")
    if success:
        mesaj_label.config(text=mesaj or "Succes!", fg="green")
        nume_label.config(text=nume_complet)

        mesaj_lower = (mesaj or "").lower()
        if "check-out" in mesaj_lower or "checkout" in mesaj_lower:
            tip_eveniment = "Check-out"
        elif "check-in" in mesaj_lower or "checkin" in mesaj_lower:
            tip_eveniment = "Check-in"
        else:
            tip_eveniment = "Scanare"

        notificare_check(nume_complet, tip_eveniment)
    else:
        mesaj_label.config(text=mesaj, fg="red")


def on_quit(icon, item):
    if icon:
        icon.stop()
    root.after(0, root.destroy)
    sys.exit()


def show_window(icon=None, item=None):
    if tray_icon:
        tray_icon.visible = False
    root.after(0, root.deiconify)
    root.after(0, root.lift)
    root.after(0, root.focus_force)


def ensure_tray_icon():
    global tray_icon, tray_image
    if tray_icon is not None:
        return

    tray_image = load_tray_image()
    menu = pystray.Menu(
        pystray.MenuItem("Deschide", show_window),
        pystray.MenuItem("Iesi", on_quit),
    )
    tray_icon = pystray.Icon(
        "Pontaj",
        tray_image,
        "Pontaj Smart",
        menu,
    )
    threading.Thread(target=tray_icon.run, daemon=True).start()
    time.sleep(0.3)


def hide_window():
    global tray_icon
    root.withdraw()
    ensure_tray_icon()
    tray_icon.visible = True


def on_closing():
    hide_window()


root.protocol("WM_DELETE_WINDOW", on_closing)
root.mainloop()

import tkinter as tk
from tkinter import ttk
import threading
import time
from nfc_reader import get_uid
import requests
from tkinter import simpledialog
import json
import os
import pystray
from PIL import Image
import sys
from win10toast import ToastNotifier

last_uid_seen = None
last_scan_time = 0
selected_workcenter_id = None
CONFIG_FILE = "config.json"
tray_icon = None
toaster = ToastNotifier()

def solicita_parola():
    if workcenter_dropdown['state'] == 'readonly':
        return  # dacă e deja activat, nu mai cerem parola

    parola = simpledialog.askstring("Autentificare", "Introduceți parola:", show='*')
    if parola == "XXX":  # aici schimbi parola cum vrei
        workcenter_dropdown.config(state="readonly")
        mesaj_label.config(text="🔓 Selectează WorkCenter", fg="green")
    else:
        mesaj_label.config(text="❌ Parolă greșită", fg="red")

def save_workcenter_config(workcenter_id):
    with open(CONFIG_FILE, "w") as f:
        json.dump({"selected_workcenter_id": workcenter_id}, f)

def load_workcenter_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
            try:
                data = json.load(f)
                return data.get("selected_workcenter_id")
            except:
                return None
    return None


# UI Setup
root = tk.Tk()
root.title("Scanare Pontaj WorkCenter")
root.geometry("600x300")
root.resizable(False, False)

# Dropdown WorkCenter
tk.Label(root, text="Selectează WorkCenter:", font=("Arial", 12)).pack(pady=10)
workcenter_var = tk.StringVar()
workcenter_dropdown = ttk.Combobox(root, textvariable=workcenter_var, state="disabled")
workcenter_dropdown.pack()
workcenter_dropdown.bind("<Button-1>", lambda event: solicita_parola())

workcenter_dropdown['values'] = [
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
    "1024 - Ambalare Bulk"
]
saved_id = load_workcenter_config()
if saved_id:
    for value in workcenter_dropdown['values']:
        if value.startswith(f"{saved_id} "):
            workcenter_var.set(value)
            selected_workcenter_id = saved_id
            break

# Mesaje și UID
nume_label = tk.Label(root, text="", font=("Arial", 14, "bold"))
nume_label.pack(pady=5)
mesaj_label = tk.Label(root, text="", font=("Arial", 12))
mesaj_label.pack(pady=5)




def on_uid_detected(uid):
    global selected_workcenter_id
    try:
        selected = workcenter_var.get()
        if not selected:
            mesaj_label.config(text="⚠️ Selectează WorkCenter!", fg="red")
            return

        selected_workcenter_id = int(selected.split(" - ")[0])
        save_workcenter_config(selected_workcenter_id)
        scan(uid)
    except Exception as e:
        mesaj_label.config(text=f"Eroare UID: {e}", fg="red")

# Thread NFC monitor
def nfc_thread():
    global last_uid_seen, last_scan_time
    while True:
        uid = get_uid()
        if uid:
            current_time = time.time()
            if uid != last_uid_seen or (current_time - last_scan_time) > 3:
                last_uid_seen = uid
                last_scan_time = current_time
                print(f"🔍 UID detectat: {uid}")
                on_uid_detected(uid)

        time.sleep(1)


# Pornește thread NFC
threading.Thread(target=nfc_thread, daemon=True).start()

def notificare_check(nume, tip_eveniment):
    from win10toast import ToastNotifier
    toaster = ToastNotifier()
    toaster.show_toast(
        "Scanare Pontaj",
        f"{nume}\nEveniment: {tip_eveniment}",
        icon_path=r"C:\Users\notki\PycharmProjects\PontajWorkCenter\card.ico",
        duration=5,
        threaded=True
    )



def scan(uid):
    global selected_workcenter_id

    try:
        payload = {
            "uid": uid,
            "workcenter_id": selected_workcenter_id
        }
        response = requests.post("http://192.168.2.1:9975/api/log_uid", json=payload)
        try:
            data = response.json()
        except Exception:
            mesaj_label.config(text=f"❌ Răspuns invalid: {response.status_code} - {response.text}", fg="red")
            return

        if data.get("success"):
            nume_complet = data.get("nume_complet", "")
            mesaj_label.config(text=data.get("mesaj", "🟢 Succes!"), fg="green")
            nume_label.config(text=nume_complet)

            # Determină tipul de eveniment în funcție de textul din 'mesaj'
            mesaj = data.get("mesaj", "").lower()
            if "check-out" in mesaj or "checkout" in mesaj:
                tip_eveniment = "Check-out"
            elif "check-in" in mesaj or "checkin" in mesaj:
                tip_eveniment = "Check-in"
            else:
                tip_eveniment = "Scanare"

            notificare_check(nume_complet, tip_eveniment)
        else:
            mesaj_label.config(text="❌ " + data.get("message", "Eroare la server"), fg="red")

    except Exception as e:
        mesaj_label.config(text=f"❌ Eroare trimitere: {e}", fg="red")


def on_quit(icon, item):
    icon.stop()
    root.after(0, root.destroy)
    sys.exit()

def show_window(icon=None, item=None):
    if tray_icon:
        tray_icon.visible = False
    root.after(0, root.deiconify)

def hide_window():
    global tray_icon
    root.withdraw()
    if tray_icon is None:
        image = Image.open(r"C:\Users\notki\PycharmProjects\PontajWorkCenter\card.ico")
        menu = pystray.Menu(
            pystray.MenuItem('Deschide', show_window),
            pystray.MenuItem('Ieși', on_quit)
        )
        tray_icon = pystray.Icon(
            "Pontaj",
            image,
            "Pontaj Smart",
            menu,
            on_click=show_window  # AICI e cheia!
        )
        tray_icon.run_detached()
        tray_icon.visible = True
    else:
        tray_icon.visible = True

def on_closing():
    hide_window()

root.protocol("WM_DELETE_WINDOW", on_closing)

root.mainloop()

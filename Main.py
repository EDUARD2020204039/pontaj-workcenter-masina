from flask import Flask, request, jsonify, render_template
import pyodbc
from datetime import datetime, date
import time
from datetime import datetime, timedelta
import schedule
import threading
import ipaddress
app = Flask(__name__)
log_messages = []
uid_last_scans = {}  # UID -> timestamp

ALLOWED_NETWORKS = [
    ipaddress.ip_network('192.168.2.0/24'),
    ipaddress.ip_network('192.168.68.0/24')
]


def is_allowed_ip(ip):
    try:
        ip_addr = ipaddress.ip_address(ip)
        return any(ip_addr in net for net in ALLOWED_NETWORKS)
    except ValueError:
        return False

def restrict_ip():
    print(f"IP primit: {request.remote_addr}")
    if not is_allowed_ip(request.remote_addr):
        return jsonify({"error": "Access denied"}), 403

db_config = {
    'server': '192.168.2.6',
    'database': 'Metal',
    'username': 'bogdan',
    'password': 'HELPAN123$',
    'driver': '{ODBC Driver 17 for SQL Server}'
}

def get_db_connection():
    return pyodbc.connect(
        f"DRIVER={db_config['driver']};"
        f"SERVER={db_config['server']};"
        f"DATABASE={db_config['database']};"
        f"UID={db_config['username']};"
        f"PWD={db_config['password']}"
    )

@app.route('/api/log', methods=['POST'])
def log_data():
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'message': 'Fără date primite'}), 400

    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute("""
            INSERT INTO PontajWorkCenter (ID, WorkCenterID, Data, OraCheckIn, OraCheckOut, DurataTotala)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            data.get("ID"),
            data.get("WorkCenterID"),
            data.get("Data"),
            data.get("OraCheckIn"),
            data.get("OraCheckOut"),
            data.get("DurataTotala")
        ))

        conn.commit()
        cursor.close()
        conn.close()

        return jsonify({'success': True})

    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/api/log_uid', methods=['POST'])
def log_uid():
    data = request.get_json()
    uid = data.get("uid")
    workcenter_id = data.get("workcenter_id")

    now_timestamp = time.time()
    last_time = uid_last_scans.get(uid, 0)

    if now_timestamp - last_time < 10:
        log(f"⛔ UID {uid} scanat recent. Ignorăm.")
        return jsonify({'success': False, 'message': "⏳ Așteaptă 10 secunde între scanări cu același card."})

    uid_last_scans[uid] = now_timestamp
    log(f"📥 START log_uid | UID={uid} | WorkCenterID={workcenter_id}")

    if not uid or not workcenter_id:
        log("❌ Lipsă UID sau WorkCenterID.")
        return jsonify({'success': False, 'message': 'UID sau WorkCenterID lipsă'}), 400

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        azi = date.today()
        acum = datetime.now()

        # Verificăm dacă e angajat valid
        cursor.execute("SELECT ID, Nume, Prenume FROM Angajati WHERE COD_CARTELA = ?", (uid,))
        row = cursor.fetchone()
        if not row:
            log("❌ Card nerecunoscut.")
            return jsonify({'success': False, 'message': 'Card nerecunoscut'}), 404

        angajat_id, nume, prenume = row
        nume_complet = f"{nume} {prenume}"
        mesaj_final = ""
        log(f"👤 Angajat: {nume_complet} (ID: {angajat_id})")

        # Verificăm dacă există sesiune deschisă
        cursor.execute("""
            SELECT WorkCenterID, OraCheckIn
            FROM PontajWorkCenter
            WHERE ID = ? AND Data = ? AND OraCheckOut IS NULL
        """, (angajat_id, azi))
        rows = cursor.fetchall()

        if rows:
            # Verificăm dacă există check-in la workcenterul curent
            already_checked_in_current = False
            for wc_id, ora_checkin in rows:
                if str(wc_id) == str(workcenter_id):
                    already_checked_in_current = True

            # Dacă există check-in la workcenterul curent: Fă check-out aici
            if already_checked_in_current:
                for wc_id, ora_checkin in rows:
                    if str(wc_id) == str(workcenter_id):
                        log(f"🔁 Închidem sesiune WC {wc_id}")
                        datetime_checkin = datetime.combine(azi, ora_checkin)
                        durata = acum - datetime_checkin
                        durata_str = str(durata).split('.')[0]
                        ora_checkout_str = acum.strftime("%H:%M:%S")
                        cursor.execute("""
                            UPDATE PontajWorkCenter
                            SET OraCheckOut = ?, DurataTotala = ?
                            WHERE ID = ? AND Data = ? AND WorkCenterID = ? AND OraCheckIn = ?
                        """, (ora_checkout_str, durata_str, angajat_id, azi, wc_id, ora_checkin))
                        cursor.execute("""
                            UPDATE ProductieAngajati
                            SET OraCheckOut = ?
                            WHERE ID = ? AND Data = ? AND OraCheckOut IS NULL
                        """, (ora_checkout_str, angajat_id, azi))
                        mesaj_final += f"📤 Check-out la WC {wc_id} la {ora_checkout_str} (durată {durata_str})\n"
            else:
                # Există check-in la alt workcenter
                for wc_id, ora_checkin in rows:
                    log(f"🔁 Închidem sesiune WC {wc_id}")
                    datetime_checkin = datetime.combine(azi, ora_checkin)
                    durata = acum - datetime_checkin
                    durata_str = str(durata).split('.')[0]
                    ora_checkout_str = acum.strftime("%H:%M:%S")
                    cursor.execute("""
                        UPDATE PontajWorkCenter
                        SET OraCheckOut = ?, DurataTotala = ?
                        WHERE ID = ? AND Data = ? AND WorkCenterID = ? AND OraCheckIn = ?
                    """, (ora_checkout_str, durata_str, angajat_id, azi, wc_id, ora_checkin))
                    cursor.execute("""
                        UPDATE ProductieAngajati
                        SET OraCheckOut = ?
                        WHERE ID = ? AND Data = ? AND OraCheckOut IS NULL
                    """, (ora_checkout_str, angajat_id, azi))
                    mesaj_final += f"📤 Check-out la WC {wc_id} la {ora_checkout_str} (durată {durata_str})\n"

                # Facem check-in la workcenterul curent
                ora_checkin_str = acum.strftime("%H:%M:%S")
                log(f"🟢 Check-in nou în WC {workcenter_id} la {ora_checkin_str}")
                cursor.execute("""
                    INSERT INTO PontajWorkCenter (ID, WorkCenterID, Data, OraCheckIn)
                    VALUES (?, ?, ?, ?)
                """, (angajat_id, workcenter_id, azi, ora_checkin_str))
                cursor.execute("""
                    INSERT INTO ProductieAngajati (ID, Data, OraCheckIn)
                    VALUES (?, ?, ?)
                """, (angajat_id, azi, ora_checkin_str))
                mesaj_final += f"📥 Check-in la WC {workcenter_id} la {ora_checkin_str}\n"
        else:
            # Nu există sesiune → check-in la workcenterul curent
            ora_checkin_str = acum.strftime("%H:%M:%S")
            log(f"🟢 Check-in nou în WC {workcenter_id} la {ora_checkin_str}")
            cursor.execute("""
                INSERT INTO PontajWorkCenter (ID, WorkCenterID, Data, OraCheckIn)
                VALUES (?, ?, ?, ?)
            """, (angajat_id, workcenter_id, azi, ora_checkin_str))
            cursor.execute("""
                INSERT INTO ProductieAngajati (ID, Data, OraCheckIn)
                VALUES (?, ?, ?)
            """, (angajat_id, azi, ora_checkin_str))
            mesaj_final += f"📥 Check-in la WC {workcenter_id} la {ora_checkin_str}\n"

        conn.commit()
        cursor.close()
        conn.close()

        log(f"✅ {mesaj_final}")
        return jsonify({'success': True, 'nume_complet': nume_complet, 'mesaj': mesaj_final})

    except Exception as e:
        log(f"❗ Eroare server: {str(e)}")
        return jsonify({'success': False, 'message': f"Eroare server: {str(e)}"}), 500


@app.route('/')
def index():
    return render_template('logs.html', logs=log_messages[::-1])  # cele mai noi sus

def log(msg):
    timestamp = datetime.now().strftime("%H:%M:%S")
    formatted = f"{timestamp}|Main     | {msg}"
    log_messages.append(formatted)
    print(formatted)
    if len(log_messages) > 100:
        log_messages.pop(0)


def inchide_automate_sesuni():
    try:
        print(f"[AutoClose] 🔔 Pornim verificarea automată la {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

        conn = get_db_connection()
        cursor = conn.cursor()

        azi = date.today()
        ora_finala = datetime.strptime("23:59:00", "%H:%M:%S").time()

        cursor.execute("""
            SELECT ID, WorkCenterID, Data, OraCheckIn
            FROM PontajWorkCenter
            WHERE Data < ? AND OraCheckOut IS NULL
        """, (azi,))
        rows = cursor.fetchall()

        print(f"[AutoClose] 🔍 Găsite {len(rows)} sesiuni neînchise cu dată < {azi}")

        for id_angajat, wc_id, data_sesiune, ora_checkin in rows:
            checkin_datetime = datetime.combine(data_sesiune, ora_checkin)
            checkout_datetime = datetime.combine(data_sesiune, ora_finala)

            if checkin_datetime > checkout_datetime:
                print(f"[AutoClose] ⚠️ OraCheckIn > 23:59 pentru ID {id_angajat}, WC {wc_id} — ignorat")
                continue

            durata = checkout_datetime - checkin_datetime
            durata_str = str(durata).split('.')[0]

            cursor.execute("""
                UPDATE PontajWorkCenter
                SET OraCheckOut = ?, DurataTotala = ?, Avertisment = 1
                WHERE ID = ? AND Data = ? AND WorkCenterID = ? AND OraCheckOut IS NULL
            """, (
                ora_finala.strftime("%H:%M:%S"),
                durata_str,
                id_angajat,
                data_sesiune,
                wc_id
            ))
            print(f"[AutoClose] ✅ Închis WC {wc_id} pentru angajat {id_angajat}")

        conn.commit()
        cursor.close()
        conn.close()

        if not rows:
            print("[AutoClose] ℹ️ Nicio sesiune de închis.")
        else:
            print(f"[AutoClose] ✅ S-au închis {len(rows)} sesiuni.")

    except Exception as e:
        print(f"[AutoClose] ❌ Eroare la închiderea automată: {e}")

@app.route('/api/angajati_pontati')
def angajati_pontati():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT A.Nume, A.Prenume, WC.Denumire
        FROM PontajWorkCenter P
        INNER JOIN Angajati A ON P.ID = A.ID
        INNER JOIN WorkCenter WC ON P.WorkCenterID = WC.WorkCenterID
        WHERE P.OraCheckOut IS NULL
        ORDER BY WC.Denumire, A.Nume, A.Prenume
    """)
    rows = cursor.fetchall()
    cursor.close()
    conn.close()

    rezultat = [
        f"{r[0]} {r[1]} - {r[2]}"
        for r in rows
    ]
    return jsonify(rezultat)



@app.route('/api/angajati_inactivi')
def angajati_inactivi():
    conn = get_db_connection()
    cursor = conn.cursor()

    # Pas 1: toți angajații activi (au cel puțin o sesiune deschisă)
    cursor.execute("""
        SELECT DISTINCT ID
        FROM PontajWorkCenter
        WHERE OraCheckOut IS NULL
    """)
    activi = set(row[0] for row in cursor.fetchall())

    # Pas 2: toți angajații din tabelă
    cursor.execute("SELECT ID, Nume, Prenume FROM Angajati")
    toti_angajatii = cursor.fetchall()

    # Pas 3: Excludem angajații activi
    inactivi = [f"{r[1]} {r[2]}" for r in toti_angajatii if r[0] not in activi]

    cursor.close()
    conn.close()
    return jsonify(inactivi)



def ruleaza_scheduler():
    while True:
        schedule.run_pending()
        time.sleep(60)


schedule.every().day.at("01:00").do(inchide_automate_sesuni)
threading.Thread(target=ruleaza_scheduler, daemon=True).start()


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=9975)

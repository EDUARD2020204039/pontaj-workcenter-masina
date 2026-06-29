import platform
from smartcard.System import readers
from smartcard.util import toHexString
from smartcard.Exceptions import NoCardException, CardConnectionException

IS_WINDOWS = platform.system() == "Windows"

SELECT_AID_APDUS = [
    [0x00, 0xA4, 0x04, 0x00, 0x07, 0xF0, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x00],
    [0x00, 0xA4, 0x04, 0x00, 0x05, 0xF2, 0x22, 0x22, 0x22, 0x22, 0x00],
]


def _read_single_reader(reader):
    connection = reader.createConnection()
    connection.connect()

    uid = ""
    hce_id = ""

    data, sw1, sw2 = connection.transmit([0xFF, 0xCA, 0x00, 0x00, 0x00])
    if sw1 == 0x90 and sw2 == 0x00 and data:
        uid = toHexString(data)

    try:
        for apdu in SELECT_AID_APDUS:
            hce_data, hce_sw1, hce_sw2 = connection.transmit(apdu)
            if hce_sw1 == 0x90 and hce_sw2 == 0x00 and hce_data:
                hce_id = toHexString(hce_data)
                break
    except Exception:
        hce_id = ""

    if uid or hce_id:
        return {"uid": uid, "hce_id": hce_id, "reader": str(reader)}

    return None


def get_scan_data():
    if not IS_WINDOWS:
        raise NotImplementedError("Functia get_scan_data este implementata doar pentru Windows in acest moment.")

    available_readers = readers()
    if not available_readers:
        print("Nu s-au gasit cititoare.")
        return None

    fallback_uid_scan = None
    for reader in available_readers:
        try:
            scan_data = _read_single_reader(reader)
            if not scan_data:
                continue
            if scan_data.get("hce_id"):
                return scan_data
            if scan_data.get("uid") and fallback_uid_scan is None:
                fallback_uid_scan = scan_data
        except (NoCardException, CardConnectionException):
            continue
        except Exception as e:
            print(f"Eroare NFC pe reader {reader}: {e}")

    if fallback_uid_scan:
        return fallback_uid_scan

    return None


def get_uid():
    scan_data = get_scan_data()
    if not scan_data:
        return None
    return scan_data.get("uid") or scan_data.get("hce_id")

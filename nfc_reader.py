import platform
from smartcard.System import readers
from smartcard.util import toHexString
from smartcard.Exceptions import NoCardException, CardConnectionException
import time

IS_WINDOWS = platform.system() == "Windows"

def get_uid():
    if not IS_WINDOWS:
        raise NotImplementedError("Funcția get_uid este implementată doar pentru Windows în acest moment.")

    r = readers()
    if not r:
        print("❌ Nu s-au găsit cititoare.")
        return None

    reader = r[0]
    connection = reader.createConnection()

    try:
        connection.connect()
        data, sw1, sw2 = connection.transmit([0xFF, 0xCA, 0x00, 0x00, 0x00])
        if sw1 == 0x90 and sw2 == 0x00:
            return toHexString(data)
    except (NoCardException, CardConnectionException):
        return None
    except Exception as e:
        print("Eroare NFC:", e)
    return None

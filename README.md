# WorkCenter Pontaj

Sistem de pontaj pe centre de lucru, cu client Windows pentru NFC/PIN și server central pentru monitorizare, versiuni și actualizări automate.

## Container Unraid

- Imagine: `ghcr.io/unraidg5/workcenterpontare:latest`
- Port container: `3490`
- Volum persistent obligatoriu: `/data`
- Fus orar implicit: `Europe/Bucharest`

Variabile opționale pentru suprascrierea conexiunii incluse:

- `DB_SERVER`
- `DB_DATABASE`
- `DB_USERNAME`
- `DB_PASSWORD`
- `DB_DRIVER` (implicit `ODBC Driver 18 for SQL Server`)
- `API_TOKEN` (dacă este configurat, protejează endpoint-urile de pontaj)

Pagina `http://192.168.2.23:3490/` afișează stațiile, versiunile, angajații activi și linkul pentru executabil.

## Client Windows

`WorkCenterPontaj.exe` este generat ca un singur fișier. Include bibliotecile Python, Microsoft ODBC Driver 18 și Visual C++ Runtime. Dacă driverul SQL nu este instalat, aplicația solicită drepturi de administrator și îl instalează.

La prima pornire:

1. apasă **Schimbă** și introdu parola de configurare;
2. selectează WorkCenter-ul;
3. dacă serverul de update nu este la adresa implicită, apasă **Server** și introdu adresa containerului.

Clientul acceptă cartelă NFC, telefon HCE și PIN. Pentru PIN caută automat în tabela `Angajati` prima coloană existentă dintre `PIN`, `PIN_PONTAJ`, `COD_PIN` și `PIN_ANGAJAT`.

## Versionare și update

Versiunea curentă este în fișierul `VERSION`. Orice release trebuie să îi mărească valoarea înainte de push. Workflow-ul:

1. construiește executabilul Windows;
2. îl include în imaginea Docker;
3. publică imaginea `latest` în GHCR;
4. Watchtower actualizează containerul;
5. clienții detectează noua versiune prin heartbeat, descarcă EXE-ul și se înlocuiesc automat.

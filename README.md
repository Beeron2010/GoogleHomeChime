# Protect Chime

Lokale Verbindung zwischen einer UniFi-Protect-Türklingel und Google-Home-/Cast-Lautsprechern. Die Weboberfläche verwaltet Klingelton, Geräte, Lautstärke und wiederkehrende Ruhezeiten je Lautsprecher.

## Start

1. Starten: `docker compose up -d --build`
2. Oberfläche öffnen: `http://<Docker-IP>:8787`
3. Protect-IP, Protect-API-Schlüssel und die automatisch vorgeschlagene Serveradresse im GUI speichern.
4. Klingelton hochladen, **Geräte suchen** drücken und die gewünschten Lautsprecher aktivieren. Die Google-Gerätenamen werden automatisch übernommen.

`network_mode: host` ist für die lokale Google-Cast-Erkennung per mDNS vorgesehen. Die im GUI angezeigte Serveradresse muss für die Lautsprecher im LAN erreichbar sein. Zugangsdaten und Regeln liegen ausschließlich in `./data`.

## Hinweise

- Zeitbasis ist standardmäßig `Europe/Berlin`.
- Eine Ruhezeit über Mitternacht, z. B. 20:00–07:00, wird unterstützt.
- Die UniFi-Anbindung nutzt die ereignisbasierte Public Integration API der `uiprotect`-Bibliothek.
- Das Projekt ist für Linux-Docker auf x86_64 und arm64 ausgelegt.

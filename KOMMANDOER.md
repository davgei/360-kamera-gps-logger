# Kommandoer

Kort oversikt over hva du kan kjøre. **TeamViewer-delen krever et assignment-token
du ikke har ennå** — alt annet kan testes uten.

## På PC-en (Windows) — bare sjekk at scriptene er gyldige

```bash
bash -n deploy/bootstrap.sh deploy/self-update.sh deploy/run-app.sh
```
Sjekker syntaks. Gjør ingen endringer. (Selve oppsettet kan kun kjøres på Pi-en.)

## På Raspberry Pi-en  (KUN her — IKKE på Windows)

> Disse kommandoene bruker `sudo`, `apt`, `systemctl` og `teamviewer`, som bare finnes
> på Raspberry Pi OS / Linux. Kjører du dem på Windows får du «command not found».

Start i repo-mappa:
```bash
git clone https://github.com/davgei/360-kamera-gps-logger.git
cd 360-kamera-gps-logger
```

**Oppsett uten token** (fungerer nå — hopper bare over TeamViewer-tilknytning):
```bash
sudo deploy/bootstrap.sh
```
Installerer git, Python (pip+venv) og TeamViewer Host, og slår på de to boot-tjenestene.

**Kjør boot-jobben manuelt** (uten å reboote):
```bash
sudo systemctl start 360logger-boot.service
journalctl -u 360logger-boot.service -b
```
Puller siste kode og sørger for at TeamViewer-daemonen kjører.

**Se app-tjenesten** (sier "no logger configured" til du fyller inn `APP_CMD` i `deploy/app.env`):
```bash
journalctl -u 360logger-app.service -b
```

## Når du får tokenet

```bash
sudo deploy/bootstrap.sh <TOKEN>   # knytter Pi-en til TeamViewer-kontoen
teamviewer info                    # viser TeamViewer-ID + tilknytningsstatus
```

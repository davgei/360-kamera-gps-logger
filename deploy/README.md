# deploy/ — Raspberry Pi 4 selvoppsett

Når du puller dette repoet på en fersk Raspberry Pi 4 og kjører bootstrap **én gang**,
setter Pi-en opp alt selv: installerer kode + TeamViewer, knytter seg til TeamViewer-kontoen
din, starter logger-appen, og oppdaterer seg selv ved hver boot.

## Førstegangsoppsett på Pi-en

```bash
git clone https://github.com/davgei/360-kamera-gps-logger.git
cd 360-kamera-gps-logger
sudo deploy/bootstrap.sh <TEAMVIEWER_ASSIGNMENT_TOKEN>
```

`bootstrap.sh`:
1. installerer `git` og **TeamViewer Host** (oppdager `armhf`/`arm64` selv),
2. aktiverer TeamViewer-daemonen og knytter enheten til kontoen med
   `teamviewer assignment --token …`,
3. installerer og aktiverer to tjenester: `360logger-boot.service` og `360logger-app.service`.

## Sikkerhet: tokenet havner aldri i repoet

TeamViewer assignment-tokenet er en hemmelighet og legges **aldri** i git. Du gir det enten:
- som argument til bootstrap (`sudo deploy/bootstrap.sh <token>`) — da lever det bare på Pi-en, eller
- i fila `deploy/teamviewer.token` — den står i `.gitignore`, så git ignorerer den helt og
  den blir aldri committet eller pushet.

Det som ligger på GitHub er kun scriptene. Hent tokenet i Management Console:
`Admin → Design & Deploy → Assignment` (eller `Account → Edit profile → Assignment`).

## Auto-start av logger-appen

Logger-appen kjøres som `360logger-app.service` (langtidskjørende, restarter automatisk ved
krasj). Den starter det du setter i `deploy/app.env` — en vanlig committet konfig-fil **uten**
hemmeligheter:

```bash
APP_CMD="python3 main.py"        # kommandoen som starter loggeren (tom = ingen app ennå)
APP_WORKDIR=""                   # mappe for kommandoen; tom = repo-rot
APP_REQUIREMENTS="requirements.txt"   # valgfritt; pip-installeres ved hver kodeoppdatering
```

Når logger-koden er på plass: fyll inn `APP_CMD`, commit og push. Alle Pi-er plukker det opp
ved neste boot (`self-update.sh` puller koden, installerer evt. deps og restarter appen).
Er `APP_CMD` tom, gjør tjenesten ingenting — den crash-looper ikke.

## Hva skjer ved hver boot

`360logger-boot.service` kjører `deploy/self-update.sh`, som:
- `git pull --ff-only` på repoet (selvoppdatering),
- sørger for at TeamViewer-daemonen kjører,
- pip-installerer `APP_REQUIREMENTS` hvis satt,
- restarter `360logger-app.service` så ny kode tas i bruk.

TeamViewer-tilknytningen skjer kun ved bootstrap (markør `/var/lib/360logger/teamviewer-assigned`).
Etterpå holder daemonen Pi-en online av seg selv.

## Test uten reboot

```bash
sudo systemctl start 360logger-boot.service 360logger-app.service
systemctl status 360logger-app.service
journalctl -u 360logger-boot.service -b
journalctl -u 360logger-app.service  -b
teamviewer info        # viser TeamViewer-ID og tilknytningsstatus
```

## Feilsøking

**Redigert på Windows?** Scriptene må ha LF-linjeskift (håndteres av `.gitattributes` i git).
Hvis de likevel har CRLF: `sudo apt-get install -y dos2unix && sudo dos2unix deploy/*.sh deploy/systemd/*.service`.

**TeamViewer-tilknytning feiler med EULA-feil.** TeamViewer krever at lisensvilkårene godtas
én gang. Kjør `sudo teamviewer setup` (eller godta i GUI via en remote-økt) og kjør
`sudo systemctl start 360logger-boot.service` på nytt. Vil du knytte til på nytt: slett
markøren `sudo rm /var/lib/360logger/teamviewer-assigned` og kjør bootstrap igjen.

**`--grant-easy-access` avvises.** Flagget finnes ikke i alle TeamViewer-versjoner. Fjern det
i `deploy/bootstrap.sh`; sett da tilgang via passord i stedet: `sudo teamviewer passwd <passord>`.

**Appen starter ikke.** Sjekk `journalctl -u 360logger-app.service -b`. Vanlige årsaker: `APP_CMD`
er tom, feil sti, eller manglende deps. Trenger appen maskinvare (kamera/GPS), må brukeren
(`APP_CMD` kjører som eieren av repoet) være i riktige grupper, f.eks. `video` og `dialout`.

**Privat repo.** `git clone`/`pull` over HTTPS trenger autentisering — bruk en deploy-key
(SSH) eller en token i remote-URL-en.

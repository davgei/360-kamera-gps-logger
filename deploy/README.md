# deploy/ — Raspberry Pi 4 selvoppsett

Når du puller dette repoet på en fersk Raspberry Pi 4 og kjører bootstrap **én gang**,
setter Pi-en opp alt selv: installerer kode + TeamViewer, knytter seg til TeamViewer-kontoen
din, og oppdaterer seg selv ved hver boot.

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
3. installerer og aktiverer `360logger-boot.service`.

**TeamViewer assignment-token** hentes i Management Console:
`Admin → Design & Deploy → Assignment` (eller `Account → Edit profile → Assignment`).
I stedet for å sende token som argument kan du legge den i `deploy/teamviewer.token`
(den er git-ignorert).

## Hva skjer ved hver boot

`360logger-boot.service` kjører `deploy/self-update.sh`, som:
- `git pull --ff-only` på repoet (selvoppdatering),
- sørger for at TeamViewer-daemonen kjører.

TeamViewer-tilknytningen skjer kun ved bootstrap (markør `/var/lib/360logger/teamviewer-assigned`).
Etterpå holder daemonen Pi-en online av seg selv.

## Test uten reboot

```bash
sudo systemctl start 360logger-boot.service
systemctl status 360logger-boot.service
journalctl -u 360logger-boot.service -b
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

**Privat repo.** `git clone`/`pull` over HTTPS trenger autentisering — bruk en deploy-key
(SSH) eller en token i remote-URL-en.

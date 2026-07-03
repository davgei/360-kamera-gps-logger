# Kommandoer

Kort jukselapp — forklaring i parentes. **Alle Pi-kommandoene kjøres på Raspberry Pi-en, ikke
på Windows.** Kjør dem fra repo-mappa:

```bash
cd ~/360-kamera-gps-logger
```

## Engangs-oppsett på Pi-en (gjør én gang)

```bash
# Avhengigheter: musetast, Google Drive, LED, fisheye-flatting (ffmpeg), ansiktssladding (deface), GPS
sudo apt-get install -y python3-evdev rclone python3-gpiozero python3-lgpio ffmpeg pipx python3-serial
pipx install deface        # ansiktssladding (laster ned opencv o.l. — kan ta et par minutter)

# La brukeren din lese musetasten uten sudo — LOGG UT OG INN igjen etterpå
sudo usermod -aG input prototype1-360-kamera-gps

# Koble rclone til Google Drive (veiviser: navn = gdrive, velg Google Drive, logg inn i nettleser)
rclone config

# Test at Drive-tilkoblingen virker (lister mappene i Google Drive)
rclone lsd gdrive:
```

## Filme — det vanlige

```bash
git pull                              # hent siste kode fra GitHub
python3 -m recorder.photo_session     # FOTO (gjeldende): muse-klikk = ett bilde, Ctrl+C = avslutt
python3 -m recorder.record_session    # VIDEO (eldre): muse-trykk = start/stopp opptak
python3 -m recorder.upload_pending    # last opp alle ventende lokale bilder nå (retry hvis offline)
```

Hvert klipp lastes opp til `gdrive:360-footage/clip_<tidspunkt>/` (begge `.mp4`-filene samlet).
Kameraets WiFi kobles til automatisk. Hvis ikke:

```bash
python3 recorder/connect_camera_wifi.py   # spør om kamera-passordet og kobler til
```

## Test / feilsøk — én del om gangen

```bash
python3 recorder/probe_camera.py      # svarer kameraet? (skriver ut modell + batteri)
python3 recorder/button_toggle.py     # toggler musetasten? (skriver START/STOP)
python3 recorder/record_clip.py       # ta opp ett 5-sekunders testklipp
rclone lsd gdrive:                    # virker Google Drive-tilkoblingen?
python3 -m recorder.status_leds --test  # lys hver LED etter tur (sjekk kobling)
python3 -m recorder.status_leds         # følg klar-status + batteri (grønn/rød LED)
python3 -m recorder.take_photo -o foto.jpg  # ta ett rått testbilde (dual-fisheye), ingen sladd/opplasting
python3 recorder/dewarp.py <bilde>.jpg  # gjør dual-fisheye om til flate/panorama-bilder (ffmpeg)
python3 -m recorder.bench_blur          # mål sladdefart på Pi-en (får «sladd på Pi» plass om natta?)
```

Steg-for-steg-test (ta bilde → flat → sladd), kjør på Pi-en på kameraets WiFi:

```bash
python3 -m recorder.take_photo -o foto.jpg                    # 1) rått bilde (to fisheye-bobler)
python3 recorder/dewarp.py foto.jpg --proj pannini --out-fov 190   # 2) → foto_pannini_yaw0.jpg + _yaw180.jpg
deface foto_pannini_yaw0.jpg -o foto_sladdet.jpg              # 3) sladd ansikter → foto_sladdet.jpg
python3 -m recorder.bench_blur --raw foto.jpg                 # 4) mål tid (legg til --scale 640x360 for rask)
```

Eller **hele kjeden på begge utsnitt i én kommando** (flat ut + sladd yaw0 *og* yaw180, med tid):

```bash
python3 -m recorder.process_photo --take                       # tar bilde + flat 1920 + sladd 640 på begge
python3 -m recorder.process_photo --input foto.jpg             # samme, på et bilde du allerede har
```
Lager `foto_pannini_yaw0_anonymized.jpg` + `foto_pannini_yaw180_anonymized.jpg` og skriver ut tiden
per steg (totalen = ett 3 m-punkt, 2 utsnitt).

Dukker ikke kameranettet opp i WiFi-lista? (Det sender på 5 GHz, channel 36.)

```bash
sudo raspi-config nonint do_wifi_country NO   # lås opp 5 GHz på Pi-en, prøv så igjen
```

## GPS (TBS M10Q over UART)

Kobling til Pi-ens 40-pins header (3.3V-logikk — ingen nivåomformer):

| Modul-ledning | Pi (fysisk pin) | Merknad |
|---------------|-----------------|---------|
| VCC           | 5V (pin 2 el. 4) | modulen regulerer selv 5V → 3.3V |
| GND           | GND (pin 6)      | felles jord |
| Tx            | RXD/GPIO15 (pin 10) | krysses (modulens Tx → Pi-ens RX) |
| Rx            | TXD/GPIO14 (pin 8)  | krysses (modulens Rx → Pi-ens TX) |
| SCL, SDA      | *ikke koblet*    | det er det innebygde kompasset, ikke GPS-en |

Skru på UART-en (engangs — krever omstart etterpå):

```bash
sudo raspi-config nonint do_serial_hw 0       # seriell maskinvare PÅ
sudo raspi-config nonint do_serial_cons 1     # seriell innloggingskonsoll AV (ellers spammer den porten)
printf 'enable_uart=1\ndtoverlay=disable-bt\n' | sudo tee -a /boot/firmware/config.txt
sudo systemctl disable hciuart                # frigjør den stabile UART-en (PL011) til GPIO14/15
sudo usermod -aG dialout prototype1-360-kamera-gps   # les serieporten uten sudo
sudo reboot
```

Sjekk og logg:

```bash
cat /dev/serial0                        # rå NMEA? ($GNGGA/$GNRMC-linjer = riktig kobling + baud)
python3 -m recorder.gps_logger          # logg breddegrad/lengdegrad hvert sekund (/dev/serial0 @ 115200)
python3 -m recorder.gps_logger --raw    # vis også rå NMEA (feilsøk kobling/baud)
python3 -m recorder.gps_logger --baud 9600   # hvis 115200 gir tomt (u-blox fabrikkstandard)
```

CSV-loggen havner i `~/360-gps-logs/gps_log_<tidspunkt>.csv` (én fil per økt). Første fix ute
under åpen himmel kan ta 30 s–et par minutter; til da står posisjonen tom (`fix=ingen fix`).

### GPS-utløser — tørrkjøring (uten kamera)

Tester utløser-logikken på ekte GPS og lagrer koordinaten der et bilde *ville* blitt tatt til fil.

```bash
# Modus «streetview» (STANDARD): bilde med jevne meter-mellomrom langs ruta, som Google-bilen
python3 -m recorder.trigger_preview                     # bilde hver 3. meter (standard)
python3 -m recorder.trigger_preview --interval-m 5      # annen avstand

# Modus «hentested»: utløs ved nærmeste passering av et hentested
python3 -m recorder.trigger_preview --mode hentested                 # mål = testkoordinaten 59.927870,10.825903
python3 -m recorder.trigger_preview --mode hentested --target 59.9279,10.8259
python3 -m recorder.trigger_preview --mode hentested --gate-m 25     # større slingringsmonn
python3 -m recorder.trigger_preview --mode hentested --targets-csv ~/hentesteder_001.csv
```

**hentested:** gå/kjør mot målet — når avstanden slutter å synke, «tas» et simulert bilde på
nærmeste passering. **streetview:** «tar» et bilde for hver `--interval-m` du beveger deg (stopp i
ro teller ikke — GPS-drift filtreres bort). Begge lagrer én rad per bilde i
`~/360-gps-logs/triggers_<tidspunkt>.csv` (tid, koordinat, avstand) og hele sporet i
`track_<tidspunkt>.csv` — så testen kan verifiseres selv om skjermen slås av.

### Kjøre-opptak (video + GPS) — `drive_session`

Filmer sammenhengende med kameraet og logger GPS samtidig, på samme klokke, så bildene kan
hentes ut hver 3. meter i etterkant på PC (Street View-stil). Krever kamera-WiFi + GPS koblet til.

```bash
python3 -m recorder.drive_session          # start opptak + GPS-logg; Ctrl+C stopper + laster ned video
python3 -m recorder.drive_session --no-download   # ikke last ned nå (hent fra SD-kortet senere)
```

Lagres i `~/360-drives/drive_<tidspunkt>/`: `session.json` (synk-tid), `gps_track.csv`, og
**begge** linse-filene (`…_10_…` og `…_00_…`) fra kameraet.

### Prosessere en økt (uttrekk → flat → sladd) — `process_drive`

Tar en `drive_<...>`-mappe (eller én video) og lager sladdede bilder. To modus:

```bash
# TEST uten GPS — én ramme hvert 2. sekund av videoen:
python3 -m recorder.process_drive --video ~/360-drives/drive_.../VID_..._10_...mp4 --interval-s 2

# EKTE — begge linser, ett bilde per 3 m langs GPS-sporet:
python3 -m recorder.process_drive --drive ~/360-drives/drive_20260703T071100 --spacing-m 3
```

For hver ramme: hent fra videoen → flat ut (enkelt-fisheye) → sladd (deface) → `<mappe>/blurred/`.
Standard flat 1920×1920, deface-skala 640×640. **Kalibrer `--fov`** (input-fisheye-FOV, standard 200)
mot et flatet bilde hvis kantene ser forvrengt ut. Rå video røres ikke her (opplasting + sletting
gjøres av kø-tjenesten).

### Kø-tjeneste (kjører 24/7) — `process_queue`

Prosesserer ferdige økter fortløpende, laster opp de sladdede bildene, og sletter rå video
**først når opplasting er bekreftet**. Ledig tid (kveld/helg) tar unna etterslep. Kjøres normalt
som boot-tjenesten `360logger-process`, men kan kjøres manuelt:

```bash
python3 -m recorder.process_queue --once        # ta én runde gjennom køen og avslutt (test)
python3 -m recorder.process_queue --no-upload    # prosesser + marker done, men ikke last opp
python3 -m recorder.process_queue --keep-raw     # ikke slett rå video etter opplasting
python3 -m recorder.process_queue                # kjør løkka (som tjenesten gjør)
```

Laster opp til `gdrive:360-streetview/<økt>/`. Markører i økt-mappa: `.processed` (bilder laget),
`.done` (lastet opp + rå slettet), `.error` (feilet — hoppes over, beholdes for inspeksjon).

### Autonomt opptak (kjører 24/7) — `auto_record`

GPS-styrt opptaks-kontroller (boot-tjenesten `360logger-drive`). Starter opptak **først når GPS har
beveget seg >10 m**, stopper hvis GPS mistes, stopper etter **3 min i ro** (venter til bevegelse), og
roter opptaket i **10-minutters biter** som lastes ned og **slettes fra kameraet** (så 256 GB-SD-en
ikke fylles). Hver bit blir en `~/360-drives/drive_<tid>/`-økt som kø-tjenesten plukker opp.

```bash
python3 -m recorder.auto_record                     # kjør kontrolleren (som tjenesten gjør)
python3 -m recorder.auto_record --stationary-min 3 --segment-min 10   # standardverdier
python3 -m recorder.auto_record --keep-on-camera    # ikke slett fra kameraet (feilsøk)
```

**LED-er under autonomt opptak:** 🔵 blå = GPS-fix · 🟢 grønn = filmer nå · 🔴 rød = kamera ikke nåbart.

## Oppsett-laget (deploy) — git-autopull + TeamViewer ved boot

Boot-tjenester (video-pipelinen): **boot** (git-pull) → **drive** (autonomt opptak) →
**process** (uttrekk+sladd+opplasting) → **upload** (rest-opplasting). Klikk-foto (`360logger-photo`)
auto-starter **ikke** lenger (den brukte kameraet og kolliderte); kjør den manuelt ved behov.

```bash
sudo bash deploy/bootstrap.sh                  # engangs: installerer + slår på boot-tjenestene
journalctl -u 360logger-drive.service -b -f    # følg det autonome opptaket (start/stopp/rotasjon)
journalctl -u 360logger-process.service -b -f  # følg kø-tjenesten (uttrekk → sladd → opplasting)
sudo systemctl stop 360logger-drive            # stopp auto-opptak (for å kjøre noe manuelt)
```

> Merk: kjør opptak og `rclone` som **din egen bruker (uten `sudo`)**. Med `sudo` leter rclone
> etter root sin konfig og finner ikke Google Drive-en din.

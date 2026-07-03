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
python3 recorder/dewarp.py <bilde>.jpg  # gjør dual-fisheye om til flate/panorama-bilder (ffmpeg)
python3 -m recorder.bench_blur          # mål sladdefart på Pi-en (får «sladd på Pi» plass om natta?)
```

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
videofilene fra kameraet. Kopier hele mappa til PC-en (scp/USB) — der kjører selve uttrekket
(video → bilde per 3 m → flat → sladd), som bygges videre i `streetview_light`.

## Oppsett-laget (deploy) — git-autopull + TeamViewer ved boot

```bash
sudo bash deploy/bootstrap.sh                 # engangs: installerer + slår på boot-tjenestene
journalctl -u 360logger-boot.service -b       # se loggen for boot-oppdateringen
journalctl -u 360logger-photo.service -b -f   # følg den auto-startede foto-øktas logg
sudo systemctl stop 360logger-photo           # stopp auto-økta (for å kjøre photo_session manuelt)
```

> Merk: kjør opptak og `rclone` som **din egen bruker (uten `sudo`)**. Med `sudo` leter rclone
> etter root sin konfig og finner ikke Google Drive-en din.

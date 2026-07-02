# recorder/ — opptaksmodul (under arbeid)

Målet: ta opp video på et **Insta360 ONE X** styrt fra Raspberry Pi-en, startet/stoppet med
et **USB-knappetrykk** (mus/fotbryter), og senere koblet sammen med GPS-logging og opplasting.

## Viktig om ONE X (fra research)

- Den **opprinnelige ONE X (2018)** styres via Insta360s offisielle **OSC HTTP-API**
  (`http://192.168.42.1/osc/...`) — *ikke* `insta360`-biblioteket på PyPI, som bare er
  testet på X3/X4.
- ONE X tar opp full 5.7K til sitt **eget microSD-kort**. Det finnes **ingen** måte å streame
  full kvalitet ut live → mønsteret blir: ta opp klipp → stopp → hent klippet til Pi-en →
  last opp fra Pi-en. (Detaljer ligger i prosjekt-minnet.)
- Pi-en må ha **ethernet (internett) + WiFi (kamera-AP) samtidig**. Du joiner kameraets WiFi
  på `wlan0`, og lar standardruta ligge på `eth0`.

## Steg 1 — koble Pi-en til kameraets WiFi

```bash
python3 recorder/connect_camera_wifi.py
```

Finner kameranettet (`ONE X …`), **spør om passordet i terminalen** (du skriver det inn — det
lagres aldri i koden), kobler til, og setter `ipv4.never-default` så **internett blir værende
på ethernet** (du mister ikke TeamViewer). Bruker NetworkManager (`nmcli`).

> Kameraet sender på 5 GHz (channel 36). Dukker ikke nettet opp, må WiFi-landet settes først:
> `sudo raspi-config nonint do_wifi_country NO` — ellers skjuler Pi-en alle 5 GHz-nett.

## Test 1 — kameratilkobling (ingen avhengigheter)

Når Pi-en er på kameraets WiFi (kameraet er da `192.168.42.1`):

```bash
python3 recorder/probe_camera.py
```

Bruker kun Python-standardbiblioteket. Den spør `/osc/info` og `/osc/state` og sier ifra om
OSC-API-et svarer (modell, firmware, batteri). Den **tar ikke opp noe**. Hvis den feiler, vet
vi at OSC ikke er veien for dette kameraet — før vi har bygget noe mer.

## Test 2 — USB-knapp (toggle)

Plugg inn musa/knappen i en USB-port på Pi-en:

```bash
sudo apt-get install -y python3-evdev          # engangs
python3 recorder/button_toggle.py              # auto-finn musknapp
python3 recorder/button_toggle.py --list       # list input-enheter
python3 recorder/button_toggle.py --device /dev/input/event3 --key BTN_LEFT
```

Trykk → `START recording`, trykk igjen → `STOP recording`. Ingen kamera involvert. For å lese
input uten `sudo`: `sudo usermod -aG input $USER` og logg inn på nytt.

## Ta opp et testklipp — `record_clip.py`

Når Pi-en er på kameraets WiFi:

```bash
python3 recorder/record_clip.py              # 5 sekunder
python3 recorder/record_clip.py --seconds 10
```

Setter video-modus → starter opptak → venter → stopper, og skriver ut **fil-URL-ene** kameraet
returnerer. ONE X lager to `.mp4` per klipp (én per linse). Den **tar opp et ekte klipp** på
kameraets SD-kort. Kommandosekvensen er verifisert mot Insta360s offisielle OSC-dokumentasjon.

> Får du `unactivated`: ONE X må aktiveres én gang i den offisielle Insta360-appen før OSC-API-et
> kan ta opp.

## Google Drive med rclone (engangs)

Opptakene lastes opp til Google Drive med `rclone`. Sett det opp én gang på Pi-en:

```bash
sudo apt-get install -y rclone
rclone config
```
I `rclone config`:
- `n` (new remote) → navn: **`gdrive`**
- Storage: velg **Google Drive** (`drive`)
- `client_id` / `client_secret`: tom (Enter)
- Scope: `1` (full tilgang)
- `service_account_file`: tom
- *Edit advanced config?* `n`
- *Use web browser to automatically authenticate?*
  - Pi med skrivebord/nettleser: `y` → logg inn med Google-kontoen din → tillat
  - Headless/SSH: `n` → kjør `rclone authorize "drive"` på en PC med nettleser, lim token tilbake
- *Configure this as a Shared Drive?* `n` → `y` (OK) → `q` (quit)

Test: `rclone lsd gdrive:` skal liste mappene i Drive. Tokenet lagres i
`~/.config/rclone/rclone.conf` på Pi-en — aldri i repoet.

## Ta bilder — `photo_session.py`  (gjeldende retning)

Foto-varianten: hvert **museklikk tar ett stillbilde** (ONE X dual-fisheye JPEG), som lastes ned
og opp til Google Drive. Kjør fra repo-roten:

```bash
python3 -m recorder.photo_session
```
- Klikk musetasten → ett bilde (`takePicture`, asynkron) → lastes ned til `photo_<tidspunkt>/` og
  opp til `gdrive:360-photos/photo_<tidspunkt>/`.
- Blå LED lyser mens bildet tas; rød/grønn = klar-status; kameraet må være nåbart (ellers nektes).
- Kjører til **Ctrl+C**. Valg: `--remote`, `--remote-path`, `--staging`, `--keep-local`, `--device`, `--key`, `--no-leds`.

Hvert bilde flates automatisk ut til 2 pannini-utsnitt (`ffmpeg`), ansiktene **sladdes** på dem
(`deface`), og **kun de sladdede flate bildene** lastes opp — råbildet lastes ikke opp (det kan
ikke sladdes pålitelig). Feiler en sladding, lastes ikke det bildet opp (personvern-sikkert).
Krever `ffmpeg` og `deface` (`pipx install deface`). Overstyr med `--proj`/`--out-fov`/`--views`/
`--rotate`/`--fov` (flatting), `--blur-thresh`/`--blur-scale`/`--no-blur` (sladding), `--keep-raw`
(last også opp råbildet), `--no-flatten`. Opplasting prøves på nytt hvert **30. sekund**, så bilder tatt uten nett lastes opp automatisk når
nettet er tilbake. En boot-tjeneste (`360logger-upload`) laster også opp ventende filer ved hver
oppstart, og `python3 -m recorder.upload_pending` gjør det manuelt.

Foto-økta **starter automatisk ved boot** (`360logger-photo.service`, installeres av bootstrap) og
kjører til den stoppes. Vil du kjøre manuelt (f.eks. for Ctrl+C i terminal), stopp tjenesten
først: `sudo systemctl stop 360logger-photo`. Merk: mens tjenesten kjører, tar hvert venstreklikk
et bilde (musa er utløseren). GPS-utløser i stedet for museklikk kommer som Steg 3.

## Gjøre om fisheye til flate bilder — `dewarp.py`  (verktøy/test)

ONE X-bildet er to fisheye-«bobler» i én JPEG. `ffmpeg` gjør det om — ingen Insta360 SDK trengs
(den er for iOS/Android/x86, ikke Pi-ens ARM):

```bash
sudo apt-get install -y ffmpeg
python3 recorder/dewarp.py <bilde>.jpg             # equirektangulær + flate utsnitt (yaw 0 og 180)
python3 recorder/dewarp.py <bilde>.jpg --fov 205   # finjuster fisheye-FOV til kameraet
python3 recorder/dewarp.py <bilde>.jpg --views 90,270 --pitch -10
python3 recorder/dewarp.py <bilde>.jpg --proj flat --out-fov 100   # rektilineær (rette linjer, men kutter)
```
Lager `<navn>_equirect.jpg` (helt panorama) og `<navn>_<proj>_yawNNN.jpg`. Standard `he`
(halv-equirect) viser **hele ~180°-hemisfæren** per linse uten å kutte; `flat` er rett men
**kutter** vide vinkler. Høyere `--flat-size` (standard 2880×2880) gir mer oppløsning.
Juster `--fov` (190–210) til skjøten ser riktig ut. Når du har valgt utsnitt/format, kobles
konverteringen inn i `photo_session` (før sladding + opplasting).

## Kjør hele opptaksøkta (video) — `record_session.py`

> Video-varianten (eldre). For gjeldende foto-retning, bruk `photo_session.py` over.

Forutsetninger: `python3-evdev` + `rclone` installert, `gdrive`-remote satt opp, og Pi-en på
kameraets WiFi. Kjør **fra repo-roten**:

```bash
python3 -m recorder.record_session
```
- Trykk på **muse-tasten** → opptak starter. Trykk igjen → opptak stopper.
- Er kameraet ikke nåbart (rødt lys) nektes start umiddelbart — du taper ikke tid på timeout.
  Manglende internett stopper *ikke* opptak (klippene lastes opp når nettet er tilbake).
- Hvert klipps to `.mp4`-filer lastes ned til `clip_<tidspunkt>/` på Pi-en og lastes opp til
  `gdrive:360-footage/clip_<tidspunkt>/` — begge filene samlet i én mappe.
- Nedlasting + opplasting skjer i bakgrunnen, så du kan starte neste klipp med en gang.
- Programmet kjører til **Ctrl+C** (venter da på at pågående opplastinger fullføres).

Valg: `--remote <navn>`, `--remote-path <mappe>`, `--staging <lokal mappe>`,
`--keep-local` (behold lokal kopi etter opplasting), `--device /dev/input/eventX`, `--key BTN_LEFT`.

## Status-LED-er

Tre LED-er viser tilstanden uten skjerm. BCM GPIO-nummerering, koblet **aktiv-høy**:
`GPIO → 330Ω → LED(+, langt ben) → LED(−) → GND`.

| Farge | GPIO | Fysisk pin | Betyr |
|-------|------|-----------|-------|
| 🔵 Blå   | 22 | 15 | Internett på (lyser = online) |
| 🟢 Grønn | 23 | 16 | Kamera klart (solid) · lavt kamerabatteri (blinker) |
| 🔴 Rød   | 24 | 18 | Kamera ikke klart (ikke nåbart) |

- Blå (internett) er uavhengig av grønn/rød (kamera): f.eks. online uten kamera → blå + rød.
  LED-ene er kun varsel — opptak vises ikke lenger på LED.
- Kamerabatteriet leses og logges jevnlig (`[status] camera battery NN%`); under 15 %
  **blinker den grønne**.
- GND finnes bl.a. på fysisk pin 14 (også 6/9/20/25/30/34/39).

Avhengigheter:
```bash
sudo apt-get install -y python3-gpiozero python3-lgpio
```

Feilsøk LED-er og klar-status uten å ta opp:
```bash
python3 -m recorder.status_leds --test    # lys hver LED etter tur (sjekk koblingen)
python3 -m recorder.status_leds           # følg klar-status + batteri live
```
Er en LED koblet «aktiv-lav», opprett den med `LED(pin, active_high=False)` i `status_leds.py`.

## GPS — `gps_logger.py`  (Steg 3, oppkobling)

GPS-modulen er en **TBS M10Q** (u-blox M10, GPS + GLONASS + Galileo + BeiDou). Den kobles
til Pi-ens UART med **3.3V-logikk** — ingen nivåomformer trengs. Ledningene krysses (modulens
Tx går til Pi-ens Rx):

| Modul | Pi (fysisk pin) | Merknad |
|-------|-----------------|---------|
| VCC   | 5V (pin 2 el. 4) | modulen har egen regulator (5V → 3.3V); **ikke** koble VCC til en GPIO-pin |
| GND   | GND (pin 6)      | felles jord |
| Tx    | RXD/GPIO15 (pin 10) | modulens Tx → Pi-ens RX |
| Rx    | TXD/GPIO14 (pin 8)  | modulens Rx → Pi-ens TX |
| SCL   | *ikke koblet*    | I2C til det innebygde kompasset (QMC5883) — ikke nødvendig for posisjon |
| SDA   | *ikke koblet*    | — |

> ⚠️ Legg aldri 5V på en GPIO-/signalpin — Pi-ens GPIO tåler bare 3.3V. VCC går **kun** til
> en 5V-pin. Signalpinnene (Tx/Rx) er 3.3V og trygge direkte.

Skru på UART-en på Pi-en (engangs; krever omstart):

```bash
sudo raspi-config nonint do_serial_hw 0       # seriell maskinvare PÅ
sudo raspi-config nonint do_serial_cons 1     # seriell innloggingskonsoll AV (den spammer ellers porten)
printf 'enable_uart=1\ndtoverlay=disable-bt\n' | sudo tee -a /boot/firmware/config.txt
sudo systemctl disable hciuart                # gir den stabile UART-en (PL011) til GPIO14/15 (Bluetooth av)
sudo usermod -aG dialout prototype1-360-kamera-gps   # les serieporten uten sudo — logg ut/inn
sudo reboot
```

`dtoverlay=disable-bt` flytter den robuste PL011-UART-en til GPIO14/15 (den er ellers opptatt av
Bluetooth). Uten den havner GPS-en på mini-UART-en, som kan miste tegn ved høy baud.

Sjekk kobling og logg:

```bash
cat /dev/serial0                        # rå NMEA — $GNGGA/$GNRMC = riktig kobling + baud
python3 -m recorder.gps_logger          # logg bredde/lengdegrad hvert sekund (/dev/serial0 @ 115200)
python3 -m recorder.gps_logger --raw    # vis også rå NMEA-linjer (feilsøk)
python3 -m recorder.gps_logger --baud 9600   # hvis 115200 gir tomt (u-blox fabrikkstandard)
```

Modulen sender som standard **115200 baud** og fler-konstellasjons-setninger med talker-ID `GN`
(`$GNGGA`, `$GNRMC`) — ikke `GP`. Loggeren filtrerer på setnings-*type* (GGA/RMC), så den fanger
begge. Hver sekund skrives en statuslinje til skjerm og en rad til
`~/360-gps-logs/gps_log_<tidspunkt>.csv`. Første fix ute kan ta 30 s–et par minutter; til da står
posisjonen tom. Krever `python3-serial` (`sudo apt-get install -y python3-serial`).

## Neste steg

- **Klynge (5 m) + utløser**: gruppere hentesteder offline (5 m single-linkage), og bytte
  musklikket i `photo_session` med en GPS-utløser (nærmeste passering + 10 m før/på/etter).
- GPS som en «myk» klar-betingelse (grace-periode) og evt. tid/kurs inn i bilde-navnet.
- Flere klar-betingelser: **SD-kort i kameraet** og lav **Pi-diskplass** (batteri er alt på plass).

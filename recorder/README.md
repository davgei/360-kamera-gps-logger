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

## Neste steg

Knapp og opptak virker hver for seg. Gjenstår å koble dem sammen (knapp → `startCapture` /
`stopCapture`), så **hente klippene** fra kameraet til Pi-en, og senere GPS + opplasting.

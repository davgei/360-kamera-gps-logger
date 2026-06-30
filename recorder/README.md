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

## Test 1 — kameratilkobling (ingen avhengigheter)

Koble Pi-en til kameraets WiFi først (kameraet blir da `192.168.42.1`), så:

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

## Neste steg

Når begge testene er grønne, kobler vi dem sammen: knapp → start/stopp opptak på kameraet via
OSC, og legger til henting av klipp + (senere) GPS og opplasting.

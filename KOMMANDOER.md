# Kommandoer

Kort jukselapp — forklaring i parentes. **Alle Pi-kommandoene kjøres på Raspberry Pi-en, ikke
på Windows.** Kjør dem fra repo-mappa:

```bash
cd ~/360-kamera-gps-logger
```

## Engangs-oppsett på Pi-en (gjør én gang)

```bash
# Avhengigheter: python3-evdev (musetasten), rclone (Google Drive), gpiozero/lgpio (LED-ene)
sudo apt-get install -y python3-evdev rclone python3-gpiozero python3-lgpio

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
```

Dukker ikke kameranettet opp i WiFi-lista? (Det sender på 5 GHz, channel 36.)

```bash
sudo raspi-config nonint do_wifi_country NO   # lås opp 5 GHz på Pi-en, prøv så igjen
```

## Oppsett-laget (deploy) — git-autopull + TeamViewer ved boot

```bash
sudo bash deploy/bootstrap.sh                 # engangs: installerer + slår på boot-tjenestene
journalctl -u 360logger-boot.service -b       # se loggen for boot-oppdateringen
```

> Merk: kjør opptak og `rclone` som **din egen bruker (uten `sudo`)**. Med `sudo` leter rclone
> etter root sin konfig og finner ikke Google Drive-en din.

# Home Assistant: Bose SoundTouch Bridge

A Home Assistant add-on repository that revives the **physical preset
buttons** on Bose SoundTouch speakers after the **Bose cloud retirement
(2026)** broke TuneIn presets, the SoundTouch app, and most cloud
sources.

The add-on listens to each speaker's local WebSocket and, when you press
a preset button, plays the URL you configured for that slot via local
UPnP — no Bose cloud needed. One instance manages every SoundTouch on
the LAN; each speaker can have its own preset map or share a default.
It also includes a Home Assistant Ingress search page for finding direct
radio stream URLs via radio-browser.info.

See [`bose_bridge/README.md`](bose_bridge/README.md) for the
configuration reference.

> Fork of [sandervg/homeassistant-bose-soundtouch-bridge](https://github.com/sandervg/homeassistant-bose-soundtouch-bridge)
> with multi-speaker support, friendly-name matching via SSDP, and a
> wildcard "master preset" entry.

## Install paths

### Home Assistant OS or Supervised (Supervisor present) — recommended

1. **Settings → Add-ons → App Store → ⋮ → Repositories**
2. Paste this repository's URL and click **Add**
3. The "Bose SoundTouch Bridge" add-on appears in the App Store —
   **Install** → **Configuration** (see `bose_bridge/README.md`) → **Start**.

MQTT credentials are auto-wired by the Supervisor when you have the
Mosquitto Broker add-on installed and the MQTT integration set up in
HA Core.

[![Open your Home Assistant instance and show the add add-on repository dialog with a specific repository URL pre-filled.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Flbochmann%2Fhomeassistant-bose-soundtouch-bridge)

### Home Assistant Container / plain Docker / NAS / Pi (no Supervisor)

Run the standalone Docker image alongside your HA instance.

```bash
curl -O https://raw.githubusercontent.com/lbochmann/homeassistant-bose-soundtouch-bridge/main/docker-compose.example.yml
mv docker-compose.example.yml docker-compose.yml
# edit SPEAKERS_JSON and MQTT host/credentials, then:
docker compose up -d
```

The image is published as `ghcr.io/lbochmann/bose-soundtouch-bridge:latest`
(multi-arch: amd64 + arm64).

Config is via env vars. `SPEAKERS_JSON` is a JSON array of speaker
entries — see `docker-compose.example.yml` for the full shape. Other
vars: `SYNC_PRESETS_ON_STARTUP`, `MQTT_HOST`, `MQTT_PORT`,
`MQTT_USERNAME`, `MQTT_PASSWORD`. `network_mode: host` is required so
the bridge can receive SSDP multicast and reach each speaker's UPnP
port.

## What works / what doesn't

| Source | Status after Bose cloud retirement | This add-on |
|---|---|---|
| Spotify Connect | ✅ still works | not needed |
| AUX in | ✅ still works | not needed |
| TuneIn presets | ❌ broken | ✅ replaced by URL push |
| SoundTouch app `LOCAL_INTERNET_RADIO` | ❌ broken | ✅ replaced |
| Plain HTTP icecast/MP3 streams | ✅ via local UPnP | ✅ used |
| Token-protected streams (some commercial radios) | ❌ | ⚠️ needs your own proxy |

## License

MIT — see [`LICENSE`](LICENSE).

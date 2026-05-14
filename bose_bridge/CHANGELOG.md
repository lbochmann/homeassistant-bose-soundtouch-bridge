# Changelog

## 1.6.1

- **UX fix:** preset URLs are now top-level fields again
  (`preset_1_url` .. `preset_6_url`), so the add-on Configuration tab
  shows the preset slots immediately — no need to add a speaker entry
  first. When set, they act as a default wildcard preset map applied to
  every discovered speaker. The `speakers:` list keeps its role for
  per-speaker overrides by `name` or `host`.
- Same shortcut for the standalone image: `PRESET_1_URL` ..
  `PRESET_6_URL` env vars re-added with the new wildcard semantics
  (alongside the per-speaker `SPEAKERS_JSON`).

## 1.6.0 — fork of [sandervg/homeassistant-bose-soundtouch-bridge](https://github.com/sandervg/homeassistant-bose-soundtouch-bridge) 1.5.0

- **Multi-speaker support.** A single bridge instance now manages every
  SoundTouch on the LAN. Define a `speakers:` list in the add-on
  Configuration tab (or `SPEAKERS_JSON` for the standalone image); each
  entry has an optional `host` and/or `name` plus `preset_1_url` …
  `preset_6_url`.
- **Match speakers by friendly name.** Instead of pinning each speaker to a
  static IP, match by the name configured on the speaker itself (e.g.
  `name: "Wohnzimmer"`). SSDP auto-discovery resolves the IP at startup,
  so speakers stay reachable even if their DHCP lease changes. `host:` is
  still honoured when you want a hard pin.
- **Master preset (wildcard) entry.** A speaker entry without `host` or
  `name` is a default that fans out to every discovered speaker no
  explicit entry claimed. So the minimal config — one entry with just
  `preset_1_url` etc. — applies the same presets to every SoundTouch on
  the LAN; add specific `name:` entries to override individual speakers
  with a different preset map.
- One thread per speaker handles the WebSocket loop; a single shared MQTT
  client dispatches HA commands to the right device by `device_id`.
  Per-speaker `button.bose_<id>_preset_N` entities appear in HA
  automatically — six per speaker.
- **Breaking:** the legacy single-speaker config shape (top-level
  `bose_host` + `preset_N_url`, and the `BOSE_HOST` / `PRESET_N_URL` env
  vars) is removed. Migrate to a `speakers:` list / `SPEAKERS_JSON`.

## 1.5.0

- **Standalone Docker image** for Home Assistant Container / plain
  Docker / NAS / Pi deployments where the Supervisor isn't available.
  Published at `ghcr.io/sandervg/bose-soundtouch-bridge:latest`
  (multi-arch: amd64 + arm64). See `docker-compose.example.yml` and the
  repo README.
- `bridge.py` now reads config from environment variables
  (`BOSE_HOST`, `PRESET_1_URL` … `PRESET_6_URL`,
  `SYNC_PRESETS_ON_STARTUP`, `MQTT_HOST`, `MQTT_PORT`, `MQTT_USERNAME`,
  `MQTT_PASSWORD`) when `/data/options.json` isn't present, so the
  same code runs inside Supervisor and standalone.
- GitHub Actions workflow builds and publishes the standalone image to
  GHCR on every version tag.

## 1.4.0

- **Auto-sync presets to the speaker on startup.** New
  `sync_presets_on_startup` option (default `true`). The add-on writes
  each configured URL onto the speaker's preset slot so physical button
  presses always emit a `nowSelectionUpdated` event for the bridge to
  intercept. Without this, factory-reset speakers leave preset slots
  empty and physical button presses become silent no-ops.
- The sync skips slots that already match the configured URL, mutes the
  speaker during the write to hide the audio blip, and verifies each
  save took effect.
- IMPORTANT firmware quirk: the SoundTouch firmware refuses to save
  preset items that carry DIDL-Lite metadata (it sets
  `isPresetable="false"`). The sync therefore writes presets without
  metadata; runtime playback still applies full DIDL via
  `SetAVTransportURI` so the speaker shows the station name and logo.

## 1.3.1

- Stop the speaker before each SetAVTransportURI so the DIDL-Lite
  metadata (station name + favicon) lands cleanly in `now_playing` even
  when the press came from a physical preset button that started
  loading a stale on-device source first (TuneIn / cached UPnP item).

## 1.3.0

- **Speaker now displays the station name and logo.** Each `Play` call
  carries DIDL-Lite metadata (`dc:title`, `upnp:albumArtURI`,
  `audioBroadcast` class). Station name + favicon are auto-fetched from
  [radio-browser.info](https://www.radio-browser.info/) by stream URL
  at startup and cached for the session.
- **Trigger presets from Home Assistant.** The add-on connects to the
  Supervisor-provided MQTT broker (Mosquitto add-on) and publishes Home
  Assistant MQTT-discovery configs so each preset auto-appears as a
  `button.bose_<id>_preset_N` entity. Press the entity in HA → bridge
  plays the same URL it would play on a physical button press.
  Requires the Mosquitto Broker add-on running and the MQTT integration
  configured in HA (the standard auto-discovery setup).
- The add-on declares `services: ["mqtt:need"]` so the Supervisor
  injects MQTT credentials automatically — no manual configuration.
  Falls back gracefully if MQTT is unavailable (logs a warning, only
  physical buttons keep working).

## 1.2.1

- Fix multi-architecture build. The `1.2.0` Dockerfile only pulled the
  amd64 base image and failed on aarch64 (ARM64) Home Assistant
  installations. Re-added `build.yaml` mapping each supported
  architecture to its correct base image.
- Dropped deprecated `armv7`, `armhf`, `i386` from `arch` (modern
  Supervisor flags these). Supported architectures are now `amd64` and
  `aarch64`.

## 1.2.0

- Polished release for public use.
- Auto-discovers the SoundTouch via SSDP if `bose_host` is left blank.
- Auto-derives the UPnP description URL from the speaker's `/info`
  endpoint — works on any SoundTouch model out of the box.
- Removed deprecated `build.yaml` (FROM image inlined into Dockerfile).
- Default config is now empty so first-time users can paste their own
  URLs.

## 1.1.0

- Added 6 configurable preset URL fields and a `bose_host` field via the
  add-on **Configuration** tab.

## 1.0.0

- Initial WebSocket → UPnP bridge with hardcoded URL map.

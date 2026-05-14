# Bose SoundTouch Bridge

Revives the **physical preset buttons** on Bose SoundTouch speakers after
the **Bose cloud retirement (2026)**. One add-on instance manages every
SoundTouch on the LAN — each speaker can have its own preset map, or
share a default.

## What this fixes

When the Bose cloud was retired, every preset that relied on it stopped
working — TuneIn presets, the SoundTouch app, and the
`LOCAL_INTERNET_RADIO` source all return errors. Spotify and AUX still
work, but the six physical buttons on top of the speaker are mostly dead.

This add-on revives them. It listens to each speaker's local WebSocket
notification stream and, whenever you press a preset button, pushes the
URL you configured for that slot via UPnP — using the local
`SetAVTransportURI` / `Play` calls that are still fully functional in
the firmware.

## Requirements

- One or more Bose SoundTouch speakers (any model with the SoundTouch
  firmware) on the same network as Home Assistant.
- Home Assistant OS or Supervised (the add-on runs as a Docker container
  managed by the Supervisor). For Home Assistant Container / plain
  Docker, use the standalone image — see the top-level README.

## Install

1. In Home Assistant: **Settings → Add-ons → App Store → ⋮ → Repositories**
2. Add this repository's GitHub URL.
3. The "Bose SoundTouch Bridge" add-on appears in the store — click
   **Install** → **Configuration** (see below) → **Start**.

## Configuration

**Easy mode — same presets on every speaker.** Fill in the six top-level
`preset_N_url` fields and leave `speakers:` empty. The bridge
auto-discovers every SoundTouch on the LAN via SSDP and applies these
presets to all of them:

```yaml
preset_1_url: "http://icecast.vrtcdn.be/radio1-high.mp3"
preset_2_url: "http://icecast.vrtcdn.be/stubru-high.mp3"
speakers: []
```

**Per-speaker overrides.** When you want a specific speaker to play
something different, add an entry to `speakers:` matched by `name:` (the
friendly name set on the speaker itself, e.g. "Wohnzimmer" — resolved
via SSDP at startup so it survives DHCP changes) or by `host:` (a fixed
IP). Speakers without an explicit entry continue to use the top-level
preset map:

```yaml
preset_1_url: "http://icecast.vrtcdn.be/radio1-high.mp3"   # default for every speaker
preset_2_url: "http://icecast.vrtcdn.be/stubru-high.mp3"
speakers:
  - name: "Bad"                                            # override: Bad plays Radio 2 OVL
    preset_1_url: "http://icecast.vrtcdn.be/ra2ovl-high.mp3"
```

Leave `sync_presets_on_startup` enabled (default). On startup the
add-on writes each configured URL into the speaker's matching preset
slot — required so physical button presses emit the WebSocket event the
bridge listens for. Skip-when-equal makes restarts cheap.

After **Save → Start**, check the **Log** tab. You should see one
`[upnp] / [sync] / [ws]` block per managed speaker. Press a preset
button on any of them and the configured stream should play.

For HA control: with the Mosquitto Broker add-on running and the MQTT
integration configured in HA Core, six `button.bose_<id>_preset_N`
entities per speaker auto-appear via MQTT discovery. Pressing one in
the HA UI / automations / scripts plays the same URL the physical
button would.

## Example URLs (Belgian / Flemish radio)

| Preset | Station | URL |
|---|---|---|
| 1 | VRT Radio 1 | `http://icecast.vrtcdn.be/radio1-high.mp3` |
| 2 | VRT Radio 2 OVL | `http://icecast.vrtcdn.be/ra2ovl-high.mp3` |
| 3 | VRT Radio 1 Classics | `http://icecast.vrtcdn.be/radio1_classics-high.mp3` |
| 4 | VRT Studio Brussel | `http://icecast.vrtcdn.be/stubru-high.mp3` |
| 6 | VRT Nieuwsbrief | `http://progressive-audio.vrtcdn.be/content/fixed/11_11niws-snip_hi.mp3` |

For other stations, look up the direct stream URL on the broadcaster's
website (search for `icecast`, `mp3`, or `aac`). Some commercial stations
hide their URL behind authenticated tokens — those won't work without an
extra proxy and are out of scope for this add-on.

## How it works

- Bose's stock firmware exposes a WebSocket notification stream on
  `ws://<speaker>:8080` (subprotocol `gabbo`). It emits
  `<nowSelectionUpdated><preset id="N">…` for every preset button press.
- The same firmware exposes a UPnP `MediaRenderer` on port 8091 with a
  fully working `AVTransport` service (the very same one the SoundTouch
  app uses for "play this URL").
- The add-on stitches them together: catch the button event, push the
  URL via UPnP. One thread per speaker, one shared MQTT client for HA
  dispatch. No cloud needed.

## Limitations

- Only **plain HTTP audio streams** (no token-protected commercial
  streams without an extra proxy).
- The speaker's display still shows whatever the original preset is set
  to — buttons trigger the bridge regardless. The sync step writes the
  configured URL onto the slot, so the display name matches in most cases.

## License

MIT

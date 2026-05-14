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

## Configuration

### Easy mode — same presets on every speaker

Fill in the six top-level `preset_N_url` fields and leave `speakers:`
empty. The bridge discovers every SoundTouch on the LAN via SSDP at
startup and applies these presets to all of them — no IPs, no
hostnames, no manual list of devices. New speakers added to the
network later are picked up on the next restart.

```yaml
preset_1_url: "http://icecast.vrtcdn.be/radio1-high.mp3"
preset_2_url: "http://icecast.vrtcdn.be/stubru-high.mp3"
speakers: []
```

After **Save → Start**, the **Log** tab should show one
`[upnp] / [sync] / [ws]` block per discovered speaker. Press a preset
button on any of them and the configured stream should play.

### Per-speaker overrides

When you want a specific speaker to play something different (e.g.
Bathroom on news, Living Room on rock), add an entry to `speakers:`
matched by the friendly name configured on the speaker itself
(*Settings → Speaker name* in the SoundTouch app, e.g. "Wohnzimmer" or
"SoundTouch 10 Bad"). The name is resolved via SSDP at startup, so it
keeps working when the speaker gets a new DHCP lease — no fixed IP
needed.

**You don't have to type the names from scratch.** Start the add-on
once with `speakers: []` and check the **Log** tab. The bridge prints
a copy-paste-ready YAML block listing every discovered speaker by name:

```
[cfg] to override presets for a specific speaker, copy one of these names into `speakers:`

[cfg]     - name: "SoundTouch 10 Wohnzimmer"
[cfg]       preset_1_url: "http://your-stream.example/stream.mp3"
[cfg]     - name: "SoundTouch 10 Bad"
[cfg]       preset_1_url: "http://your-stream.example/stream.mp3"
```

Copy the speaker name into your config and set the URLs you want.
Speakers without an explicit entry keep using the top-level preset map:

```yaml
preset_1_url: "http://icecast.vrtcdn.be/radio1-high.mp3"   # default for every speaker
preset_2_url: "http://icecast.vrtcdn.be/stubru-high.mp3"
speakers:
  - name: "SoundTouch 10 Bad"                              # override: Bad plays Radio 2 OVL
    preset_1_url: "http://icecast.vrtcdn.be/ra2ovl-high.mp3"
```

If you'd rather pin a speaker to an IP (e.g. because you've set a DHCP
reservation), use `host:` instead of `name:`. You can mix both styles
across entries.

> Why isn't the speaker list auto-filled into the Configuration form?
> Home Assistant renders the form from a static schema *before* the
> add-on starts, so the add-on has no way to push live discovery
> results back into the UI. The log hint is the closest equivalent.

### Preset sync on startup

`sync_presets_on_startup` (default `true`) writes each configured URL
into the speaker's matching preset slot at startup. This is what
makes physical button presses emit the WebSocket event the bridge
listens for — without sync, factory-reset speakers stay silent on
button presses. Sync skips slots already in the right state, so
restarts are cheap.

### Home Assistant control buttons

With the Mosquitto Broker add-on running and the MQTT integration
configured in HA Core, the bridge publishes MQTT-discovery configs so
six `button.bose_<id>_preset_N` entities per speaker auto-appear in
HA. Pressing one in the HA UI / automations / scripts plays the same
URL the physical button would. Falls back gracefully if MQTT is
unavailable — only physical buttons keep working.

## Example URLs (Belgian / Flemish radio)

| Preset | Station | URL |
|---|---|---|
| 1 | VRT Radio 1 | `http://icecast.vrtcdn.be/radio1-high.mp3` |
| 2 | VRT Radio 2 OVL | `http://icecast.vrtcdn.be/ra2ovl-high.mp3` |
| 3 | VRT Radio 1 Classics | `http://icecast.vrtcdn.be/radio1_classics-high.mp3` |
| 4 | VRT Studio Brussel | `http://icecast.vrtcdn.be/stubru-high.mp3` |
| 6 | VRT Nieuwsbrief | `http://progressive-audio.vrtcdn.be/content/fixed/11_11niws-snip_hi.mp3` |

For other stations, look up the direct stream URL on the broadcaster's
website (search for `icecast`, `mp3`, or `aac`). See *Limitations*
below for HTTPS and token-protected streams.

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

### Plain `http://` streams only — no `https://`

The bridge hands the URL to the speaker via UPnP; the actual fetch is
done by the SoundTouch firmware. That firmware predates modern TLS and
has no working HTTPS client for stream playback. Practical implications:

- `https://...` URLs will hang or fail silently on the speaker even
  though the bridge logs them as "playing".
- Many station websites today only advertise HTTPS URLs (Rockantenne,
  several SWR/WDR substations, …). Look around — most still publish a
  plain HTTP equivalent for legacy media renderers. Search the
  station's site for *"Streaming-URL"*, *"icecast"*, or open the m3u
  playlist and read the underlying URL.
- If a station genuinely doesn't offer HTTP, you need a reverse proxy
  in your network that fronts the HTTPS source over plain HTTP (e.g.
  nginx with `proxy_pass`, or any Icecast relay). Out of scope for
  this add-on for now — a built-in HTTPS→HTTP proxy is on the roadmap.

### Token-protected commercial streams

Some commercial radios (BigFM, several Antenne-* variants) hide the
real stream URL behind an authenticated handshake. The SoundTouch
firmware has no way to perform that handshake, and the bridge isn't
trying to either. Same fix as for HTTPS: a separate proxy that
performs the auth and republishes the resulting stream as plain HTTP.

### Display name on the speaker

The speaker's display still shows whatever the original preset is set
to — buttons trigger the bridge regardless. The sync step writes the
configured URL onto the slot, so the display name matches in most cases.

## License

MIT

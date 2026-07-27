# piSignage for Home Assistant

Control [piSignage](https://pisignage.com) digital signage screens from Home Assistant.

Every player on your account becomes a Home Assistant device with a playlist picker,
so you can see what a screen is showing and change it from a dashboard, a script, or
an automation.

> Supports the **hosted** service only — `https://<account>.pisignage.com`.
> Self-hosted piSignage servers and the on-device Pi player API are not supported.
> See [Known limitations](#known-limitations).

## Entities

One device is created per player. Each carries:

| Entity | Type | What it does |
|---|---|---|
| `select.<player>_playlist` | Select | The playlist this screen is playing. Read it, or set it to switch. |
| `sensor.<player>_current_playlist` | Sensor | The playing playlist as a plain string, handy in templates. |
| `binary_sensor.<player>_online` | Binary sensor | `on` when piSignage reports the player connected. Same state the piSignage dashboard shows. |
| `sensor.<player>_last_seen` | Sensor | Timestamp of the last check-in. Disabled by default. |

A screen named *Lobby Screen* gives you `select.lobby_screen_playlist`, and so on.

## Installation

### HACS

1. HACS → ⋮ → **Custom repositories**.
2. Add this repository's URL with category **Integration**.
3. Find **piSignage** in HACS and install it.
4. Restart Home Assistant.

### Manual

Copy `custom_components/pisignage` into your Home Assistant `config/custom_components/`
directory, so you end up with `config/custom_components/pisignage/manifest.json`, then
restart Home Assistant.

## Configuration

**Settings → Devices & Services → Add Integration → piSignage.**

| Field | What to enter |
|---|---|
| Account | Your piSignage subdomain. If you sign in at `https://myco.pisignage.com`, enter `myco`. A full URL works too. |
| Username or email | The same one you sign in to piSignage with. |
| Password | That account's password. |

The credentials are checked against the live API before the entry is created, so a typo
is reported immediately rather than showing up as a broken integration later.

### Options

**Configure** on the integration lets you change the update interval. The default is
60 seconds, and 30 seconds is the minimum — the hosted piSignage service is shared
infrastructure and its own documentation asks clients not to poll faster than that.

## How switching playlists works

This is the part worth understanding before you wire it into automations.

In piSignage, playlists are not attached to individual screens. They are attached to
**groups**, and each player belongs to a group:

```
Asset ──▶ Playlist ──assigned to──▶ Group ──contains──▶ Player
```

A group plays its **whole eligible set** on rotation, and `POST /setplaylist` only plays
something *once* before that rotation resumes. So there is no per-screen "play only
this" concept in piSignage at all.

To make an assignment persistent — the screen shows this playlist and keeps showing it —
the integration sets the group's playlist list to **exactly** the playlist you chose,
then deploys.

> [!WARNING]
> Selecting a playlist **removes the group's other playlists**, including any schedules
> configured on them in piSignage. If a group is set up to rotate between several
> playlists, or to show one only at certain times, choosing a playlist here discards
> that arrangement.
>
> The integration logs a warning naming exactly what it removed, so check the Home
> Assistant log if a group changes unexpectedly. Re-add them in piSignage to restore.
>
> **Give each screen its own group** if you want them independently controllable —
> selecting a playlist affects every player in the group.

Every selection deploys, including re-selecting the playlist that is already assigned.
That makes the selector a way to **force a screen back into line** if it has drifted,
rather than a no-op that leaves a wrong-looking screen wrong. It also means each
selection causes that group to re-sync, so drive it from events rather than a fast
automation loop.

The selector shows your choice immediately, while the player is still syncing. It falls
back to the polled value once piSignage confirms the change, so if a deploy silently
fails the entity will revert rather than lie to you.

## Examples

Switch the lobby screen to the promo playlist when the shop opens:

```yaml
automation:
  - alias: "Lobby screen: opening content"
    triggers:
      - trigger: time
        at: "08:30:00"
    actions:
      - action: select.select_option
        target:
          entity_id: select.lobby_screen_playlist
        data:
          option: "Promos"
```

Show a closing playlist, but only if the screen is actually online:

```yaml
automation:
  - alias: "Lobby screen: closing content"
    triggers:
      - trigger: time
        at: "17:45:00"
    conditions:
      - condition: state
        entity_id: binary_sensor.lobby_screen_online
        state: "on"
    actions:
      - action: select.select_option
        target:
          entity_id: select.lobby_screen_playlist
        data:
          option: "Closing"
```

Notify when a screen drops offline during opening hours:

```yaml
automation:
  - alias: "Alert on signage screen offline"
    triggers:
      - trigger: state
        entity_id: binary_sensor.lobby_screen_online
        to: "off"
        for: "00:10:00"
    conditions:
      - condition: time
        after: "08:00:00"
        before: "18:00:00"
    actions:
      - action: notify.mobile_app
        data:
          message: >-
            Lobby screen has been offline for 10 minutes.
            Last playlist: {{ states('sensor.lobby_screen_current_playlist') }}
```

## Troubleshooting

**A screen shows as offline but looks fine.**
The state comes from piSignage's own `isConnected` flag — the same one its dashboard
shows — which tracks the player's live connection to the server. A screen can be
powered on and playing perfectly while that connection is briefly dropped, so short
flaps are normal. Enable `sensor.<player>_last_seen` (disabled by default) to see when
the player actually last checked in; if that timestamp is recent, the screen is fine.

If your player is old enough not to report `isConnected`, the integration falls back to
treating a check-in older than 5 minutes as offline.

When automating on this, put a `for:` duration on the trigger so a momentary drop does
not page you — the example above uses 10 minutes.

**I picked a playlist and the screen did not change.**
Check the Home Assistant log. If the playlist had to be deployed, the player may still
be downloading it — large videos take a while. The screen should switch once the sync
completes, and the entity will catch up on the next poll.

**Another screen changed when I only touched one.**
Both screens are in the same piSignage group, and assignment is per group. Put each
screen in its own group. See [How switching playlists works](#how-switching-playlists-works).

**A group lost playlists I had scheduled.**
Selecting a playlist makes it that group's only playlist, which is what makes the
assignment stick. The Home Assistant log records exactly what was removed — search it
for `removing`. Re-add them in piSignage.

**"Re-authentication required".**
The stored password stopped working. Home Assistant will prompt you to re-enter it;
the rest of the configuration is kept.

**State takes up to a minute to update.**
That is the poll interval. You can lower it to 30 seconds in the integration options,
but not below.

## Known limitations

- **Hosted piSignage only.** Self-hosted open-source servers use HTTP Basic auth rather
  than the JWT flow, and are not supported. Neither is talking to a Raspberry Pi player
  directly on port 8000.
- **Playlists are read-only.** You can switch between playlists, but not create, edit or
  delete them, and not upload assets.
- **No playback transport controls.** Pause, next and previous are not exposed.
- **No scheduling control.** piSignage's own per-group schedule still applies, and may
  switch a screen away from the playlist you pinned.
- **Group-level side effects.** As described above, deploying a new playlist affects
  every player in the group. This is how piSignage works, not something the integration
  can avoid.

## Removing the integration

Settings → Devices & Services → piSignage → ⋮ → **Delete**. Nothing is changed on the
piSignage account itself — playlists, groups and players are left exactly as they are.

## Development

```bash
python3.13 -m venv .venv
.venv/bin/pip install -r requirements-test.txt

.venv/bin/python -m pytest tests -q --cov=custom_components.pisignage --cov-report=term-missing
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
```

All HTTP lives in `custom_components/pisignage/api.py`; everything above it works with
plain dictionaries. The client is vendored rather than published as a PyPI package so
the integration installs straight from HACS — this is recorded as an explicit exemption
in `quality_scale.yaml`.

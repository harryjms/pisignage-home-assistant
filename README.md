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
| `switch.<player>_tv` | Switch | Turns the attached TV on and off — over HDMI-CEC, or through a `media_player` you map to that screen. See [TV power](#tv-power). |

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
the integration sets the group's playlist list to **exactly** the playlist you chose and
deploys, assembling the deploy the same way the piSignage console's **Deploy** button
does:

- the group's `assets` list is rebuilt from the playlist — its media files, the files of
  any playlist embedded in a zone, a custom template, and the group logo;
- crucially, `__<playlist>.json`, the playlist's own **descriptor**, is included. Without
  it the player downloads the media and has nothing telling it what to play, so the
  screen keeps showing the old playlist even though the deploy succeeded;
- the entry gets its `plType` (`regular`, `advt`, `domination`, `event`, `keyPress`,
  `audio`, or `special` for `TV_OFF`) and `skipForSchedule`, plus the playlist's own
  settings. A player ignores an entry with no `plType`;
- the whole group object is posted back with `deploy: true`.

A partial deploy is accepted by the server and then quietly ignored by the player, which
is why anything less than the full set above leaves the screen unchanged.

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

The screen picks the change up on its next check-in, usually within a minute. Larger
playlists take longer, because the player downloads the content first.

## TV power

Each screen can get a `switch.<player>_tv` that turns its TV on and off. There are two
ways it can drive the TV, and a screen gets a switch if **either** is available.

### Over HDMI-CEC

piSignage can switch the TV through the player itself. This is used automatically for
players reporting `isCecSupported`, with state from the player's own `tvStatus` — so the
switch also follows the TV being switched off by piSignage's own schedule.

CEC support is re-read on every poll rather than fixed at startup, because a player probes
its TV after booting and can report CEC a little later. The switch appears on its own when
it does.

> [!NOTE]
> Switching the TV **off** over CEC works by putting the player on piSignage's special
> `TV_OFF` playlist, not by stopping playback. Switching it back **on** restores power but
> leaves the player on `TV_OFF`, so the screen comes back lit but without content. Select
> the screen's playlist again to bring it back.

### Through a `media_player` entity

Plenty of screens have no working CEC but hang off a TV that Home Assistant already
controls another way. **Configure** on the integration lists every screen and lets you
pick a `media_player` entity for it. When one is set, that screen's TV switch calls
`media_player.turn_on` / `turn_off` on your entity instead of talking to piSignage, and
takes its state from that entity — which is the truth, since piSignage knows nothing about
a TV it is not driving. Clearing the field puts the screen back on CEC.

A mapped entity wins over CEC, and a screen with neither gets no switch at all: piSignage
reports success for the CEC command whether or not the TV can act on it, so an
always-succeeding switch that changes nothing would be worse than none.

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
Give it a minute — the player only switches on its next check-in, and it downloads the
content first, which takes longer for large videos. If it still has not changed, check
the Home Assistant log for an error from the deploy. A screen that downloads the new
content but keeps showing the old playlist was a bug in versions before 1.3.3; update if
you are on an older one.

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

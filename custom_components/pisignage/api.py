"""Async client for the hosted piSignage REST API.

Only the hosted surface (``https://<account>.pisignage.com/api``) is supported.
Authentication is a JWT obtained from ``POST /session`` and replayed on every
later call in the ``x-access-token`` header.

Most endpoints answer with an envelope::

    {"success": true, "stat_message": "...", "data": {...}}

``success`` can be ``false`` on an HTTP 200, so responses are judged on the body
rather than the status code. But the envelope is not universal — ``POST
/session`` returns a bare ``{token, userInfo}`` with no ``success`` field, and
failures return ``{"message": ..., "error": {}}``. All three shapes are handled.

Other live-API quirks worth knowing, each verified against a real account:

* Collection paging is **zero-indexed**; ``?page=1`` on a one-page collection
  returns nothing.
* ``pages`` and ``count`` describe the page just returned, not the collection,
  so a short batch is the only reliable end signal.
* ``data`` is sometimes a bare list (``/playlists``, ``/groups``) and sometimes
  an object with ``objects`` (``/players``).
* Timestamps are not consistent even within one object: ``lastReported`` is an
  ISO 8601 string while ``lastUpload`` is epoch milliseconds.
* The JWT is short-lived (about four hours), so re-auth on 401 is a normal
  part of operation rather than an error path.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from http import HTTPStatus
import logging
from typing import Any
from urllib.parse import quote

import aiohttp

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)

#: Pages to walk before giving up, so a misbehaving server cannot loop forever.
MAX_PAGES = 50
PER_PAGE = 100

#: A player is considered offline once its last check-in is older than this.
OFFLINE_AFTER_SECONDS = 300


class PiSignageError(Exception):
    """Base class for every error raised by this client."""


class PiSignageConnectionError(PiSignageError):
    """The server could not be reached, or did not answer in time."""


class PiSignageAuthError(PiSignageError):
    """The credentials or token were rejected."""


def build_base_url(account: str) -> str:
    """Turn user input into a usable API base URL.

    Accepts a bare account name (``myco``), a hostname
    (``myco.pisignage.com``), or a full URL with or without the ``/api``
    suffix.
    """
    value = account.strip().rstrip("/")
    if not value:
        raise ValueError("Account must not be empty")

    if "://" not in value:
        # A bare word is an account name on the hosted service; anything with a
        # dot is already a hostname.
        value = f"https://{value}" if "." in value else f"https://{value}.pisignage.com"

    if not value.endswith("/api"):
        value = f"{value}/api"
    return value


def normalise_epoch(value: Any) -> float | None:
    """Return *value* as epoch seconds.

    piSignage is wildly inconsistent about time. Live hosted accounts return
    ``lastReported`` as an ISO 8601 string, while ``lastUpload`` on the same
    object is epoch milliseconds and the API docs describe seconds. All three
    are accepted here.
    """
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            # fromisoformat handles the trailing Z from Python 3.11 onwards.
            parsed = datetime.fromisoformat(text)
        except ValueError:
            # Fall through: it might still be a number wearing a string.
            pass
        else:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed.timestamp()

    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    if number > 1e11:  # year 5138 in seconds — must be milliseconds
        number /= 1000
    return number


def extract_list(data: Any) -> list[Any]:
    """Pull the list out of a response payload.

    The API is not consistent about where collections live: players and groups
    arrive under ``objects``, assets under ``dbdata``, and playlists have been
    seen both as a bare list and under ``posts``.
    """
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("objects", "posts", "dbdata", "playlists", "files"):
            value = data.get(key)
            if isinstance(value, list):
                return value
    return []


#: Per-playlist settings the console copies from the playlist onto the group's
#: entry for it before deploying.
PLAYLIST_SETTING_KEYS = (
    "ads",
    "domination",
    "event",
    "keyPress",
    "onlineOnly",
    "audio",
)

#: Zone columns an asset row can carry alongside its main file.
ZONE_KEYS = ("side", "bottom", "zone4", "zone5", "zone6")


def _playlist_type(playlist: dict[str, Any], has_assets: bool) -> str:
    """Classify a playlist the way the console does before deploying.

    The player uses ``plType`` to decide how to treat each entry, so an entry
    deployed without one is not played.
    """
    if playlist.get("name") == "TV_OFF":
        return "special"
    if not has_assets:
        return "no assets"

    settings = playlist.get("settings") or {}

    def enabled(key: str, flag: str) -> bool:
        value = settings.get(key)
        return bool(isinstance(value, dict) and value.get(flag))

    if enabled("ads", "adPlaylist"):
        return "advt"
    if enabled("domination", "enable"):
        return "domination"
    if enabled("event", "enable"):
        return "event"
    if enabled("keyPress", "enable"):
        return "keyPress"
    if enabled("audio", "enable"):
        return "audio"
    return "regular"


def build_deploy_payload(
    group: dict[str, Any],
    playlist: str,
    details: dict[str, Any],
    resolve: Callable[[str], dict[str, Any] | None] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Build the group entry and asset list for deploying *playlist* alone.

    This mirrors what the piSignage console assembles before it deploys, which
    is the only combination the player actually acts on:

    * every media file the playlist uses, plus the files of any playlist
      embedded in one of its zones,
    * ``__<playlist>.json`` — the playlist's own descriptor. **Without this the
      player downloads the media and then has nothing telling it what to play,
      so the screen keeps showing whatever it showed before.**
    * the custom template, if the playlist uses one, and the group's logo,
    * ``plType`` and ``skipForSchedule`` on the entry, plus the playlist's own
      ads/domination/event/keyPress/onlineOnly/audio settings.

    *resolve* looks up another playlist by name, for zone-embedded playlists.
    """
    assets: list[str] = []

    def add(name: Any) -> None:
        if (
            isinstance(name, str)
            and name
            and not name.startswith("_system")
            and name not in assets
        ):
            assets.append(name)

    rows = [row for row in (details.get("assets") or []) if isinstance(row, dict)]
    for row in rows:
        add(row.get("filename"))
        for zone in ZONE_KEYS:
            value = row.get(zone)
            if not isinstance(value, str) or not value:
                continue
            add(value)
            # A zone can hold a whole playlist, referenced as __name.json; its
            # files have to travel too or the zone renders empty.
            if value.startswith("__") and ".json" in value and resolve is not None:
                nested = resolve(value[2 : value.index(".json")])
                for nested_row in (nested or {}).get("assets") or []:
                    if isinstance(nested_row, dict):
                        add(nested_row.get("filename"))

    # The descriptor is what turns downloaded files into a playable playlist.
    add(f"__{playlist}.json")
    add(details.get("templateName"))
    add(group.get("logo"))

    settings = details.get("settings") or {}
    entry: dict[str, Any] = {
        "name": playlist,
        "skipForSchedule": not rows and playlist != "TV_OFF",
        "plType": _playlist_type({**details, "name": playlist}, bool(rows)),
        "settings": {key: settings.get(key) for key in PLAYLIST_SETTING_KEYS},
    }
    return entry, assets


def _stringify_params(params: dict[str, Any] | None) -> dict[str, str] | None:
    """Aiohttp only accepts primitive query values, so coerce everything."""
    if not params:
        return None
    return {
        key: ("true" if value is True else "false" if value is False else str(value))
        for key, value in params.items()
        if value is not None
    }


class PiSignageClient:
    """Thin async wrapper over the hosted piSignage API."""

    def __init__(
        self,
        account: str,
        username: str,
        password: str,
        session: aiohttp.ClientSession,
        token: str | None = None,
    ) -> None:
        """Initialise the client. *session* is owned by the caller."""
        self.base_url = build_base_url(account)
        self._username = username
        self._password = password
        self._session = session
        self._token = token
        self._login_lock = asyncio.Lock()

    @property
    def token(self) -> str | None:
        """The cached JWT, if the client has logged in."""
        return self._token

    # ------------------------------------------------------------------
    # transport
    # ------------------------------------------------------------------

    async def _raw_request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        authenticated: bool = True,
    ) -> Any:
        """Perform one request and unwrap the response envelope."""
        url = f"{self.base_url}/{path}"
        kwargs: dict[str, Any] = {"timeout": REQUEST_TIMEOUT}

        if (query := _stringify_params(params)) is not None:
            kwargs["params"] = query
        if json is not None:
            kwargs["json"] = json
        if authenticated:
            if self._token is None:
                await self.async_login()
            kwargs["headers"] = {"x-access-token": self._token or ""}

        try:
            response = await self._session.request(method, url, **kwargs)
            status = response.status
            payload = await response.json(content_type=None)
        except TimeoutError as err:
            raise PiSignageConnectionError(f"Timed out calling {path}") from err
        except aiohttp.ClientError as err:
            raise PiSignageConnectionError(f"Cannot reach {url}: {err}") from err
        except ValueError as err:
            raise PiSignageError(f"{path} returned a non-JSON response") from err

        if not isinstance(payload, dict):
            raise PiSignageError(f"{path} returned an unexpected payload")

        # Failures carry their reason in `message`; successful envelope
        # responses use `stat_message`.
        message = (
            payload.get("message")
            or payload.get("stat_message")
            or f"{path} failed with HTTP {status}"
        )

        if status in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
            raise PiSignageAuthError(message)
        if status >= HTTPStatus.BAD_REQUEST:
            raise PiSignageError(message)

        if "success" in payload:
            if not payload["success"]:
                # Application failures come back with HTTP 200, so the body is
                # the only reliable signal.
                raise PiSignageError(message)
            return payload.get("data")

        # Not every endpoint uses the envelope — POST /session answers with a
        # bare {token, userInfo} object and no success flag at all. Treating a
        # missing flag as failure made every login fail.
        return payload

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        """Perform a request, re-authenticating once if the token expired."""
        if self._token is None:
            await self.async_login()

        stale_token = self._token
        try:
            return await self._raw_request(method, path, **kwargs)
        except PiSignageAuthError:
            await self._async_reauth(stale_token)
            # A second failure is a real credential problem, so let it bubble.
            return await self._raw_request(method, path, **kwargs)

    async def _async_reauth(self, stale_token: str | None) -> None:
        """Log in again unless a concurrent caller already did."""
        async with self._login_lock:
            if self._token != stale_token:
                return
            await self._async_login_locked()

    async def async_login(self) -> str:
        """Obtain and cache a JWT."""
        async with self._login_lock:
            return await self._async_login_locked()

    async def _async_login_locked(self) -> str:
        data = await self._raw_request(
            "POST",
            "session",
            json={
                # `email` accepts an email address or a username.
                "email": self._username,
                "password": self._password,
                "getToken": True,
            },
            authenticated=False,
        )
        token = data.get("token") if isinstance(data, dict) else None
        if not token:
            raise PiSignageAuthError("Login succeeded but no token was returned")
        self._token = str(token)
        return self._token

    async def _async_paginated(
        self, path: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Walk a paginated collection endpoint.

        Paging is **zero-indexed**: ``?page=1`` on a single-page collection
        returns nothing at all. Asking for page 1 first is why this used to
        report an empty fleet on accounts that clearly had players.

        The ``pages`` and ``count`` fields describe the page just returned
        rather than the collection, so they cannot be used to decide when to
        stop. A short batch is the reliable end-of-collection signal.
        """
        results: list[dict[str, Any]] = []
        for page in range(MAX_PAGES):
            query = dict(params or {})
            query.update({"page": page, "per_page": PER_PAGE})
            data = await self._request("GET", path, params=query)

            batch = [item for item in extract_list(data) if isinstance(item, dict)]
            results.extend(batch)

            if len(batch) < PER_PAGE:
                break
        return results

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------

    async def async_get_players(self) -> list[dict[str, Any]]:
        """Every player on the account."""
        return await self._async_paginated("players")

    async def async_get_groups(self) -> list[dict[str, Any]]:
        """Every group, including the per-player pseudo-groups.

        ``all`` matters here: a player with no explicit group still has a
        private group referenced by ``selfGroupId``, and playlists have to be
        deployed to that group like any other.
        """
        return await self._async_paginated("groups", {"all": True})

    async def async_get_group(self, group_id: str) -> dict[str, Any]:
        """One group by id.

        Used as a fallback when a player's group is missing from the bulk
        listing, which happens for some per-player pseudo-groups.
        """
        data = await self._request("GET", f"groups/{quote(str(group_id))}")
        if isinstance(data, dict):
            # Some builds nest the group under a key rather than returning it flat.
            for key in ("group", "object"):
                nested = data.get(key)
                if isinstance(nested, dict):
                    return nested
            return data
        raise PiSignageError(f"Group {group_id} returned an unexpected payload")

    async def async_get_playlist_names(self) -> list[str]:
        """Every playlist name on the account, case-insensitively sorted."""
        data = await self._request("GET", "playlists")
        names: list[str] = []
        for item in extract_list(data):
            if isinstance(item, str):
                names.append(item)
            elif isinstance(item, dict):
                name = item.get("name")
                if isinstance(name, str) and name:
                    names.append(name)
        return sorted(set(names), key=str.casefold)

    # ------------------------------------------------------------------
    # writes
    # ------------------------------------------------------------------

    async def async_set_tv_power(self, player_id: str, on: bool) -> None:
        """Switch the TV attached to *player_id* on or off over HDMI-CEC.

        ``POST /pitv`` takes an **inverted** flag: ``status: true`` switches the
        TV *off*. Only players reporting ``isCecSupported`` can be expected to
        obey it — on the rest the server still reports the command as issued.

        piSignage implements "off" by putting the player on the special
        ``TV_OFF`` playlist rather than by cutting playback, so switching the TV
        back on restores power but leaves the screen on that playlist until a
        playlist is deployed to it again.
        """
        await self._request("POST", f"pitv/{quote(player_id)}", json={"status": not on})

    async def async_get_playlist(self, playlist: str) -> dict[str, Any]:
        """Fetch one playlist in full, including its asset rows and settings."""
        data = await self._request("GET", f"playlists/{quote(playlist, safe='')}")
        if isinstance(data, dict):
            return data
        raise PiSignageError(f"Playlist {playlist} returned an unexpected payload")

    async def async_assign_playlist(
        self, group: dict[str, Any], playlist: str
    ) -> list[str]:
        """Make *playlist* the group's only playlist and deploy it.

        piSignage has no per-screen playlist assignment: playlists belong to a
        group, so making a screen show one playlist means making it that group's
        entire set.

        The deploy is assembled the way the console's own Deploy button
        assembles it — the group is re-read, the playlist's descriptor and files
        are collected into the group's asset list, the entry is given its
        ``plType`` and settings, and the whole group object is posted with
        ``deploy``. Anything less is accepted by the server and then ignored by
        the player, which is why a screen used to download new content and carry
        on showing the old playlist.

        Returns the playlists that were dropped from the group, so the caller can
        say what changed. An empty list means nothing was dropped — the deploy
        still went out, so re-selecting is a way to force a drifted screen back.
        """
        group_id = group.get("_id")
        if not group_id:
            raise PiSignageError("Group has no id, cannot assign a playlist")

        # Re-read the group rather than trusting the poll snapshot: the whole
        # object is posted back, and stale fields would undo console-side edits.
        try:
            current = await self.async_get_group(str(group_id))
        except PiSignageError:
            current = group

        deployed = [
            entry.get("name")
            for entry in (current.get("deployedPlaylists") or [])
            if isinstance(entry, dict)
        ]
        removed = [name for name in deployed if name and name != playlist]

        details = await self.async_get_playlist(playlist)

        # A zone can embed another playlist as __name.json; its files have to be
        # deployed too. Fetch those up front so the builder stays synchronous.
        nested: dict[str, dict[str, Any]] = {}
        for row in details.get("assets") or []:
            if not isinstance(row, dict):
                continue
            for zone in ZONE_KEYS:
                value = row.get(zone)
                if (
                    isinstance(value, str)
                    and value.startswith("__")
                    and ".json" in value
                ):
                    name = value[2 : value.index(".json")]
                    if name not in nested:
                        try:
                            nested[name] = await self.async_get_playlist(name)
                        except PiSignageError:
                            _LOGGER.debug("Zone playlist '%s' could not be read", name)

        entry, assets = build_deploy_payload(current, playlist, details, nested.get)

        body = {key: value for key, value in current.items() if key != "_id"}
        body["playlists"] = [entry]
        body["assets"] = assets
        body["deploy"] = True
        await self._request("POST", f"groups/{quote(str(group_id))}", json=body)

        return removed

    # ------------------------------------------------------------------
    # setup helpers
    # ------------------------------------------------------------------

    async def async_validate_connection(self) -> None:
        """Log in and prove the token works, for use by the config flow."""
        await self.async_login()
        await self._request("GET", "players", params={"page": 1, "per_page": 1})

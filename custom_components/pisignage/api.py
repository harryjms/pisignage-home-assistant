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

#: A freshly deployed playlist is not immediately accepted by ``/setplaylist`` —
#: the player has to sync it first. Retry the pin a few times before complaining.
PIN_ATTEMPTS = 3
PIN_RETRY_DELAY = 2.0

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

    async def async_set_player_playlist(self, player_id: str, playlist: str) -> None:
        """Pin one player to a playlist that is already deployed to it."""
        await self._request(
            "POST", f"setplaylist/{quote(player_id)}/{quote(playlist, safe='')}"
        )

    async def async_deploy_playlist_to_group(
        self, group: dict[str, Any], playlist: str
    ) -> None:
        """Attach *playlist* to *group* and push it to every member player.

        ``POST /groups/{id}`` is a partial update at the top level, but the
        ``playlists`` array is replaced wholesale — so the current array is read
        from *group* and appended to rather than overwritten.
        """
        group_id = group.get("_id")
        if not group_id:
            raise PiSignageError("Group has no id, cannot deploy")

        playlists = [
            entry for entry in (group.get("playlists") or []) if isinstance(entry, dict)
        ]
        if not any(entry.get("name") == playlist for entry in playlists):
            playlists.append({"name": playlist, "settings": {}})

        await self._request(
            "POST",
            f"groups/{quote(str(group_id))}",
            json={"playlists": playlists, "deploy": True},
        )

    async def async_activate_playlist(
        self, player_id: str, group: dict[str, Any], playlist: str
    ) -> bool:
        """Make *player_id* play *playlist*, deploying it first if necessary.

        Returns ``True`` when a group-wide deploy was needed. A deploy makes
        every player in the group re-sync, so callers should surface that.
        """
        deployed = {
            entry.get("name")
            for entry in (group.get("deployedPlaylists") or [])
            if isinstance(entry, dict)
        }

        if playlist in deployed:
            await self.async_set_player_playlist(player_id, playlist)
            return False

        await self.async_deploy_playlist_to_group(group, playlist)

        last_error: Exception | None = None
        for attempt in range(PIN_ATTEMPTS):
            await asyncio.sleep(PIN_RETRY_DELAY)
            try:
                await self.async_set_player_playlist(player_id, playlist)
            except PiSignageAuthError:
                raise
            except PiSignageError as err:
                last_error = err
                _LOGGER.debug(
                    "Pinning %s to %s failed (attempt %s/%s): %s",
                    player_id,
                    playlist,
                    attempt + 1,
                    PIN_ATTEMPTS,
                    err,
                )
            else:
                return True

        raise PiSignageError(
            f"'{playlist}' was deployed to the group, but the player has not "
            f"finished syncing it yet so it could not be started immediately. "
            f"It should begin playing once the sync completes. ({last_error})"
        )

    # ------------------------------------------------------------------
    # setup helpers
    # ------------------------------------------------------------------

    async def async_validate_connection(self) -> None:
        """Log in and prove the token works, for use by the config flow."""
        await self.async_login()
        await self._request("GET", "players", params={"page": 1, "per_page": 1})

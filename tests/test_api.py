"""Tests for the vendored piSignage API client.

These drive a fake session rather than mocking the client, because the point is
to prove the envelope handling, the re-auth retry, and — most importantly — the
deploy-or-not decision that makes a group-wide re-sync happen.
"""

from __future__ import annotations

from typing import Any

import pytest

from custom_components.pisignage import api as api_module
from custom_components.pisignage.api import (
    PER_PAGE,
    PiSignageAuthError,
    PiSignageClient,
    PiSignageError,
    build_base_url,
    normalise_epoch,
)


class FakeResponse:
    """Minimal stand-in for an aiohttp response."""

    def __init__(self, status: int, payload: Any) -> None:
        self.status = status
        self._payload = payload

    async def json(self, content_type: str | None = None) -> Any:
        return self._payload


class FakeSession:
    """Returns queued responses and records every call in order."""

    def __init__(self, responses: list[FakeResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append((method, url, kwargs))
        if not self._responses:
            raise AssertionError(f"Unexpected extra request: {method} {url}")
        return self._responses.pop(0)

    @property
    def paths(self) -> list[str]:
        """Just the request paths, for readable assertions."""
        return [url.split("/api/", 1)[-1] for _, url, _ in self.calls]


def ok(data: Any = None) -> FakeResponse:
    return FakeResponse(200, {"success": True, "stat_message": "ok", "data": data})


def login_ok() -> FakeResponse:
    return ok({"token": "jwt-token"})


def playlist_ok(assets: list[str] | None = None) -> FakeResponse:
    """A GET /playlists/{name} response carrying the playlist's asset rows."""
    return ok({"name": "pl", "assets": [{"filename": a} for a in (assets or [])]})


def make_client(session: FakeSession, token: str | None = None) -> PiSignageClient:
    return PiSignageClient("myco", "admin", "secret", session, token=token)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("myco", "https://myco.pisignage.com/api"),
        ("myco.pisignage.com", "https://myco.pisignage.com/api"),
        ("https://myco.pisignage.com", "https://myco.pisignage.com/api"),
        ("https://myco.pisignage.com/api", "https://myco.pisignage.com/api"),
        ("https://myco.pisignage.com/api/", "https://myco.pisignage.com/api"),
        ("  myco  ", "https://myco.pisignage.com/api"),
    ],
)
def test_build_base_url(value: str, expected: str) -> None:
    assert build_base_url(value) == expected


def test_build_base_url_rejects_empty() -> None:
    with pytest.raises(ValueError):
        build_base_url("   ")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1481008511, 1481008511),
        (1481008511817, 1481008511.817),  # milliseconds get scaled down
        ("1481008511", 1481008511),
        # Live hosted accounts return lastReported as ISO 8601, which used to
        # parse as None and made every screen read offline.
        ("2026-07-27T19:47:14.376Z", 1785181634.376),
        ("2026-07-27T19:47:14Z", 1785181634.0),
        (None, None),
        ("nonsense", None),
        ("", None),
        (0, None),
    ],
)
def test_normalise_epoch(value: Any, expected: Any) -> None:
    assert normalise_epoch(value) == expected


async def test_login_caches_token() -> None:
    session = FakeSession([login_ok(), ok({"objects": [], "pages": 1})])
    client = make_client(session)

    await client.async_get_players()

    assert client.token == "jwt-token"
    # One login, then the actual read — the JWT is not re-fetched per request.
    assert session.paths == ["session", "players"]

    # The token travels in the header, never the query string.
    _, _, players_kwargs = session.calls[1]
    assert players_kwargs["headers"] == {"x-access-token": "jwt-token"}
    assert "token" not in players_kwargs["params"]


async def test_session_response_has_no_envelope() -> None:
    """POST /session answers with a bare object and no `success` field.

    Treating the missing flag as failure made every single login fail, which
    surfaced in the UI as "could not reach piSignage".
    """
    session = FakeSession(
        [
            FakeResponse(
                200,
                {
                    "token": "jwt-token",
                    "userInfo": {"username": "gaydio", "role": "User"},
                },
            ),
            ok({"objects": [], "pages": 1}),
        ]
    )
    client = make_client(session)

    await client.async_get_players()

    assert client.token == "jwt-token"


async def test_bad_password_is_an_auth_error_not_a_connection_error() -> None:
    """A rejected password returns 401 with `message`, not `stat_message`."""
    session = FakeSession(
        [FakeResponse(401, {"message": "Incorrect password.", "error": {}})]
    )
    client = make_client(session)

    with pytest.raises(PiSignageAuthError, match="Incorrect password"):
        await client.async_login()


async def test_expired_token_message_is_preserved() -> None:
    session = FakeSession(
        [
            FakeResponse(
                401,
                {
                    "message": "Your session has expired or you are not "
                    "signed in. Please log in again.",
                    "error": {},
                },
            ),
            FakeResponse(200, {"token": "fresh-token"}),
            ok([{"name": "Promos"}]),
        ]
    )
    client = make_client(session, token="stale-token")

    assert await client.async_get_playlist_names() == ["Promos"]
    assert client.token == "fresh-token"


async def test_success_false_raises_even_on_http_200() -> None:
    """The API reports application failures with a 200, so the body decides."""
    session = FakeSession(
        [
            login_ok(),
            FakeResponse(200, {"success": False, "stat_message": "No such playlist"}),
        ]
    )
    client = make_client(session)

    with pytest.raises(PiSignageError, match="No such playlist"):
        await client.async_get_playlist_names()


async def test_401_reauths_once_then_retries() -> None:
    session = FakeSession(
        [
            FakeResponse(401, {"success": False, "stat_message": "expired"}),
            login_ok(),
            ok([{"name": "Promos"}]),
        ]
    )
    client = make_client(session, token="stale-token")

    assert await client.async_get_playlist_names() == ["Promos"]
    assert session.paths == ["playlists", "session", "playlists"]


async def test_401_twice_gives_up_as_auth_error() -> None:
    session = FakeSession(
        [
            FakeResponse(401, {"success": False, "stat_message": "expired"}),
            login_ok(),
            FakeResponse(401, {"success": False, "stat_message": "expired"}),
        ]
    )
    client = make_client(session, token="stale-token")

    with pytest.raises(PiSignageAuthError):
        await client.async_get_playlist_names()


async def test_pagination_starts_at_page_zero() -> None:
    """Paging is zero-indexed; asking for page 1 first returns nothing at all."""
    session = FakeSession([login_ok(), ok({"objects": [{"_id": "a"}], "pages": 1})])
    client = make_client(session)

    assert await client.async_get_players() == [{"_id": "a"}]

    _, _, kwargs = session.calls[1]
    assert kwargs["params"]["page"] == "0"


async def test_pagination_walks_until_a_short_page() -> None:
    """`pages` and `count` describe the current page, so only length can end it."""
    full_page = [{"_id": f"p{n}"} for n in range(PER_PAGE)]
    session = FakeSession(
        [
            login_ok(),
            # A full page reports pages=1 even though more remain.
            ok({"objects": full_page, "pages": 1, "count": PER_PAGE}),
            ok({"objects": [{"_id": "last"}], "pages": 1, "count": 1}),
        ]
    )
    client = make_client(session)

    players = await client.async_get_players()

    assert len(players) == PER_PAGE + 1
    assert players[-1]["_id"] == "last"
    pages = [kwargs["params"]["page"] for _, _, kwargs in session.calls[1:]]
    assert pages == ["0", "1"]


async def test_pagination_stops_on_empty_page() -> None:
    session = FakeSession([login_ok(), ok({"objects": [], "pages": 99})])
    client = make_client(session)

    assert await client.async_get_players() == []


async def test_playlist_names_are_deduped_and_sorted() -> None:
    session = FakeSession(
        [login_ok(), ok([{"name": "zebra"}, {"name": "Apple"}, {"name": "Apple"}])]
    )
    client = make_client(session)

    assert await client.async_get_playlist_names() == ["Apple", "zebra"]


async def test_assign_deploys_even_when_already_assigned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-selecting must still deploy, so it can force a drifted screen back."""
    monkeypatch.setattr(api_module, "PIN_RETRY_DELAY", 0)
    session = FakeSession([login_ok(), playlist_ok(["logo.png"]), ok(), ok()])
    client = make_client(session)
    group = {
        "_id": "group1",
        "name": "Stores",
        "playlists": [{"name": "Promos"}],
        "deployedPlaylists": [{"name": "Promos"}],
    }

    removed = await client.async_assign_playlist("player1", group, "Promos")

    # Nothing was dropped, but the deploy still went out.
    assert removed == []
    assert session.paths == [
        "session",
        "playlists/Promos",
        "groups/group1",
        "setplaylist/player1/Promos",
    ]
    _, _, kwargs = session.calls[2]
    assert kwargs["json"]["deploy"] is True
    assert [e["name"] for e in kwargs["json"]["playlists"]] == ["Promos"]
    # The group asset list is rebuilt from the playlist, or the screen never
    # actually switches even though the deploy succeeds.
    assert kwargs["json"]["assets"] == ["logo.png"]


async def test_assign_replaces_the_group_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """Assignment must replace, not append.

    Appending left the group playing every playlist ever selected on rotation,
    so a change never stuck. The group must end up with exactly one entry.
    """
    monkeypatch.setattr(api_module, "PIN_RETRY_DELAY", 0)
    session = FakeSession([login_ok(), playlist_ok(["sale.mp4"]), ok(), ok()])
    client = make_client(session)
    group = {
        "_id": "group1",
        "name": "Stores",
        "playlists": [{"name": "Promos", "settings": {}}],
        "deployedPlaylists": [{"name": "Promos", "settings": {}}],
    }

    removed = await client.async_assign_playlist("player1", group, "NewYearSale")

    assert removed == ["Promos"]
    assert session.paths == [
        "session",
        "playlists/NewYearSale",
        "groups/group1",
        "setplaylist/player1/NewYearSale",
    ]

    _, _, deploy_kwargs = session.calls[2]
    body = deploy_kwargs["json"]
    assert body["deploy"] is True
    assert [entry["name"] for entry in body["playlists"]] == ["NewYearSale"]
    assert body["assets"] == ["sale.mp4"]


async def test_assign_keeps_existing_schedule_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A playlist the group already knows keeps its per-group schedule."""
    monkeypatch.setattr(api_module, "PIN_RETRY_DELAY", 0)
    session = FakeSession([login_ok(), playlist_ok(), ok(), ok()])
    client = make_client(session)
    scheduled = {
        "name": "Gaydio Gold",
        "skipForSchedule": False,
        "settings": {"timeEnable": True, "starttime": "19:55", "endtime": "00:00"},
    }
    group = {
        "_id": "group1",
        "name": "Studio 1",
        "playlists": [{"name": "Logo"}, scheduled],
        "deployedPlaylists": [{"name": "Logo"}, scheduled],
    }

    removed = await client.async_assign_playlist("player1", group, "Gaydio Gold")

    assert removed == ["Logo"]
    _, _, deploy_kwargs = session.calls[2]
    assert deploy_kwargs["json"]["playlists"] == [scheduled]


async def test_assign_survives_a_failed_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    """The deploy is what persists; a slow player must not fail the call."""
    monkeypatch.setattr(api_module, "PIN_RETRY_DELAY", 0)

    def failure() -> FakeResponse:
        return FakeResponse(
            200, {"success": False, "stat_message": "playlist not deployed"}
        )

    session = FakeSession(
        [login_ok(), playlist_ok(["x.mp4"]), ok(), failure(), failure(), failure()]
    )
    client = make_client(session)
    group = {
        "_id": "group1",
        "name": "Stores",
        "playlists": [],
        "deployedPlaylists": [{"name": "Old"}],
    }

    removed = await client.async_assign_playlist("player1", group, "NewYearSale")

    assert removed == ["Old"]
    assert session.paths[2] == "groups/group1"


async def test_playlist_name_with_spaces_is_encoded() -> None:
    session = FakeSession([login_ok(), playlist_ok(), ok(), ok()])
    client = make_client(session)
    group = {
        "_id": "group1",
        "playlists": [{"name": "Summer Sale"}],
        "deployedPlaylists": [{"name": "Summer Sale"}],
    }

    await client.async_assign_playlist("player1", group, "Summer Sale")

    # Both the asset lookup and the pin have to encode the space.
    assert session.paths[1] == "playlists/Summer%20Sale"
    assert session.paths[-1] == "setplaylist/player1/Summer%20Sale"


async def test_assign_without_group_id_raises() -> None:
    session = FakeSession([login_ok()])
    client = make_client(session)

    with pytest.raises(PiSignageError, match="no id"):
        await client.async_assign_playlist("player1", {}, "Promos")


async def test_redeploy_re_deploys_the_group_and_pins() -> None:
    """The follow-up nudge deploys the group again and pins, like Deploy does."""
    session = FakeSession([login_ok(), playlist_ok(["ny.mp4"]), ok(), ok()])
    client = make_client(session)
    group = {
        "_id": "group1",
        "name": "Stores",
        "playlists": [{"name": "NewYearSale", "settings": {}}],
        "deployedPlaylists": [{"name": "NewYearSale", "settings": {}}],
    }

    await client.async_redeploy_playlist("player1", group, "NewYearSale")

    assert session.paths == [
        "session",
        "playlists/NewYearSale",
        "groups/group1",
        "setplaylist/player1/NewYearSale",
    ]
    _, _, deploy_kwargs = session.calls[2]
    assert deploy_kwargs["json"]["deploy"] is True
    assert [e["name"] for e in deploy_kwargs["json"]["playlists"]] == ["NewYearSale"]
    assert deploy_kwargs["json"]["assets"] == ["ny.mp4"]


async def test_redeploy_keeps_existing_schedule_settings() -> None:
    """Re-deploying must not strip the playlist's per-group schedule."""
    session = FakeSession([login_ok(), playlist_ok(), ok(), ok()])
    client = make_client(session)
    scheduled = {
        "name": "Gaydio Gold",
        "settings": {"timeEnable": True, "starttime": "19:55", "endtime": "00:00"},
    }
    group = {
        "_id": "group1",
        "playlists": [scheduled],
        "deployedPlaylists": [scheduled],
    }

    await client.async_redeploy_playlist("player1", group, "Gaydio Gold")

    _, _, deploy_kwargs = session.calls[2]
    # The schedule survives; only skipForSchedule is forced on so it plays.
    assert deploy_kwargs["json"]["playlists"] == [
        {**scheduled, "skipForSchedule": False}
    ]


async def test_redeploy_survives_a_failed_pin() -> None:
    """A slow player rejecting the pin must not turn the nudge into an error."""
    session = FakeSession(
        [
            login_ok(),
            playlist_ok(["x.mp4"]),
            ok(),
            FakeResponse(200, {"success": False, "stat_message": "not deployed"}),
        ]
    )
    client = make_client(session)
    group = {"_id": "group1", "playlists": [], "deployedPlaylists": []}

    # The deploy went out; the failed pin is swallowed rather than raised.
    await client.async_redeploy_playlist("player1", group, "NewYearSale")

    assert session.paths[2] == "groups/group1"


async def test_redeploy_without_group_id_raises() -> None:
    session = FakeSession([login_ok()])
    client = make_client(session)

    with pytest.raises(PiSignageError, match="no id"):
        await client.async_redeploy_playlist("player1", {}, "Promos")


async def test_assign_deploys_without_assets_when_playlist_lookup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deploy plain if the asset list cannot be read.

    Sending an empty asset list would be worse than sending none — it would drop
    the playlist from scheduling entirely.
    """
    monkeypatch.setattr(api_module, "PIN_RETRY_DELAY", 0)
    lookup_fails = FakeResponse(200, {"success": False, "stat_message": "no playlist"})
    session = FakeSession([login_ok(), lookup_fails, ok(), ok()])
    client = make_client(session)
    group = {"_id": "group1", "playlists": [], "deployedPlaylists": []}

    await client.async_assign_playlist("player1", group, "NewYearSale")

    _, _, deploy_kwargs = session.calls[2]
    # The deploy still happened, but with no 'assets' key (not an empty list).
    assert deploy_kwargs["json"]["deploy"] is True
    assert "assets" not in deploy_kwargs["json"]


async def test_get_playlist_assets_collects_filenames() -> None:
    session = FakeSession(
        [
            login_ok(),
            ok(
                {
                    "name": "Promos",
                    "assets": [
                        {"filename": "a.mp4"},
                        {"filename": "b.png"},
                        {"filename": "a.mp4"},  # duplicate is dropped
                        {"duration": 10},  # no filename is ignored
                    ],
                }
            ),
        ]
    )
    client = make_client(session)

    assert await client.async_get_playlist_assets("Promos") == ["a.mp4", "b.png"]

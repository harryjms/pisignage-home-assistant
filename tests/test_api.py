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
        (None, None),
        ("nonsense", None),
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


async def test_pagination_walks_then_stops() -> None:
    session = FakeSession(
        [
            login_ok(),
            ok({"objects": [{"_id": "a"}], "pages": 2}),
            ok({"objects": [{"_id": "b"}], "pages": 2}),
        ]
    )
    client = make_client(session)

    players = await client.async_get_players()

    assert [player["_id"] for player in players] == ["a", "b"]


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


async def test_activate_already_deployed_playlist_does_not_deploy() -> None:
    """The fast path must not touch the group — a deploy re-syncs every player."""
    session = FakeSession([login_ok(), ok()])
    client = make_client(session)
    group = {
        "_id": "group1",
        "name": "Stores",
        "playlists": [{"name": "Promos"}],
        "deployedPlaylists": [{"name": "Promos"}],
    }

    deployed = await client.async_activate_playlist("player1", group, "Promos")

    assert deployed is False
    assert session.paths == ["session", "setplaylist/player1/Promos"]


async def test_activate_undeployed_playlist_deploys_then_pins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api_module, "PIN_RETRY_DELAY", 0)
    session = FakeSession([login_ok(), ok(), ok()])
    client = make_client(session)
    group = {
        "_id": "group1",
        "name": "Stores",
        "playlists": [{"name": "Promos", "settings": {}}],
        "deployedPlaylists": [{"name": "Promos", "settings": {}}],
    }

    deployed = await client.async_activate_playlist("player1", group, "NewYearSale")

    assert deployed is True
    assert session.paths == [
        "session",
        "groups/group1",
        "setplaylist/player1/NewYearSale",
    ]

    # The existing playlist must survive — the API replaces the array wholesale.
    _, _, deploy_kwargs = session.calls[1]
    body = deploy_kwargs["json"]
    assert body["deploy"] is True
    assert [entry["name"] for entry in body["playlists"]] == ["Promos", "NewYearSale"]


async def test_activate_reports_when_pin_never_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deploy that lands but never pins must not be reported as success."""
    monkeypatch.setattr(api_module, "PIN_RETRY_DELAY", 0)

    def failure() -> FakeResponse:
        return FakeResponse(
            200, {"success": False, "stat_message": "playlist not deployed"}
        )

    session = FakeSession([login_ok(), ok(), failure(), failure(), failure()])
    client = make_client(session)
    group = {
        "_id": "group1",
        "name": "Stores",
        "playlists": [],
        "deployedPlaylists": [],
    }

    with pytest.raises(PiSignageError, match="has not finished syncing"):
        await client.async_activate_playlist("player1", group, "NewYearSale")


async def test_playlist_name_with_spaces_is_encoded() -> None:
    session = FakeSession([login_ok(), ok()])
    client = make_client(session)
    group = {
        "_id": "group1",
        "deployedPlaylists": [{"name": "Summer Sale"}],
    }

    await client.async_activate_playlist("player1", group, "Summer Sale")

    assert session.paths[-1] == "setplaylist/player1/Summer%20Sale"


async def test_deploy_without_group_id_raises() -> None:
    session = FakeSession([login_ok()])
    client = make_client(session)

    with pytest.raises(PiSignageError, match="no id"):
        await client.async_deploy_playlist_to_group({}, "Promos")

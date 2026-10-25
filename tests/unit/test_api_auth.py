"""Tests for token handling and error mapping in the API client."""
from __future__ import annotations

import asyncio
from typing import Any

import aiohttp
import pytest

from custom_components.intuis_connect.intuis_api.api import (
    CannotConnect,
    IntuisAPI,
    InvalidAuth,
)


class FakeResponse:
    """Minimal aiohttp response stand-in."""

    def __init__(self, status: int, payload: Any = None) -> None:
        self.status = status
        self._payload = payload if payload is not None else {}
        self.headers: dict[str, str] = {}
        self.request_info = None
        self.history = ()
        self.released = False

    async def json(self) -> Any:
        return self._payload

    async def text(self) -> str:
        return str(self._payload)

    async def release(self) -> None:
        self.released = True

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise aiohttp.ClientResponseError(None, (), status=self.status)

    async def __aenter__(self) -> "FakeResponse":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None


class _PostContext:
    def __init__(self, session: "FakeSession", url: str, kwargs: dict) -> None:
        self._session, self._url, self._kwargs = session, url, kwargs

    async def __aenter__(self) -> FakeResponse:
        self._session.posts.append((self._url, self._kwargs))
        result = self._session.post_results.pop(0)
        if isinstance(result, Exception):
            raise result
        await asyncio.sleep(0)
        return result

    async def __aexit__(self, *args: Any) -> None:
        return None


class FakeSession:
    """Session returning queued results for post() and request()."""

    def __init__(self, post_results=(), request_results=()) -> None:
        self.post_results = list(post_results)
        self.request_results = list(request_results)
        self.posts: list = []
        self.requests: list = []

    def post(self, url: str, **kwargs: Any) -> _PostContext:
        return _PostContext(self, url, kwargs)

    async def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.requests.append((method, url, kwargs))
        return self.request_results.pop(0)


def _token(access: str = "access_new", refresh: str | None = "refresh_new") -> dict:
    data = {"access_token": access, "expires_in": 10800}
    if refresh is not None:
        data["refresh_token"] = refresh
    return data


def _api(session: FakeSession) -> IntuisAPI:
    api = IntuisAPI(session, home_id="home_1", min_request_delay=0)
    api.refresh_token = "refresh_old"
    return api


@pytest.mark.parametrize("status", [500, 503, 429])
async def test_refresh_server_errors_are_not_auth_failures(status):
    """A cloud outage must not ask the user to log in again."""
    api = _api(FakeSession(post_results=[FakeResponse(status)]))
    with pytest.raises(CannotConnect):
        await api.async_refresh_access_token()


@pytest.mark.parametrize("status", [400, 401, 403])
async def test_refresh_refused_is_auth_failure(status):
    api = _api(FakeSession(post_results=[FakeResponse(status)]))
    with pytest.raises(InvalidAuth):
        await api.async_refresh_access_token()


async def test_refresh_network_error_is_cannot_connect():
    api = _api(FakeSession(post_results=[aiohttp.ClientConnectionError("down")]))
    with pytest.raises(CannotConnect):
        await api.async_refresh_access_token()


async def test_refresh_keeps_token_when_none_returned_and_reports_changes():
    seen: list[str] = []
    session = FakeSession(
        post_results=[FakeResponse(200, _token(refresh=None)), FakeResponse(200, _token(refresh="rotated"))]
    )
    api = _api(session)
    api.set_refresh_token_callback(seen.append)

    await api.async_refresh_access_token()
    assert api.refresh_token == "refresh_old"
    assert seen == []

    await api.async_refresh_access_token(failed_access_token="access_new")
    assert api.refresh_token == "rotated"
    assert seen == ["rotated"]


async def test_concurrent_refreshes_send_one_request():
    """Two requests refused with the same token trigger a single refresh."""
    session = FakeSession(post_results=[FakeResponse(200, _token())])
    api = _api(session)
    api._access_token = "access_old"

    await asyncio.gather(
        api.async_refresh_access_token(failed_access_token="access_old"),
        api.async_refresh_access_token(failed_access_token="access_old"),
    )
    assert len(session.posts) == 1


async def test_401_retry_keeps_url_headers_and_releases():
    session = FakeSession(
        post_results=[FakeResponse(200, _token())],
        request_results=[FakeResponse(401), FakeResponse(200, {"ok": True})],
    )
    api = _api(session)
    api._access_token = "access_old"
    api._expiry = asyncio.get_running_loop().time() + 3600
    first_response = session.request_results[0]

    resp = await api._async_request(
        "get", "/ignored", full_url="https://example.test/full",
        headers={"X-Test": "1"}, timeout=5,
    )

    assert resp.status == 200
    assert first_response.released
    (_, url1, kw1), (_, url2, kw2) = session.requests
    assert url1 == url2 == "https://example.test/full"
    assert kw2["headers"] == {"X-Test": "1", "Authorization": "Bearer access_new"}
    assert kw2["timeout"] == 5


async def test_401_after_refresh_is_auth_failure():
    session = FakeSession(
        post_results=[FakeResponse(200, _token())],
        request_results=[FakeResponse(401), FakeResponse(401)],
    )
    api = _api(session)
    api._access_token = "access_old"
    api._expiry = asyncio.get_running_loop().time() + 3600
    with pytest.raises(InvalidAuth):
        await api._async_request("get", "/path")


async def test_login_wrong_password_is_invalid_auth():
    from custom_components.intuis_connect.utils.const import BASE_URLS

    session = FakeSession(post_results=[FakeResponse(400) for _ in BASE_URLS])
    with pytest.raises(InvalidAuth):
        await IntuisAPI(session).async_login("user", "wrong")


async def test_login_unreachable_cluster_is_cannot_connect():
    from custom_components.intuis_connect.utils.const import BASE_URLS

    results: list = [FakeResponse(400) for _ in BASE_URLS]
    results[-1] = aiohttp.ClientConnectionError("down")
    session = FakeSession(post_results=results)
    with pytest.raises(CannotConnect):
        await IntuisAPI(session).async_login("user", "secret")

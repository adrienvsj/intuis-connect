"""History import and daily energy behaviour (real recorder where it matters)."""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.intuis_connect.history_import import (
    HistoryImportManager,
    async_import_energy_history,
)
from custom_components.intuis_connect.intuis_api.api import (
    CannotConnect,
    RateLimitError,
)
from custom_components.intuis_connect.intuis_data import IntuisData
from custom_components.intuis_connect.utils.const import DOMAIN, HISTORY_IMPORT_KEY

TZ = ZoneInfo("Europe/Paris")
HOME_ID = "home_1"


def _home(room_ids=("room_1",)) -> MagicMock:
    home = MagicMock()
    home.id = HOME_ID
    home.timezone = "Europe/Paris"
    rooms = {}
    for room_id in room_ids:
        room = MagicMock()
        room.name = f"Room {room_id}"
        rooms[room_id] = room
    home.rooms = rooms
    return home


@pytest.fixture
async def recorder_hass(recorder_mock, hass, freezer):
    await hass.config.async_set_time_zone("Europe/Paris")
    freezer.move_to(datetime(2026, 10, 20, 12, 30, tzinfo=TZ))
    with patch("custom_components.intuis_connect.history_import.API_DELAY_SECONDS", 0):
        yield hass


def _register(hass, room_id: str = "room_1") -> str:
    return er.async_get(hass).async_get_or_create(
        "sensor", DOMAIN, f"intuis_{HOME_ID}_{room_id}_energy", suggested_object_id=f"{room_id}_energy"
    ).entity_id


async def _run(hass, api, home, days=50):
    manager = HistoryImportManager(hass, "entry_1")
    await manager.async_load()
    result = await async_import_energy_history(
        hass=hass, api=api, intuis_home=home, manager=manager, days=days,
        room_filter=None, home_id=HOME_ID,
    )
    await async_wait_recording_done(hass)
    return result


async def _rows(hass, entity_id):
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, datetime(2026, 1, 1, tzinfo=UTC), None,
        {entity_id}, "hour", None, {"sum"},
    )
    return stats.get(entity_id, [])


async def test_chunk_boundary_hour_counted_once(recorder_hass):
    """Two chunks both returning their boundary hour must not double it."""
    hass = recorder_hass
    entity_id = _register(hass)

    async def hourly(room_id, begin, end):
        # Inclusive of the end hour, like the API at chunk boundaries
        return [(ts, 100) for ts in range(begin, end + 1, 3600)]

    api = MagicMock()
    api.async_get_room_energy_hourly = AsyncMock(side_effect=hourly)
    api.circuit_breaker.check.return_value = 0

    await _run(hass, api, _home())

    assert api.async_get_room_energy_hourly.await_count == 2
    rows = await _rows(hass, entity_id)
    assert rows
    assert round(rows[-1]["sum"], 3) == round(0.1 * len(rows), 3)
    # Only complete, settled hours: the last one is 11:00 local at 12:30
    assert datetime.fromtimestamp(rows[-1]["start"], tz=TZ) <= datetime(2026, 10, 20, 11, tzinfo=TZ)
    assert hass.data[DOMAIN][HISTORY_IMPORT_KEY][HOME_ID] is False


async def test_failed_chunk_skips_the_room(recorder_hass):
    """A chunk that fails must not leave a gap: nothing is imported for the room."""
    hass = recorder_hass
    entity_id = _register(hass)
    api = MagicMock()
    api.async_get_room_energy_hourly = AsyncMock(
        side_effect=[[(int(datetime(2026, 9, 1, tzinfo=UTC).timestamp()), 100)], CannotConnect("down")]
    )
    api.circuit_breaker.check.return_value = 0

    result = await _run(hass, api, _home())

    assert result["errors"]
    assert await _rows(hass, entity_id) == []
    assert hass.data[DOMAIN][HISTORY_IMPORT_KEY][HOME_ID] is False


async def test_early_return_does_not_pause_hourly_stats(recorder_hass):
    """No importable room: the 'import running' flag must not stay set."""
    hass = recorder_hass
    api = MagicMock()
    result = await _run(hass, api, _home())  # no energy entity registered
    assert result == {"error": "No rooms found"}
    assert not hass.data.get(DOMAIN, {}).get(HISTORY_IMPORT_KEY, {}).get(HOME_ID)


async def test_failed_daily_energy_keeps_last_value(mock_api, mock_intuis_home, sample_room_data):
    """A room whose energy fetch fails keeps its last value instead of dropping to 0."""
    room = sample_room_data["room_123"]
    room.bridge_id = "bridge_1"
    options = {"energy_scale": "5min"}
    data = IntuisData(api=mock_api, intuis_home=mock_intuis_home, overrides={}, get_options=lambda: options)
    mock_api.async_get_energy_measures = AsyncMock(side_effect=[{"room_123": 1500.0}, {}])
    now = datetime(2026, 10, 20, 12, 30)

    await data._fetch_energy_data(sample_room_data, now)
    assert room.energy == 1.5
    room.energy = 0.0  # rooms are rebuilt on every poll
    await data._fetch_energy_data(sample_room_data, now)
    assert room.energy == 1.5


async def test_energy_measures_stop_on_rate_limit():
    """A rate limit stops the per-room loop instead of hitting the API for every room."""
    from custom_components.intuis_connect.intuis_api.api import IntuisAPI

    api = IntuisAPI(MagicMock(), home_id=HOME_ID)
    with patch.object(
        api, "_async_get_room_energy", AsyncMock(side_effect=[100.0, RateLimitError("429"), 300.0])
    ) as per_room:
        result = await api.async_get_energy_measures(
            [{"id": "a"}, {"id": "b"}, {"id": "c"}], 0, 1
        )
    assert result == {"a": 100.0}
    assert per_room.await_count == 2

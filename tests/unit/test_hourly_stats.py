"""Hourly energy statistics against a real recorder."""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_import_statistics,
    statistics_during_period,
)
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.intuis_connect.hourly_stats import HourlyStatsUpdater
from custom_components.intuis_connect.utils.const import DOMAIN

TZ = ZoneInfo("Europe/Paris")
HOME_ID = "home_1"
WH_PER_HOUR = 100  # every hour consumes 0.1 kWh


def _local(*args: int) -> datetime:
    return datetime(*args, tzinfo=TZ)


@pytest.fixture
async def setup(recorder_mock, hass):
    """Register the energy sensor and return helpers for one room."""
    await hass.config.async_set_time_zone("Europe/Paris")
    entity_id = er.async_get(hass).async_get_or_create(
        "sensor", DOMAIN, f"intuis_{HOME_ID}_room_1_energy", suggested_object_id="salon_energy"
    ).entity_id

    room = MagicMock()
    room.name = "Salon"
    home = MagicMock()
    home.id = HOME_ID
    home.rooms = {"room_1": room}

    async def hourly(room_id, date_begin, date_end):
        """Every hour from date_begin up to the hour containing date_end."""
        return [(ts, WH_PER_HOUR) for ts in range(date_begin, date_end, 3600)]

    api = MagicMock()
    api.async_get_room_energy_hourly = AsyncMock(side_effect=hourly)
    updater = HourlyStatsUpdater(hass, api, home, "entry_1", HOME_ID, 120, "Europe/Paris")
    return hass, updater, api, entity_id


def _metadata(entity_id: str) -> StatisticMetaData:
    return StatisticMetaData(
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        name="Salon Energy",
        source="recorder",
        statistic_id=entity_id,
        unit_of_measurement="kWh",
        unit_class="energy",
    )


async def _seed(hass, entity_id: str, rows: list[tuple[datetime, float]]) -> None:
    async_import_statistics(
        hass,
        _metadata(entity_id),
        [StatisticData(start=start, state=total, sum=total) for start, total in rows],
    )
    await async_wait_recording_done(hass)


async def _sums(hass, entity_id: str, start: datetime) -> dict[datetime, float]:
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, start, None, {entity_id}, "hour", None, {"sum"}
    )
    return {
        datetime.fromtimestamp(row["start"], tz=TZ): round(row["sum"], 3)
        for row in stats.get(entity_id, [])
    }


@pytest.mark.parametrize(
    "storage",
    [
        {},  # storage lost
        {"midnight_bases": {"sensor.salon_energy": {"2026-10-19": 50.0}},
         "anchors": {"sensor.salon_energy": {"prev_sum": 60.0, "prev_start": 0}}},  # rolled back
        {"midnight_bases": {"sensor.old_name": {"2026-10-19": 100.0}}, "anchors": {}},  # renamed
    ],
    ids=["no-storage", "storage-behind", "entity-renamed"],
)
async def test_chains_from_recorder_sum(setup, freezer, storage):
    """The window continues from the recorder's sum, whatever the storage says."""
    hass, updater, _api, entity_id = setup
    await _seed(hass, entity_id, [(_local(2026, 10, 18, 22), 99.9), (_local(2026, 10, 18, 23), 100.0)])
    freezer.move_to(_local(2026, 10, 20, 12, 30))
    await updater.load_storage()
    updater._data.update(storage)

    await updater.async_update()
    await async_wait_recording_done(hass)

    sums = await _sums(hass, entity_id, _local(2026, 10, 18, 23))
    assert sums[_local(2026, 10, 18, 23)] == 100.0
    assert sums[_local(2026, 10, 19, 0)] == 100.1
    assert sums[_local(2026, 10, 20, 11)] == 103.6
    assert updater._data["midnight_bases"][entity_id]["2026-10-20"] == 102.4


@pytest.mark.parametrize(
    ("now", "last_hour"),
    [((2026, 10, 20, 12, 30), 11), ((2026, 10, 20, 12, 10), 10)],
    ids=["settled", "previous-hour-not-settled"],
)
async def test_only_settled_hours_are_imported(setup, freezer, now, last_hour):
    """The current hour, and the previous one during its first 15 minutes, are skipped."""
    hass, updater, _api, entity_id = setup
    await _seed(hass, entity_id, [(_local(2026, 10, 18, 23), 100.0)])
    freezer.move_to(_local(*now))

    await updater.async_update()
    await async_wait_recording_done(hass)

    sums = await _sums(hass, entity_id, _local(2026, 10, 19, 0))
    assert max(sums) == _local(2026, 10, 20, last_hour)


async def test_catches_up_after_downtime(setup, freezer):
    """After several days without imports the window restarts after the last hour."""
    hass, updater, api, entity_id = setup
    await _seed(
        hass,
        entity_id,
        [(_local(2026, 10, 14, 23), 79.0)]
        + [(_local(2026, 10, 15, h), round(79.0 + 0.1 * (h + 1), 3)) for h in range(11)],
    )
    freezer.move_to(_local(2026, 10, 20, 12, 30))

    await updater.async_update()
    await async_wait_recording_done(hass)

    assert api.async_get_room_energy_hourly.await_args.kwargs["date_begin"] == int(
        _local(2026, 10, 15).timestamp()
    )
    sums = await _sums(hass, entity_id, _local(2026, 10, 15))
    assert sums[_local(2026, 10, 15, 0)] == 79.1
    hours = int((_local(2026, 10, 20, 12) - _local(2026, 10, 15)) / timedelta(hours=1))
    assert sums[_local(2026, 10, 20, 11)] == round(79.0 + 0.1 * hours, 3)
    assert len(sums) == hours


def test_complete_hours_dedups_without_summing():
    """Two points for the same hour keep one value, never their sum."""
    start = int(_local(2026, 10, 20, 8).timestamp())
    end_ms = int(_local(2026, 10, 20, 12).timestamp() * 1000)
    points = HourlyStatsUpdater._complete_hours(
        [(start + 3600, 50), (start, 30), (start + 1800, 40), (start + 4 * 3600, 99)], end_ms
    )
    assert points == [(start + 1800, 40), (start + 3600, 50)]


async def test_dst_fall_back_day_has_25_continuous_hours(setup, freezer):
    """25 Oct 2026 (Europe/Paris) has 25 hours: all are imported, sums stay continuous."""
    hass, updater, _api, entity_id = setup
    await _seed(hass, entity_id, [(_local(2026, 10, 24, 23), 100.0)])
    freezer.move_to(_local(2026, 10, 26, 12, 30))

    await updater.async_update()
    await async_wait_recording_done(hass)

    # Keyed by UTC timestamp: the two local 02:00 starts compare equal as
    # aware datetimes (fold is ignored), so they must not be dict keys.
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, _local(2026, 10, 25), _local(2026, 10, 26),
        {entity_id}, "hour", None, {"sum"},
    )
    rows = stats[entity_id]
    assert len(rows) == 25
    values = [round(row["sum"], 3) for row in rows]
    assert values[0] == 100.1
    assert all(round(b - a, 3) == 0.1 for a, b in zip(values, values[1:]))
    assert updater._data["midnight_bases"][entity_id]["2026-10-26"] == 102.5

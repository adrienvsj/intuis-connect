"""Tests that run the real service handlers on a Home Assistant instance."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.intuis_connect.entity.intuis_home import IntuisHome
from custom_components.intuis_connect.entity.intuis_schedule import (
    IntuisRoomTemperature,
    IntuisThermSchedule,
    IntuisThermZone,
    IntuisTimetable,
)
from custom_components.intuis_connect.services import async_register_services
from custom_components.intuis_connect.utils.const import DOMAIN

HOME_ID = "home_123"
COMFORT, NIGHT = 0, 1


def _make_home() -> IntuisHome:
    """Build a home with one active schedule: Comfort 07:00-22:00, Night otherwise."""
    zones = [
        IntuisThermZone(
            id=COMFORT, name="Comfort", type=0,
            rooms_temp=[IntuisRoomTemperature(room_id="room_1", temp=20)], rooms=[],
        ),
        IntuisThermZone(
            id=NIGHT, name="Night", type=1,
            rooms_temp=[IntuisRoomTemperature(room_id="room_1", temp=17)], rooms=[],
        ),
    ]
    timetables = [IntuisTimetable(zone_id=NIGHT, m_offset=0)]
    for day in range(7):
        timetables.append(IntuisTimetable(zone_id=COMFORT, m_offset=day * 1440 + 420))
        timetables.append(IntuisTimetable(zone_id=NIGHT, m_offset=day * 1440 + 1320))
    schedule = IntuisThermSchedule(
        timetables=timetables, zones=zones, name="Weekly", default=False,
        away_temp=12, hg_temp=7, id="sched_1", type="therm", selected=True,
    )
    return IntuisHome(
        id=HOME_ID, name="Home", coordinates=(0.0, 0.0), country="FR",
        timezone="Europe/Paris", rooms={}, schedules=[schedule],
    )


@pytest.fixture
async def services_setup(hass):
    """Register the services against a fake entry and return its API mock."""
    entry = MockConfigEntry(domain=DOMAIN, data={}, entry_id="entry_1")
    entry.add_to_hass(hass)
    api = MagicMock()
    api.async_sync_schedule = AsyncMock(return_value=None)
    coordinator = MagicMock()
    coordinator.async_request_refresh = AsyncMock()
    home = _make_home()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "api": api,
        "intuis_home": home,
        "coordinator": coordinator,
    }
    await async_register_services(hass, entry)
    return api, home


async def test_set_schedule_slot_with_schedule_card_payload(hass, services_setup):
    """The exact payload sent by intuis-schedule-card 1.4.0 must sync the schedule."""
    api, home = services_setup

    await hass.services.async_call(
        DOMAIN,
        "set_schedule_slot",
        {
            "start_day": "0",
            "end_day": "0",
            "start_time": "12:00",
            "end_time": "14:00",
            "zone_name": "Night",
        },
        blocking=True,
    )

    api.async_sync_schedule.assert_awaited_once()
    kwargs = api.async_sync_schedule.await_args.kwargs
    assert kwargs["away_temp"] == 12
    assert kwargs["hg_temp"] == 7
    offsets = {t["m_offset"]: t["zone_id"] for t in kwargs["timetable"]}
    assert offsets[720] == NIGHT
    assert offsets[840] == COMFORT
    # The local copy follows the synced timetable.
    local = {t.m_offset: t.zone_id for t in home.schedules[0].timetables}
    assert local == offsets

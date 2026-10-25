"""Schedule slot editing, service validation and calendar events."""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.intuis_connect.calendar import IntuisScheduleCalendar
from custom_components.intuis_connect.entity.intuis_home import IntuisHome
from custom_components.intuis_connect.entity.intuis_schedule import (
    IntuisRoomTemperature,
    IntuisThermSchedule,
    IntuisThermZone,
    IntuisTimetable,
)
from custom_components.intuis_connect.services import async_register_services
from custom_components.intuis_connect.timetable import apply_slot
from custom_components.intuis_connect.utils.const import DOMAIN

COMFORT, ECO, NIGHT = 0, 1, 2
MON, SUN = 0, 6 * 1440


def _tt(*pairs: tuple[int, int]) -> list[dict]:
    return [{"m_offset": o, "zone_id": z} for o, z in pairs]


def _as_map(timetable: list[dict]) -> dict[int, int]:
    return {e["m_offset"]: e["zone_id"] for e in timetable}


def test_slot_replaces_entries_inside_it():
    """08:00-12:00 Night on '07 Comfort, 09 Eco, 17 Comfort' must not keep 09 Eco."""
    timetable = _tt((0, NIGHT), (420, COMFORT), (540, ECO), (1020, COMFORT))
    result = _as_map(apply_slot(timetable, 480, 720, NIGHT))
    assert result == {0: NIGHT, 420: COMFORT, 480: NIGHT, 720: ECO, 1020: COMFORT}


def test_slot_over_sunday_midnight_wraps_to_monday():
    """Sunday 22:00 -> Monday 06:00 covers Monday 00:00 and restores at 06:00."""
    timetable = _tt((0, NIGHT), (420, COMFORT), (SUN + 1320, NIGHT))
    result = _as_map(apply_slot(timetable, SUN + 1320, MON + 360, ECO))
    assert result == {0: ECO, 360: NIGHT, 420: COMFORT, SUN + 1320: ECO}


def test_sunday_slot_ending_at_midnight():
    """A Sunday slot ending at 24:00 (offset 10080) restores Monday's zone at 0."""
    timetable = _tt((0, NIGHT), (420, COMFORT), (SUN + 1200, NIGHT))
    result = _as_map(apply_slot(timetable, SUN + 1200, 7 * 1440, COMFORT))
    assert result == {0: NIGHT, 420: COMFORT, SUN + 1200: COMFORT}


def _home(selected_second: bool = False) -> IntuisHome:
    def schedule(sid, name, zone_names, selected):
        zones = [
            IntuisThermZone(id=i, name=n, type=i,
                            rooms_temp=[IntuisRoomTemperature(room_id="room_1", temp=19)], rooms=[])
            for i, n in enumerate(zone_names)
        ]
        timetables = [IntuisTimetable(zone_id=0, m_offset=0), IntuisTimetable(zone_id=1, m_offset=420)]
        return IntuisThermSchedule(timetables=timetables, zones=zones, name=name, default=False,
                                   away_temp=12, hg_temp=7, id=sid, type="therm", selected=selected)

    return IntuisHome(
        id="home_1", name="Home", coordinates=(0.0, 0.0), country="FR", timezone="Europe/Paris",
        rooms={}, schedules=[
            schedule("s1", "Semaine", ["Nuit", "Confort"], not selected_second),
            schedule("s2", "Vacances", ["Hors-gel", "Tiède"], selected_second),
        ],
    )


@pytest.fixture
async def services(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={}, entry_id="entry_1")
    entry.add_to_hass(hass)
    api = MagicMock()
    api.async_sync_schedule = AsyncMock()
    home = _home()
    coordinator = MagicMock(async_request_refresh=AsyncMock())
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "api": api, "intuis_home": home, "coordinator": coordinator,
    }
    await async_register_services(hass)
    return api, home


async def test_zone_of_a_schedule_activated_later_is_accepted(hass, services):
    """Zone names are checked against the live active schedule, not startup options."""
    api, home = services
    home.schedules[0].selected, home.schedules[1].selected = False, True

    await hass.services.async_call(
        DOMAIN, "set_schedule_slot",
        {"start_day": "2", "end_day": "2", "start_time": "10:00", "end_time": "11:00",
         "zone_name": "Tiède", "home_id": "home_1"},
        blocking=True,
    )
    assert api.async_sync_schedule.await_args.kwargs["schedule_id"] == "s2"


async def test_unknown_zone_is_reported_to_the_caller(hass, services):
    """The schedule card must get an error instead of a silent no-op."""
    api, _home_obj = services
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "set_schedule_slot",
            {"start_day": "0", "end_day": "0", "start_time": "10:00", "end_time": "11:00",
             "zone_name": "Missing"},
            blocking=True,
        )
    api.async_sync_schedule.assert_not_awaited()


async def test_calendar_returns_every_week(hass):
    """A two-week range returns the events of both weeks."""
    tz = ZoneInfo("Europe/Paris")
    home = _home()
    coordinator = MagicMock(data={"intuis_home": home})
    calendar = IntuisScheduleCalendar(coordinator, MagicMock(), "home_1", home, home.schedules[0])

    start = datetime(2026, 10, 12, tzinfo=tz)  # a Monday
    events = await calendar.async_get_events(hass, start, start + timedelta(days=14))

    weeks = {ev.start.isocalendar().week for ev in events}
    assert len(weeks) == 2
    assert len(events) == 4

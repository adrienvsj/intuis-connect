"""Cost entity migration and state shared between several homes."""
from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from homeassistant.helpers import entity_registry as er

from custom_components.intuis_connect import _async_migrate_cost_entity_ids
from custom_components.intuis_connect.hourly_stats import HourlyStatsUpdater
from custom_components.intuis_connect.utils.const import DOMAIN

HOME_ID = "home_1"


def _home(names: dict[str, str]) -> MagicMock:
    home = MagicMock()
    home.id = HOME_ID
    rooms = {}
    for room_id, name in names.items():
        room = MagicMock()
        room.name = name
        rooms[room_id] = room
    home.rooms = rooms
    return home


async def test_cost_migration_uses_registry_only(hass):
    """Accented names are slugified, user ids are kept, foreign entities untouched."""
    reg = er.async_get(hass)
    old = lambda room_id: f"intuis_{HOME_ID}_{room_id}_energy_cost"
    reg.async_get_or_create("sensor", DOMAIN, old("r1"), suggested_object_id="sejour_energy_cost")
    reg.async_get_or_create("sensor", DOMAIN, old("r2"), suggested_object_id="my_custom_cost")
    reg.async_get_or_create("sensor", DOMAIN, old("r3"), suggested_object_id="chambre_energy_cost")
    foreign = reg.async_get_or_create("sensor", "template", "tpl", suggested_object_id="chambre_cost")

    _async_migrate_cost_entity_ids(hass, _home({"r1": "Séjour", "r2": "Bureau", "r3": "Chambre"}))

    new = lambda room_id: reg.async_get_entity_id("sensor", DOMAIN, f"intuis_{HOME_ID}_{room_id}_cost")
    assert new("r1") == "sensor.sejour_cost"
    assert new("r2") == "sensor.my_custom_cost"  # user-chosen id kept
    assert new("r3") == "sensor.chambre_energy_cost"  # target taken by another integration
    assert reg.async_get(foreign.entity_id) is not None

    # Running again changes nothing
    _async_migrate_cost_entity_ids(hass, _home({"r1": "Séjour", "r2": "Bureau", "r3": "Chambre"}))
    assert new("r1") == "sensor.sejour_cost"


async def test_two_homes_publish_without_overwriting(hass):
    hass.data[DOMAIN] = {}
    updaters = []
    for home_id, sid in (("home_a", "sensor.a_energy"), ("home_b", "sensor.b_energy")):
        home = MagicMock()
        home.id = home_id
        home.rooms = {}
        updater = HourlyStatsUpdater(hass, MagicMock(), home, f"entry_{home_id}", home_id)
        today = datetime.now(ZoneInfo("Europe/Paris")).strftime("%Y-%m-%d")
        updater._data = {"midnight_bases": {sid: {today: 1.0}}, "anchors": {}}
        updater._loaded = True
        updaters.append(updater)

    for updater in updaters:
        updater._publish_sensor_base()

    assert set(hass.data[DOMAIN]["sensor_base"]) == {"sensor.a_energy", "sensor.b_energy"}

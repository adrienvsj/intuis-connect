"""Robustness tests for the coordinator data path and the climate entity."""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.components.climate import HVACMode
from homeassistant.core import is_callback

from custom_components.intuis_connect.entity.intuis_home_config import IntuisHomeConfig
from custom_components.intuis_connect.entity.intuis_room import (
    IntuisRoom,
    IntuisRoomDefinition,
)
from custom_components.intuis_connect.intuis_api.api import APIError
from custom_components.intuis_connect.intuis_api.mapper import extract_rooms
from custom_components.intuis_connect.intuis_data import IntuisData


def _definition(room_id: str = "room_1") -> IntuisRoomDefinition:
    definition = MagicMock(spec=IntuisRoomDefinition)
    definition.id = room_id
    definition.name = "Room"
    definition.module_ids = []
    return definition


def test_empty_overrides_dict_is_shared(mock_api, mock_intuis_home):
    """Climate entities and the coordinator must work on the same dict, even when empty."""
    shared: dict = {}
    data = IntuisData(api=mock_api, intuis_home=mock_intuis_home, overrides=shared)
    shared["room_1"] = {"mode": "manual"}
    assert data._overrides is shared


async def test_config_failure_keeps_room_data(mock_api, mock_intuis_home, sample_room_data):
    """A failing config call keeps the last config instead of failing the update."""
    data = IntuisData(api=mock_api, intuis_home=mock_intuis_home, overrides={})
    good = {"home_id": "home_123", "modules": [{"id": "gw_1"}]}
    mock_api.async_get_config = AsyncMock(side_effect=[good, APIError("boom")])

    with patch(
        "custom_components.intuis_connect.intuis_data.extract_rooms",
        return_value=sample_room_data,
    ), patch(
        "custom_components.intuis_connect.intuis_data.extract_modules",
        return_value=[],
    ):
        first = await data.async_update()
        second = await data.async_update()

    assert second["rooms"] == sample_room_data
    assert second["home_config"] is first["home_config"]
    assert second["home_config"].module_id == "gw_1"


def test_config_with_several_gateways_is_parsed():
    """Homes with more than one gateway must not fail every update."""
    config = IntuisHomeConfig.from_dict(
        {"home_id": "h", "modules": [{"id": "gw_1"}, {"id": "gw_2"}]}
    )
    assert config.module_id == "gw_1"


def test_unknown_room_is_skipped():
    """A room missing from the home definition is ignored, the others still load."""
    home = {
        "rooms": [
            {"id": "room_1", "therm_setpoint_mode": "home"},
            {"id": "room_new", "therm_setpoint_mode": "home"},
        ]
    }
    rooms = extract_rooms(home, [], {}, {"room_1": _definition()}, None)
    assert list(rooms) == ["room_1"]


def test_null_temperatures_do_not_crash():
    """An explicit null setpoint or measurement is kept as None."""
    room = IntuisRoom.from_dict(
        _definition(),
        {
            "id": "room_1",
            "therm_setpoint_mode": "home",
            "therm_setpoint_temperature": None,
            "therm_measured_temperature": None,
        },
        [],
    )
    assert room.target_temperature is None
    assert room.temperature is None
    assert room.heating is False


def test_climate_unavailable_when_room_missing(climate_entity_factory, mock_coordinator):
    """The climate entity reports unavailable instead of raising."""
    entity = climate_entity_factory()
    mock_coordinator.data["rooms"] = {}
    assert entity.available is False
    assert entity.current_temperature is None
    assert entity.target_temperature is None
    assert entity.hvac_mode is None


def test_optimistic_mode_released_after_hold(climate_entity_factory, sample_room):
    """A mode set from HA must not hide later changes made outside HA."""
    entity = climate_entity_factory()
    entity._attr_hvac_mode = HVACMode.OFF
    entity._hold_optimistic_state()

    # The refresh requested right after the command keeps the optimistic value.
    entity._handle_coordinator_update()
    assert entity.hvac_mode == HVACMode.OFF

    # Once the hold is over, the cloud state wins again.
    sample_room.mode = "home"
    with patch(
        "custom_components.intuis_connect.climate.time.monotonic",
        return_value=time.monotonic() + 3600,
    ):
        entity._handle_coordinator_update()
    assert entity.hvac_mode == HVACMode.AUTO


def test_end_refresh_timer_is_a_callback_and_replaced(climate_entity_factory):
    """The end-of-override refresh runs in the event loop and only the latest is kept."""
    entity = climate_entity_factory()
    first_cancel, second_cancel = MagicMock(), MagicMock()
    with patch(
        "custom_components.intuis_connect.climate.async_call_later",
        side_effect=[first_cancel, second_cancel],
    ) as call_later:
        entity._schedule_end_refresh(int(time.time()) + 60)
        entity._schedule_end_refresh(int(time.time()) + 120)

    assert is_callback(call_later.call_args.args[2])
    first_cancel.assert_called_once()
    second_cancel.assert_not_called()

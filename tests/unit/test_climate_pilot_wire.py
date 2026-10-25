"""Tests for pilot-wire (FP4) rooms, which take orders instead of temperatures."""
from __future__ import annotations

import pytest

from homeassistant.components.climate import ClimateEntityFeature, HVACMode

from custom_components.intuis_connect.climate import IntuisPilotWireClimate
from custom_components.intuis_connect.entity.intuis_room import is_pilot_wire
from custom_components.intuis_connect.utils.const import (
    API_FP_COMFORT,
    API_FP_ECO,
    API_FP_FROST,
    API_MODE_AWAY,
    API_MODE_HG,
    API_MODE_HOME,
    API_MODE_MANUAL,
    API_MODE_OFF,
    PILOT_WIRE_PRESETS,
    PRESET_COMFORT,
    PRESET_ECO,
    PRESET_PILOT_WIRE_FROST,
)


@pytest.fixture
def pilot_room(sample_room):
    sample_room.muller_type = "FP4"
    sample_room.mode = API_MODE_HOME
    sample_room.therm_setpoint_fp = API_FP_COMFORT
    return sample_room


@pytest.fixture
def pilot_entity(climate_entity_factory, pilot_room, default_options):
    def _make(overrides: dict | None = None):
        return climate_entity_factory(
            room=pilot_room, options=default_options, overrides=overrides, entity_cls=IntuisPilotWireClimate
        )
    return _make


@pytest.mark.parametrize(("muller_type", "expected"), [("FP4", True), ("", False), (None, False), ("NMH", False)])
def test_is_pilot_wire(muller_type, expected):
    assert is_pilot_wire(muller_type) is expected


class TestPilotWireState:

    def test_no_temperature_dial(self, pilot_entity):
        entity = pilot_entity()
        assert entity.supported_features == ClimateEntityFeature.PRESET_MODE
        assert entity.current_temperature is None
        assert entity.target_temperature is None
        assert entity.preset_modes == PILOT_WIRE_PRESETS

    @pytest.mark.parametrize(
        ("mode", "fp", "preset", "hvac"),
        [
            (API_MODE_HOME, API_FP_COMFORT, PRESET_COMFORT, HVACMode.AUTO),
            (API_MODE_HOME, API_FP_ECO, PRESET_ECO, HVACMode.AUTO),
            (API_MODE_MANUAL, API_FP_ECO, PRESET_ECO, HVACMode.HEAT),
            (API_MODE_HG, API_FP_FROST, PRESET_PILOT_WIRE_FROST, HVACMode.HEAT),
            (API_MODE_OFF, API_FP_COMFORT, None, HVACMode.OFF),
            (API_MODE_AWAY, API_FP_ECO, PRESET_ECO, HVACMode.AUTO),
        ],
    )
    def test_state_follows_live_order(self, pilot_entity, pilot_room, mode, fp, preset, hvac):
        pilot_room.mode = mode
        pilot_room.therm_setpoint_fp = fp
        entity = pilot_entity()
        assert entity.preset_mode == preset
        assert entity.hvac_mode == hvac

    def test_attributes_expose_order_and_source(self, pilot_entity, pilot_room):
        assert pilot_entity().extra_state_attributes == {
            "pilot_wire_order": API_FP_COMFORT,
            "setpoint_source": "schedule",
        }
        pilot_room.mode = API_MODE_MANUAL
        assert pilot_entity().extra_state_attributes["setpoint_source"] == "manual"


    def test_missing_room_reports_nothing(self, pilot_entity, mock_coordinator):
        entity = pilot_entity()
        mock_coordinator.data["rooms"] = {}
        assert entity.hvac_mode is None
        assert entity.preset_mode is None
        assert entity.extra_state_attributes is None


class TestPilotWireCommands:

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("preset", "fp"), [(PRESET_ECO, API_FP_ECO), (PRESET_COMFORT, API_FP_COMFORT)])
    async def test_order_preset_sends_manual_fp(self, pilot_entity, mock_api, preset, fp):
        overrides = {}
        entity = pilot_entity(overrides)

        await entity.async_set_preset_mode(preset)

        mock_api.async_set_room_state.assert_called_once_with("room_123", API_MODE_MANUAL, duration=5, fp=fp)
        assert overrides["room_123"]["fp"] == fp
        assert overrides["room_123"]["temp"] is None

    @pytest.mark.asyncio
    async def test_frost_preset_uses_hg(self, pilot_entity, mock_api):
        await pilot_entity().async_set_preset_mode(PRESET_PILOT_WIRE_FROST)

        assert mock_api.async_set_room_state.call_args.args[:2] == ("room_123", API_MODE_HG)

    @pytest.mark.asyncio
    async def test_heat_mode_means_manual_comfort(self, pilot_entity, mock_api):
        await pilot_entity().async_set_hvac_mode(HVACMode.HEAT)

        mock_api.async_set_room_state.assert_called_once_with(
            "room_123", API_MODE_MANUAL, duration=5, fp=API_FP_COMFORT
        )

    @pytest.mark.asyncio
    async def test_auto_mode_returns_to_schedule(self, pilot_entity, mock_api):
        await pilot_entity().async_set_hvac_mode(HVACMode.AUTO)

        mock_api.async_set_room_state.assert_called_once_with("room_123", API_MODE_HOME)

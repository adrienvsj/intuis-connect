"""Climate platform for Intuis Connect."""
from __future__ import annotations

import logging
import time
from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.typing import StateType
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .entity.intuis_entity import IntuisDataUpdateCoordinator, IntuisEntity
from .entity.intuis_room import IntuisRoom, is_pilot_wire
from .intuis_api.api import IntuisAPI
from .utils.const import (
    API_FP_COMFORT,
    API_FP_ECO,
    API_FP_FROST,
    API_MODE_AUTO,
    API_MODE_AWAY,
    API_MODE_BOOST,
    API_MODE_HG,
    API_MODE_HOME,
    API_MODE_MANUAL,
    API_MODE_OFF,
    CONF_AWAY_DURATION,
    CONF_AWAY_TEMP,
    CONF_BOOST_DURATION,
    CONF_BOOST_TEMP,
    CONF_FROST_PROTECT_DURATION,
    CONF_FROST_PROTECT_TEMP,
    CONF_MANUAL_DURATION,
    DEFAULT_AWAY_DURATION,
    DEFAULT_AWAY_TEMP,
    DEFAULT_BOOST_DURATION,
    DEFAULT_BOOST_TEMP,
    DEFAULT_FROST_PROTECT_DURATION,
    DEFAULT_FROST_PROTECT_TEMP,
    DEFAULT_MANUAL_DURATION,
    DOMAIN,
    PILOT_WIRE_PRESETS,
    PRESET_AWAY,
    PRESET_BOOST,
    PRESET_COMFORT,
    PRESET_ECO,
    PRESET_FROST_PROTECT,
    PRESET_PILOT_WIRE_FROST,
    PRESET_SCHEDULE,
)
from .utils.helper import get_basic_utils

_LOGGER = logging.getLogger(__name__)

_PRESET_TO_FP = {PRESET_COMFORT: API_FP_COMFORT, PRESET_ECO: API_FP_ECO}

# How long a value set from Home Assistant is shown before the cloud data takes
# over again: it covers the refresh requested right after the command, which can
# still return the previous state.
OPTIMISTIC_HOLD_SECONDS = 30
_FP_TO_PRESET = {API_FP_COMFORT: PRESET_COMFORT, API_FP_ECO: PRESET_ECO, API_FP_FROST: PRESET_PILOT_WIRE_FROST}


class IntuisConnectClimate(
    CoordinatorEntity[IntuisDataUpdateCoordinator], ClimateEntity, IntuisEntity
):
    """Climate entity for an Intuis Connect-compatible device."""

    _attr_hvac_modes = [HVACMode.AUTO, HVACMode.HEAT, HVACMode.OFF]
    _attr_preset_modes = [PRESET_AWAY, PRESET_BOOST, PRESET_SCHEDULE, PRESET_FROST_PROTECT]
    _attr_supported_features = (
            ClimateEntityFeature.TARGET_TEMPERATURE | ClimateEntityFeature.PRESET_MODE
    )
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_has_entity_name = True

    def __init__(
            self,
            coordinator: IntuisDataUpdateCoordinator,
            home_id: str,
            room: IntuisRoom,
            api: IntuisAPI,
            entry_id: str,
    ) -> None:
        """Initialize the climate entity."""
        CoordinatorEntity.__init__(self, coordinator)
        ClimateEntity.__init__(self)
        IntuisEntity.__init__(self, coordinator, room, home_id, f"{room.name} Climate", "climate")
        self._home_id = home_id
        self._api = api
        self._entry_id = entry_id
        # We have real state from the coordinator, not assumed state
        self._attr_assumed_state = False
        self._attr_hvac_mode: HVACMode | None = None
        self._attr_preset_mode: str | None = None
        self._attr_target_temperature: float | None = None
        self._optimistic_until = 0.0
        self._cancel_end_refresh: CALLBACK_TYPE | None = None

    @property
    def available(self) -> bool:
        """Unavailable while the room is missing from the latest data."""
        return super().available and self._get_room() is not None

    def _hold_optimistic_state(self) -> None:
        """Keep the values just set until fresh data can reflect them."""
        self._optimistic_until = time.monotonic() + OPTIMISTIC_HOLD_SECONDS

    @callback
    def _handle_coordinator_update(self) -> None:
        """Let fresh data replace the values set from Home Assistant."""
        if time.monotonic() >= self._optimistic_until:
            self._attr_hvac_mode = None
            self._attr_preset_mode = None
            self._attr_target_temperature = None
        super()._handle_coordinator_update()

    async def async_will_remove_from_hass(self) -> None:
        """Cancel the pending end-of-override refresh."""
        if self._cancel_end_refresh:
            self._cancel_end_refresh()
            self._cancel_end_refresh = None
        await super().async_will_remove_from_hass()

    def _get_overrides(self) -> dict[str, dict]:
        data = self.hass.data.get(DOMAIN, {}).get(self._entry_id, {})
        return data.get("overrides", {})
    
    def _get_intuis_home(self):
        """Get the IntuisHome object from coordinator data."""
        return self.coordinator.data.get("intuis_home")

    def _get_save_overrides(self):
        """Get the save_overrides callback."""
        data = self.hass.data.get(DOMAIN, {}).get(self._entry_id, {})
        return data.get("save_overrides")

    def _get_option(self, key: str, default: Any) -> Any:
        """Get an option value from the config entry."""
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry:
            return entry.options.get(key, default)
        return default

    def _schedule_end_refresh(self, end_ts: int) -> None:
        """Refresh slightly after the override ends (only the latest override counts)."""
        if self._cancel_end_refresh:
            self._cancel_end_refresh()
        delay = max(0, end_ts - int(time.time()) + 1)

        @callback
        def _cb(_now) -> None:
            self._cancel_end_refresh = None
            self.hass.async_create_task(self.coordinator.async_request_refresh())

        self._cancel_end_refresh = async_call_later(self.hass, delay, _cb)

    @property
    def device_info(self) -> DeviceInfo:
        """Return the device info."""
        return self._attr_device_info

    @property
    def current_temperature(self) -> StateType:
        """Return the current temperature."""
        room = self._get_room()
        return room.temperature if room else None

    @property
    def target_temperature(self) -> StateType:
        """Return the target temperature."""
        room = self._get_room()
        return room.target_temperature if room else None

    @property
    def hvac_mode(self) -> HVACMode | None:
        """Return hvac operation ie. heat, cool mode."""
        if self._attr_hvac_mode is not None:
            return self._attr_hvac_mode
        
        # Check if boost is active first (detected via boost_status, not therm_setpoint_mode)
        if self._is_boost_active():
            return HVACMode.HEAT

        room = self._get_room()
        if room is None:
            return None
        mode = room.mode
        if mode == API_MODE_OFF:
            return HVACMode.OFF
        if mode in (API_MODE_AUTO, API_MODE_HOME, API_MODE_HG):
            return HVACMode.AUTO
        if mode in (API_MODE_MANUAL, API_MODE_BOOST, API_MODE_AWAY):
            return HVACMode.HEAT
        _LOGGER.warning("Unhandled HVAC mode: %s", mode)
        return HVACMode.HEAT

    @property
    def preset_mode(self) -> str | None:
        """Return the current preset mode."""
        if self._attr_preset_mode is not None:
            return self._attr_preset_mode
        
        room = self._get_room()
        if not room:
            return None
        
        room_id = room.id
        
        # First, check overrides to see if away/boost mode is active
        # This is more reliable than therm_setpoint_mode which only returns "auto" or "manual"
        # Overrides are set when we programmatically set away/boost modes
        overrides = self._get_overrides()
        override = overrides.get(room_id)
        
        if override:
            override_mode = override.get("mode")
            if override_mode == API_MODE_AWAY:
                return PRESET_AWAY
            if override_mode == API_MODE_BOOST:
                return PRESET_BOOST
            if override_mode == API_MODE_HG:
                return PRESET_FROST_PROTECT
            # If override is manual, it's not a preset (user-set temperature)
            if override_mode == API_MODE_MANUAL:
                return None
        
        # Check room's boost_status (from API homestatus)
        # boost_status indicates "in_progress" when boost is active, "disabled" otherwise
        # This is the primary method to detect boost mode
        if room.boost_status == "in_progress":
            return PRESET_BOOST
        
        # Fallback: check therm_setpoint_mode (though it may not have away/boost)
        # This often only returns "auto" or "manual", not "away" or "boost"
        # But it can also return "hg" for Hors-Gel (Frost Protection) mode
        # Note: Some installations may return "boost" in therm_setpoint_mode for compatibility
        mode = room.mode
        if mode == API_MODE_AWAY:
            return PRESET_AWAY
        if mode == API_MODE_BOOST:
            # Fallback for installations that return boost in therm_setpoint_mode
            return PRESET_BOOST
        if mode == API_MODE_HG:
            return PRESET_FROST_PROTECT
        
        # If in AUTO mode, return schedule preset
        return PRESET_SCHEDULE if self.hvac_mode == HVACMode.AUTO else None

    def _is_boost_active(self) -> bool:
        """Check if boost preset is currently active."""
        room = self._get_room()
        if not room:
            return False
        
        # Check if boost_status indicates boost is in progress
        if room.boost_status == "in_progress":
            return True
        
        # Check overrides
        room_id = room.id
        overrides = self._get_overrides()
        override = overrides.get(room_id)
        if override and override.get("mode") == API_MODE_BOOST:
            return True
        
        # Check therm_setpoint_mode as fallback
        if room.mode == API_MODE_BOOST:
            return True
        
        return False

    @property
    def hvac_action(self) -> HVACAction | None:
        """Return the current running hvac operation."""
        if self.hvac_mode == HVACMode.OFF:
            return HVACAction.OFF
        
        room = self._get_room()
        if not room:
            return HVACAction.IDLE
        
        # If boost is active, always return HEATING
        if self._is_boost_active():
            return HVACAction.HEATING
        
        # Otherwise, use the room's heating status
        return HVACAction.HEATING if room.heating else HVACAction.IDLE

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set new target temperature."""
        temp = kwargs.get("temperature")
        if temp is None:
            return
        await self._async_apply_manual(self._room.id, temp=float(temp))
        self._attr_target_temperature = temp
        self._attr_hvac_mode = HVACMode.HEAT
        self._attr_preset_mode = None
        self._hold_optimistic_state()
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()

    async def _async_apply_manual(self, room_id: str, temp: float | None = None, fp: str | None = None) -> None:
        """Send a manual setpoint (temperature or pilot-wire order) and keep it as a sticky override."""
        manual_duration = self._get_option(CONF_MANUAL_DURATION, DEFAULT_MANUAL_DURATION)
        if fp is None:
            await self._api.async_set_room_state(room_id, API_MODE_MANUAL, temp, manual_duration)
        else:
            await self._api.async_set_room_state(room_id, API_MODE_MANUAL, duration=manual_duration, fp=fp)
        now_ts = int(time.time())
        end_ts = now_ts + manual_duration * 60
        override = {"mode": API_MODE_MANUAL, "temp": temp, "end": end_ts, "sticky": True, "last_reapply": now_ts}
        if fp is not None:
            override["fp"] = fp
        self._get_overrides()[room_id] = override
        save_overrides = self._get_save_overrides()
        if save_overrides:
            await save_overrides()
        self._schedule_end_refresh(end_ts)

    async def _async_apply_frost_protect(self, room_id: str) -> None:
        """Send frost protection (hg) and keep it as a sticky override."""
        frost_protect_temp = self._get_option(CONF_FROST_PROTECT_TEMP, DEFAULT_FROST_PROTECT_TEMP)
        frost_protect_duration = self._get_option(CONF_FROST_PROTECT_DURATION, DEFAULT_FROST_PROTECT_DURATION)
        await self._api.async_set_room_state(room_id, API_MODE_HG, frost_protect_temp, frost_protect_duration)
        now_ts = int(time.time())
        end_ts = now_ts + frost_protect_duration * 60
        self._get_overrides()[room_id] = {
            "mode": API_MODE_HG,
            "temp": float(frost_protect_temp),
            "end": end_ts,
            "sticky": True,
            "last_reapply": now_ts,
        }
        save_overrides = self._get_save_overrides()
        if save_overrides:
            await save_overrides()
        self._schedule_end_refresh(end_ts)

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set new hvac mode."""
        room_id = self._room.id
        overrides = self._get_overrides()
        overrides_changed = False

        if hvac_mode == HVACMode.OFF:
            await self._api.async_set_room_state(room_id, API_MODE_OFF)
            if room_id in overrides:
                overrides.pop(room_id, None)
                overrides_changed = True
            self._attr_hvac_mode = HVACMode.OFF
            self._attr_preset_mode = None
        elif hvac_mode == HVACMode.AUTO:
            await self._api.async_set_room_state(room_id, API_MODE_HOME)
            if room_id in overrides:
                overrides.pop(room_id, None)
                overrides_changed = True
            self._attr_hvac_mode = HVACMode.AUTO
            self._attr_preset_mode = PRESET_SCHEDULE
        elif hvac_mode == HVACMode.HEAT:
            await self._async_apply_manual(room_id, temp=float(self.target_temperature or 20.0))
            self._attr_hvac_mode = HVACMode.HEAT
            self._attr_preset_mode = None

        # Persist overrides to storage
        if overrides_changed:
            save_overrides = self._get_save_overrides()
            if save_overrides:
                await save_overrides()

        self._hold_optimistic_state()
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Set new preset mode."""
        room_id = self._room.id
        overrides = self._get_overrides()
        overrides_changed = False

        if preset_mode == PRESET_SCHEDULE:
            await self._api.async_set_room_state(room_id, API_MODE_HOME)
            self._attr_hvac_mode = HVACMode.AUTO
            if room_id in overrides:
                overrides.pop(room_id, None)
                overrides_changed = True
        elif preset_mode == PRESET_AWAY:
            away_temp = self._get_option(CONF_AWAY_TEMP, DEFAULT_AWAY_TEMP)
            away_duration = self._get_option(CONF_AWAY_DURATION, DEFAULT_AWAY_DURATION)
            await self._api.async_set_room_state(
                room_id,
                API_MODE_AWAY,
                away_temp,
                away_duration,
            )
            self._attr_hvac_mode = HVACMode.HEAT
            now_ts = int(time.time())
            end_ts = now_ts + away_duration * 60
            overrides[room_id] = {
                "mode": API_MODE_AWAY,
                "temp": float(away_temp),
                "end": end_ts,
                "sticky": True,
                "last_reapply": now_ts,
            }
            overrides_changed = True
            self._schedule_end_refresh(end_ts)
        elif preset_mode == PRESET_BOOST:
            boost_temp = self._get_option(CONF_BOOST_TEMP, DEFAULT_BOOST_TEMP)
            boost_duration = self._get_option(CONF_BOOST_DURATION, DEFAULT_BOOST_DURATION)
            await self._api.async_set_room_state(
                room_id,
                API_MODE_BOOST,
                boost_temp,
                boost_duration,
            )
            self._attr_hvac_mode = HVACMode.HEAT
            now_ts = int(time.time())
            end_ts = now_ts + boost_duration * 60
            overrides[room_id] = {
                "mode": API_MODE_BOOST,
                "temp": float(boost_temp),
                "end": end_ts,
                "sticky": True,
                "last_reapply": now_ts,
            }
            overrides_changed = True
            self._schedule_end_refresh(end_ts)
        elif preset_mode == PRESET_FROST_PROTECT:
            await self._async_apply_frost_protect(room_id)
            self._attr_hvac_mode = HVACMode.AUTO

        # Persist overrides to storage
        if overrides_changed:
            save_overrides = self._get_save_overrides()
            if save_overrides:
                await save_overrides()

        self._attr_preset_mode = preset_mode
        self._hold_optimistic_state()
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()


class IntuisPilotWireClimate(IntuisConnectClimate):
    """Pilot-wire radiator: driven by orders (comfort/eco/frost) and read live from the room."""

    _attr_preset_modes = PILOT_WIRE_PRESETS
    _attr_supported_features = ClimateEntityFeature.PRESET_MODE
    _attr_translation_key = "intuis_room"

    @property
    def current_temperature(self) -> StateType:
        """The API reports no measured temperature for pilot-wire rooms."""
        return None

    @property
    def target_temperature(self) -> StateType:
        """Pilot-wire rooms have orders, not temperature setpoints."""
        return None

    @property
    def hvac_mode(self) -> HVACMode | None:
        """Return AUTO when the order comes from the home (schedule or away), HEAT for a manual order."""
        room = self._get_room()
        if room is None:
            return None
        if room.mode == API_MODE_OFF:
            return HVACMode.OFF
        return HVACMode.AUTO if room.mode in (API_MODE_HOME, API_MODE_AUTO, API_MODE_AWAY) else HVACMode.HEAT

    @property
    def preset_mode(self) -> str | None:
        """Return the active pilot-wire order as a preset."""
        room = self._get_room()
        if room is None or room.mode == API_MODE_OFF:
            return None
        if room.mode == API_MODE_HG:
            return PRESET_PILOT_WIRE_FROST
        return _FP_TO_PRESET.get(room.therm_setpoint_fp)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose the raw order and whether it comes from the schedule."""
        room = self._get_room()
        if room is None:
            return None
        return {
            "pilot_wire_order": room.therm_setpoint_fp,
            "setpoint_source": "schedule" if self.hvac_mode == HVACMode.AUTO else "manual",
        }

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """HEAT means a manual Comfort order; other modes behave as for temperature rooms."""
        if hvac_mode == HVACMode.HEAT:
            await self._async_apply_manual(self._room.id, fp=API_FP_COMFORT)
            await self.coordinator.async_request_refresh()
        else:
            await super().async_set_hvac_mode(hvac_mode)

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Send the pilot-wire order matching the preset."""
        room_id = self._room.id
        if preset_mode == PRESET_PILOT_WIRE_FROST:
            await self._async_apply_frost_protect(room_id)
        else:
            await self._async_apply_manual(room_id, fp=_PRESET_TO_FP[preset_mode])
        await self.coordinator.async_request_refresh()


async def async_setup_entry(
        hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the climate entities."""
    coordinator, home_id, rooms, api = get_basic_utils(hass, entry)

    entities = []
    for room_id in rooms:
        room = rooms.get(room_id)
        climate_cls = (
            IntuisPilotWireClimate
            if is_pilot_wire(room.muller_type, room.temperature, room.target_temperature)
            else IntuisConnectClimate
        )
        entities.append(climate_cls(coordinator, home_id, room, api, entry.entry_id))
    async_add_entities(entities, update_before_add=True)

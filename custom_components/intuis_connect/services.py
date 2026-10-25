"""Service handlers for Intuis Connect integration.

This module contains all service registration and handling logic,
including dynamic services.yaml generation for the Home Assistant UI.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from functools import partial, wraps
from typing import TYPE_CHECKING

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
    TimeSelector,
    TimeSelectorConfig,
)

from .entity.intuis_home import IntuisHome
from .entity.intuis_schedule import (
    IntuisThermSchedule,
    IntuisThermZone,
    IntuisTimetable,
)
from .history_import import (
    DEFAULT_HISTORY_DAYS,
    MAX_HISTORY_DAYS,
    HistoryImportManager,
    async_import_energy_history,
)
from .intuis_api.api import (
    APIError,
    CannotConnect,
    IntuisAPI,
    InvalidAuth,
    RateLimitError,
)
from .timetable import (
    DAYS_OF_WEEK,
    DAYS_OF_WEEK_LABELS,
    MINUTES_PER_DAY,
    apply_slot,
    remove_consecutive_duplicates,
)
from .utils.const import DOMAIN

if TYPE_CHECKING:
    pass

_LOGGER = logging.getLogger(__name__)

# Service names
SERVICE_SWITCH_SCHEDULE = "switch_schedule"
SERVICE_REFRESH_SCHEDULES = "refresh_schedules"
SERVICE_SET_SCHEDULE_SLOT = "set_schedule_slot"
SERVICE_SET_ZONE_TEMPERATURE = "set_zone_temperature"
SERVICE_IMPORT_ENERGY_HISTORY = "import_energy_history"
SERVICE_SYNC_SCHEDULE = "sync_schedule"
SERVICE_RECALCULATE_COST_HISTORY = "recalculate_cost_history"

# Service attributes
ATTR_SCHEDULE_NAME = "schedule_name"
ATTR_SCHEDULE_ID = "schedule_id"
ATTR_HOME_ID = "home_id"
ATTR_DAY = "day"  # Legacy, kept for backward compatibility
ATTR_START_DAY = "start_day"
ATTR_END_DAY = "end_day"
ATTR_START_TIME = "start_time"
ATTR_END_TIME = "end_time"
ATTR_ZONE_NAME = "zone_name"
ATTR_ROOM_NAME = "room_name"
ATTR_TEMPERATURE = "temperature"
ATTR_DAYS = "days"
ATTR_GRANULARITY = "granularity"
ATTR_CLEAR_EXISTING = "clear_existing"
ATTR_TIMETABLE = "timetable"
ATTR_AWAY_TEMP = "away_temp"
ATTR_HG_TEMP = "hg_temp"

# Base service schemas (dynamic schemas are built in async_register_services)
REFRESH_SCHEDULES_SCHEMA = vol.Schema({
    vol.Optional(ATTR_HOME_ID): str,  # Target specific home (multi-home support)
})


def _with_api_errors(
    handler: Callable[[ServiceCall], Awaitable[None]],
) -> Callable[[ServiceCall], Awaitable[None]]:
    """Report Intuis API errors to the caller as translated Home Assistant errors."""

    @wraps(handler)
    async def wrapper(call: ServiceCall) -> None:
        try:
            await handler(call)
        except InvalidAuth as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="auth_failed"
            ) from err
        except (APIError, CannotConnect, RateLimitError) as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="api_error",
                translation_placeholders={"error": str(err)},
            ) from err

    return wrapper


async def async_register_services(hass: HomeAssistant) -> None:
    """Register the Intuis Connect services (once, for every configured home)."""

    def _get_target_entries(target_home_id: str | None) -> list[tuple[str, dict]]:
        """Get config entries matching the target home_id.

        Args:
            target_home_id: Optional home ID to filter by.

        Returns:
            List of (entry_id, data) tuples for matching entries.
        """
        entries = []
        for entry_id, data in list(hass.data.get(DOMAIN, {}).items()):
            if not isinstance(data, dict):
                continue
            intuis_home: IntuisHome = data.get("intuis_home")
            if not intuis_home:
                continue
            if target_home_id and intuis_home.id != target_home_id:
                continue
            entries.append((entry_id, data))
        return entries

    async def async_handle_switch_schedule(call: ServiceCall) -> None:
        """Handle switch_schedule service call."""
        schedule_name = call.data.get(ATTR_SCHEDULE_NAME)
        target_home_id = call.data.get(ATTR_HOME_ID)

        if not schedule_name:
            raise ServiceValidationError("schedule_name must be provided")

        entries = _get_target_entries(target_home_id)

        if not entries:
            if target_home_id:
                raise ServiceValidationError(f"Home with ID {target_home_id} not found")
            raise ServiceValidationError("No configured homes found")

        # Warn if multiple homes and no home_id specified
        if len(entries) > 1 and not target_home_id:
            home_names = [d.get("intuis_home").name for _, d in entries if d.get("intuis_home")]
            _LOGGER.warning(
                "Multiple homes configured (%s) but no home_id specified. "
                "Using first home. Add 'home_id' parameter to target a specific home.",
                ", ".join(home_names),
            )

        # Process first matching entry
        entry_id, data = entries[0]
        api: IntuisAPI = data.get("api")
        intuis_home: IntuisHome = data.get("intuis_home")
        coordinator = data.get("coordinator")

        if not api or not intuis_home:
            raise ServiceValidationError("API or home data not available")

        # Find the schedule by name
        target_schedule = None
        available_schedules = []
        for schedule in intuis_home.schedules:
            if isinstance(schedule, IntuisThermSchedule):
                available_schedules.append(schedule.name)
                if schedule.name == schedule_name:
                    target_schedule = schedule
                    break

        if not target_schedule:
            raise ServiceValidationError(f"Schedule not found: {schedule_name}. Available schedules: {available_schedules}")

        _LOGGER.info(
            "Switching to schedule: %s (ID: %s) for home %s",
            target_schedule.name,
            target_schedule.id,
            intuis_home.name or intuis_home.id,
        )

        try:
            await api.async_switch_schedule(intuis_home.id, target_schedule.id)
            # Update local state
            for s in intuis_home.schedules:
                if isinstance(s, IntuisThermSchedule):
                    s.selected = (s.id == target_schedule.id)
            # Refresh coordinator
            if coordinator:
                await coordinator.async_request_refresh()
        except (APIError, CannotConnect, RateLimitError) as err:
            _LOGGER.debug("Failed to switch schedule: %s", err)
            raise

    async def async_handle_refresh_schedules(call: ServiceCall) -> None:
        """Handle refresh_schedules service call.

        This fetches fresh schedule data from the Intuis API and updates
        the local state.

        Optional home_id parameter targets a specific home (for multi-home setups).
        If not provided, refreshes all configured homes.
        """
        target_home_id = call.data.get(ATTR_HOME_ID)
        refreshed_count = 0

        for entry_id, data in list(hass.data.get(DOMAIN, {}).items()):
            if not isinstance(data, dict):
                continue

            api: IntuisAPI = data.get("api")
            intuis_home: IntuisHome = data.get("intuis_home")
            coordinator = data.get("coordinator")

            if not api or not intuis_home:
                continue

            # Filter by home_id if specified
            if target_home_id and intuis_home.id != target_home_id:
                continue

            _LOGGER.info(
                "Fetching fresh schedule data from Intuis API for home: %s",
                intuis_home.name or intuis_home.id
            )

            try:
                # Fetch fresh homes data (includes schedules) from the API
                fresh_home = await api.async_get_homes_data()

                # Update local schedules from fresh data
                intuis_home.schedules = fresh_home.schedules

                _LOGGER.info(
                    "Refreshed %d schedules for home %s",
                    len(fresh_home.schedules),
                    intuis_home.name or intuis_home.id,
                )

                # Also trigger coordinator refresh for room status
                if coordinator:
                    await coordinator.async_request_refresh()

                refreshed_count += 1

            except (APIError, CannotConnect, RateLimitError) as err:
                _LOGGER.debug(
                    "Failed to refresh schedules for home %s: %s",
                    intuis_home.name or intuis_home.id,
                    err,
                )
                raise

        if refreshed_count == 0 and target_home_id:
            _LOGGER.warning("Home with ID %s not found", target_home_id)
        else:
            _LOGGER.info("Refreshed schedules for %d home(s)", refreshed_count)

    async def async_handle_set_schedule_slot(call: ServiceCall) -> None:
        """Handle set_schedule_slot service call.

        Sets a zone for a specific time range in a schedule.
        Supports multi-day spans with start_day and end_day parameters.
        Creates two timetable entries: one at start_time with the target zone,
        and one at end_time to restore the previous zone.
        
        If schedule_name is provided, modifies that specific schedule.
        Otherwise, modifies the active (selected) schedule.
        """
        # Support both legacy 'day' and new 'start_day'/'end_day' parameters
        schedule_name = call.data.get(ATTR_SCHEDULE_NAME)
        start_day_str = call.data.get(ATTR_START_DAY) or call.data.get(ATTR_DAY)
        end_day_str = call.data.get(ATTR_END_DAY) or call.data.get(ATTR_DAY)
        start_time = call.data.get(ATTR_START_TIME)
        end_time = call.data.get(ATTR_END_TIME)
        zone_name = call.data.get(ATTR_ZONE_NAME)
        target_home_id = call.data.get(ATTR_HOME_ID)

        # Errors are raised, not only logged, so callers such as the schedule
        # card can tell the user that nothing was changed.
        if not zone_name:
            raise ServiceValidationError("zone_name must be provided")

        # Convert start_day from string to int
        try:
            start_day = int(start_day_str)
            if not (0 <= start_day <= 6):
                raise ValueError("start_day must be between 0 and 6")
        except (ValueError, TypeError) as err:
            raise ServiceValidationError(f"Invalid start_day value: {start_day_str} (expected 0-6)") from err

        # Convert end_day from string to int
        try:
            end_day = int(end_day_str)
            if not (0 <= end_day <= 6):
                raise ValueError("end_day must be between 0 and 6")
        except (ValueError, TypeError) as err:
            raise ServiceValidationError(f"Invalid end_day value: {end_day_str} (expected 0-6)") from err

        # Parse start_time (TimeSelector returns HH:MM:SS string or dict)
        try:
            if isinstance(start_time, dict):
                start_hours = start_time.get("hours", 0)
                start_minutes = start_time.get("minutes", 0)
            else:
                # Handle HH:MM or HH:MM:SS format
                parts = str(start_time).split(":")
                start_hours = int(parts[0])
                start_minutes = int(parts[1]) if len(parts) > 1 else 0
            if not (0 <= start_hours <= 23 and 0 <= start_minutes <= 59):
                raise ValueError("Invalid start time")
        except (ValueError, AttributeError, TypeError) as e:
            raise ServiceValidationError(f"Invalid start_time format: {start_time} ({e})") from e

        # Parse end_time (TimeSelector returns HH:MM:SS string or dict)
        # Special case: 00:00 means end of day (midnight)
        try:
            if isinstance(end_time, dict):
                end_hours = end_time.get("hours", 0)
                end_minutes = end_time.get("minutes", 0)
            else:
                # Handle HH:MM or HH:MM:SS format
                parts = str(end_time).split(":")
                end_hours = int(parts[0])
                end_minutes = int(parts[1]) if len(parts) > 1 else 0
            if not (0 <= end_hours <= 23 and 0 <= end_minutes <= 59):
                raise ValueError("Invalid end time")
        except (ValueError, AttributeError, TypeError) as e:
            raise ServiceValidationError(f"Invalid end_time format: {end_time} ({e})") from e

        # Calculate m_offsets (minutes from Monday 00:00)
        start_m_offset = start_day * MINUTES_PER_DAY + start_hours * 60 + start_minutes

        # For end offset: handle 00:00 (midnight) specially
        # - Same day with 00:00 end: means "end of that day" = start of next day
        # - Different day with 00:00: means "start of that day" (the midnight boundary)
        if end_hours == 0 and end_minutes == 0 and start_day == end_day:
            # Same day, 00:00 means end of that day (next day's 00:00)
            end_m_offset = (end_day + 1) * MINUTES_PER_DAY
        else:
            # Different day or non-midnight: calculate normally
            end_m_offset = end_day * MINUTES_PER_DAY + end_hours * 60 + end_minutes

        # Validate that end is after start (considering week wrap)
        # For multi-day spans, end_m_offset might be less if wrapping around the week
        if start_day == end_day and end_m_offset <= start_m_offset:
            raise ServiceValidationError(
                f"end_time ({end_time}) must be after start_time ({start_time}) on the same day"
            )

        entries = _get_target_entries(target_home_id)

        if not entries:
            if target_home_id:
                raise ServiceValidationError(f"Home with ID {target_home_id} not found")
            raise ServiceValidationError("No configured homes found")

        # Warn if multiple homes and no home_id specified
        if len(entries) > 1 and not target_home_id:
            home_names = [d.get("intuis_home").name for _, d in entries if d.get("intuis_home")]
            _LOGGER.warning(
                "Multiple homes configured (%s) but no home_id specified. "
                "Using first home. Add 'home_id' parameter to target a specific home.",
                ", ".join(home_names),
            )

        # Process first matching entry
        entry_id, data = entries[0]
        api: IntuisAPI = data.get("api")
        intuis_home: IntuisHome = data.get("intuis_home")
        coordinator = data.get("coordinator")

        if not api or not intuis_home:
            raise ServiceValidationError("API or home data not available")

        # Find the target schedule (by name if provided, otherwise active)
        target_schedule = None
        available_schedules = []
        
        for schedule in intuis_home.schedules:
            if isinstance(schedule, IntuisThermSchedule):
                has_zones = schedule.zones and len(schedule.zones) > 0
                has_timetables = schedule.timetables and len(schedule.timetables) > 0
                if has_zones and has_timetables:
                    available_schedules.append(schedule.name)
                    
                    if schedule_name:
                        # Match by name if schedule_name is provided
                        if schedule.name == schedule_name:
                            target_schedule = schedule
                    elif schedule.selected:
                        # Fall back to active schedule if no name provided
                        target_schedule = schedule

        if not target_schedule:
            if schedule_name:
                raise ServiceValidationError(
                    f"Schedule not found: {schedule_name}. Available schedules: {available_schedules}"
                )
            raise ServiceValidationError(f"No active therm schedule found. Available: {available_schedules}")

        # Find the target zone by name
        target_zone = None
        available_zones = []
        for zone in target_schedule.zones:
            if isinstance(zone, IntuisThermZone):
                available_zones.append(zone.name)
                if zone.name.lower() == zone_name.lower():
                    target_zone = zone
                    break

        if not target_zone:
            raise ServiceValidationError(
                f"Zone not found: {zone_name}. Available zones: {available_zones}"
            )

        if start_day == end_day:
            _LOGGER.info(
                "Setting zone '%s' (ID: %d) on %s from %s to %s for home %s",
                target_zone.name,
                target_zone.id,
                DAYS_OF_WEEK[start_day],
                start_time,
                end_time,
                intuis_home.name or intuis_home.id,
            )
        else:
            _LOGGER.info(
                "Setting zone '%s' (ID: %d) from %s %s to %s %s for home %s",
                target_zone.name,
                target_zone.id,
                DAYS_OF_WEEK[start_day],
                start_time,
                DAYS_OF_WEEK[end_day],
                end_time,
                intuis_home.name or intuis_home.id,
            )

        # Build updated timetable
        timetable = [
            {"zone_id": t.zone_id, "m_offset": t.m_offset}
            for t in target_schedule.timetables
        ]

        # Validate timetable is not empty
        if not timetable:
            raise ServiceValidationError(
                f"Schedule '{target_schedule.name}' has no timetable entries. Cannot set slot."
            )

        # Replace everything inside the slot with the target zone and restore,
        # at the slot end, the zone that was scheduled there (wraps the week).
        timetable = apply_slot(timetable, start_m_offset, end_m_offset, target_zone.id)

        # Sort and remove consecutive duplicates (API requirement)
        timetable = remove_consecutive_duplicates(timetable)

        _LOGGER.debug(
            "Timetable after update: %s",
            [(t["m_offset"], t["zone_id"]) for t in timetable],
        )

        # Build zones payload (only rooms_temp, not rooms - API requirement)
        zones_payload = []
        for zone in target_schedule.zones:
            if isinstance(zone, IntuisThermZone):
                zone_data = {
                    "id": zone.id,
                    "name": zone.name,
                    "type": zone.type,
                    "rooms_temp": [
                        {"room_id": rt.room_id, "temp": rt.temp}
                        for rt in zone.rooms_temp
                    ],
                }
                zones_payload.append(zone_data)

        try:
            await api.async_sync_schedule(
                schedule_id=target_schedule.id,
                schedule_name=target_schedule.name,
                schedule_type=target_schedule.type,
                timetable=timetable,
                zones=zones_payload,
                away_temp=target_schedule.away_temp,
                hg_temp=target_schedule.hg_temp,
            )

            # Update local timetable state
            target_schedule.timetables = [
                IntuisTimetable(zone_id=t["zone_id"], m_offset=t["m_offset"])
                for t in timetable
            ]

            # Refresh coordinator
            if coordinator:
                await coordinator.async_request_refresh()

            _LOGGER.info("Schedule slot updated successfully")

        except (APIError, CannotConnect, RateLimitError) as err:
            _LOGGER.debug("Failed to set schedule slot: %s", err)
            raise

    async def async_handle_set_zone_temperature(call: ServiceCall) -> None:
        """Handle set_zone_temperature service call.

        Sets the temperature for a specific room in a schedule zone.
        Works with any schedule, not just the active one.
        
        Accepts schedule_id (preferred) or schedule_name (fallback for compatibility).
        """
        schedule_id = call.data.get(ATTR_SCHEDULE_ID)
        schedule_name = call.data.get(ATTR_SCHEDULE_NAME)
        zone_name = call.data.get(ATTR_ZONE_NAME)
        room_name = call.data.get(ATTR_ROOM_NAME)
        temperature = call.data.get(ATTR_TEMPERATURE)
        target_home_id = call.data.get(ATTR_HOME_ID)

        if not schedule_id and not schedule_name:
            raise ServiceValidationError("Either schedule_id or schedule_name must be provided")
            
        if not all([zone_name, room_name, temperature is not None]):
            raise ServiceValidationError("Missing required parameters for set_zone_temperature")

        # Validate temperature range
        if not (5 <= temperature <= 30):
            raise ServiceValidationError(f"Temperature must be between 5 and 30, got: {temperature}")

        entries = _get_target_entries(target_home_id)

        if not entries:
            if target_home_id:
                raise ServiceValidationError(f"Home with ID {target_home_id} not found")
            raise ServiceValidationError("No configured homes found")

        # Warn if multiple homes and no home_id specified
        if len(entries) > 1 and not target_home_id:
            home_names = [d.get("intuis_home").name for _, d in entries if d.get("intuis_home")]
            _LOGGER.warning(
                "Multiple homes configured (%s) but no home_id specified. "
                "Using first home. Add 'home_id' parameter to target a specific home.",
                ", ".join(home_names),
            )

        # Process first matching entry
        entry_id, data = entries[0]
        api: IntuisAPI = data.get("api")
        intuis_home: IntuisHome = data.get("intuis_home")
        coordinator = data.get("coordinator")

        if not api or not intuis_home:
            raise ServiceValidationError("API or home data not available")

        # Find the target schedule by ID (preferred) or name (fallback)
        target_schedule = None
        available_schedules = []
        for schedule in intuis_home.schedules:
            if isinstance(schedule, IntuisThermSchedule):
                has_zones = schedule.zones and len(schedule.zones) > 0
                has_timetables = schedule.timetables and len(schedule.timetables) > 0
                if has_zones and has_timetables:
                    available_schedules.append(f"{schedule.name} (ID: {schedule.id})")
                    # Match by ID first (preferred)
                    if schedule_id and schedule.id == schedule_id:
                        target_schedule = schedule
                        _LOGGER.debug("Found schedule by ID: %s", schedule_id)
                        break
                    # Fallback to name match only if no ID provided
                    elif not schedule_id and schedule_name and schedule.name == schedule_name:
                        target_schedule = schedule
                        _LOGGER.warning(
                            "Finding schedule by name '%s' (deprecated). Use schedule_id for reliability.",
                            schedule_name
                        )
                        break

        if not target_schedule:
            raise ServiceValidationError(f"Schedule not found: ID={schedule_id}, name={schedule_name}. Available schedules: {available_schedules}")

        # Find the target zone by name within the schedule
        target_zone = None
        available_zones = []
        for zone in target_schedule.zones:
            if isinstance(zone, IntuisThermZone):
                available_zones.append(zone.name)
                if zone.name.lower() == zone_name.lower():
                    target_zone = zone
                    break

        if not target_zone:
            raise ServiceValidationError(f"Zone not found: {zone_name} in schedule {schedule_name}. Available zones: {available_zones}")

        # Find the room by name and get its ID
        room_id = None
        available_rooms = []
        for rid, room_def in intuis_home.rooms.items():
            rname = room_def.name if hasattr(room_def, 'name') else str(rid)
            available_rooms.append(rname)
            if rname.lower() == room_name.lower():
                room_id = rid
                break

        if not room_id:
            raise ServiceValidationError(f"Room not found: {room_name}. Available rooms: {available_rooms}")

        # Find and update the room temperature in the zone
        room_temp_found = False
        for room_temp in target_zone.rooms_temp:
            if room_temp.room_id == room_id:
                old_temp = room_temp.temp
                room_temp.temp = int(temperature)
                room_temp_found = True
                _LOGGER.info(
                    "Updating temperature for room '%s' in zone '%s' of schedule '%s' in home %s: %d -> %d",
                    room_name,
                    zone_name,
                    schedule_name,
                    intuis_home.name or intuis_home.id,
                    old_temp,
                    int(temperature),
                )
                break

        if not room_temp_found:
            raise ServiceValidationError(f"Room '{room_name}' (ID: {room_id}) not found in zone '{zone_name}' rooms_temp")

        # Build zones payload (only rooms_temp, not rooms - API requirement)
        zones_payload = []
        for zone in target_schedule.zones:
            if isinstance(zone, IntuisThermZone):
                zone_data = {
                    "id": zone.id,
                    "name": zone.name,
                    "type": zone.type,
                    "rooms_temp": [
                        {"room_id": rt.room_id, "temp": rt.temp}
                        for rt in zone.rooms_temp
                    ],
                }
                zones_payload.append(zone_data)

        # Build timetable payload
        timetable = [
            {"zone_id": t.zone_id, "m_offset": t.m_offset}
            for t in target_schedule.timetables
        ]

        try:
            await api.async_sync_schedule(
                schedule_id=target_schedule.id,
                schedule_name=target_schedule.name,
                schedule_type=target_schedule.type,
                timetable=timetable,
                zones=zones_payload,
                away_temp=target_schedule.away_temp,
                hg_temp=target_schedule.hg_temp,
                
            )

            # Refresh coordinator
            if coordinator:
                await coordinator.async_request_refresh()

            _LOGGER.info("Zone temperature updated successfully")

        except (APIError, CannotConnect, RateLimitError) as err:
            _LOGGER.debug("Failed to set zone temperature: %s", err)
            raise

    async def async_handle_sync_schedule(call: ServiceCall) -> None:
        """Handle sync_schedule service call.

        Synchronizes the complete timetable for a schedule.
        Used by the planning UI to save all changes at once.
        
        Accepts schedule_id (preferred) or schedule_name (fallback for compatibility).
        """
        import json
        
        schedule_id = call.data.get(ATTR_SCHEDULE_ID)
        schedule_name = call.data.get(ATTR_SCHEDULE_NAME)
        timetable_json = call.data.get(ATTR_TIMETABLE)
        target_home_id = call.data.get(ATTR_HOME_ID)
        override_away_temp = call.data.get(ATTR_AWAY_TEMP)
        override_hg_temp = call.data.get(ATTR_HG_TEMP)

        if not schedule_id and not schedule_name:
            raise ServiceValidationError("Either schedule_id or schedule_name must be provided")

        if not timetable_json:
            raise ServiceValidationError("timetable must be provided")

        # Parse timetable JSON
        try:
            if isinstance(timetable_json, str):
                timetable_raw = json.loads(timetable_json)
            else:
                timetable_raw = timetable_json
            
            # Validate and convert timetable format (ensure int types)
            if not isinstance(timetable_raw, list):
                raise ValueError("timetable must be a list")
            
            timetable = []
            for entry in timetable_raw:
                if not isinstance(entry, dict) or "zone_id" not in entry or "m_offset" not in entry:
                    raise ValueError("Each entry must have zone_id and m_offset")
                # Use zone_id first to match API response format
                timetable.append({
                    "zone_id": int(entry["zone_id"]),
                    "m_offset": int(entry["m_offset"])
                })
                
        except (json.JSONDecodeError, ValueError) as err:
            raise ServiceValidationError(f"Invalid timetable format: {err}")

        entries = _get_target_entries(target_home_id)

        if not entries:
            if target_home_id:
                raise ServiceValidationError(f"Home with ID {target_home_id} not found")
            raise ServiceValidationError("No configured homes found")

        # Process first matching entry
        entry_id, data = entries[0]
        api: IntuisAPI = data.get("api")
        intuis_home: IntuisHome = data.get("intuis_home")
        coordinator = data.get("coordinator")

        if not api or not intuis_home:
            raise ServiceValidationError("API or home data not available")

        # Find the target schedule by ID (preferred) or name (fallback)
        target_schedule = None
        available_schedules = []
        
        for schedule in intuis_home.schedules:
            if isinstance(schedule, IntuisThermSchedule):
                has_zones = schedule.zones and len(schedule.zones) > 0
                if has_zones:
                    available_schedules.append(f"{schedule.name} (ID: {schedule.id})")
                    # Match by ID first (preferred)
                    if schedule_id and schedule.id == schedule_id:
                        target_schedule = schedule
                        _LOGGER.debug("Found schedule by ID: %s", schedule_id)
                    # Fallback to name match only if no ID provided
                    elif not schedule_id and schedule_name and schedule.name == schedule_name:
                        target_schedule = schedule
                        _LOGGER.warning(
                            "Finding schedule by name '%s' (deprecated). Use schedule_id for reliability.",
                            schedule_name
                        )

        if not target_schedule:
            raise ServiceValidationError(f"Schedule not found: ID={schedule_id}, name={schedule_name}. Available schedules: {available_schedules}")

        # Get valid zone IDs from the schedule
        valid_zone_ids = {zone.id for zone in target_schedule.zones if isinstance(zone, IntuisThermZone)}
        _LOGGER.debug("Valid zone IDs for schedule '%s': %s", target_schedule.name, valid_zone_ids)
        
        # Validate all zone_ids in timetable exist in the schedule
        for entry in timetable:
            if entry["zone_id"] not in valid_zone_ids:
                raise ServiceValidationError(f"Invalid zone_id {entry['zone_id']} in timetable. Valid IDs: {valid_zone_ids}")
            # Validate m_offset is in valid range (0-10079)
            if not (0 <= entry["m_offset"] < 10080):
                raise ServiceValidationError(f"Invalid m_offset {entry['m_offset']} in timetable. Must be 0-10079")

        _LOGGER.info(
            "Syncing full timetable for schedule '%s' with %d entries",
            target_schedule.name,
            len(timetable),
        )

        # Ensure timetable starts at m_offset = 0 (API requirement)
        timetable = sorted(timetable, key=lambda x: x["m_offset"])
        if not timetable or timetable[0]["m_offset"] != 0:
            # Add a slot at 0 with the last zone (week wrap-around)
            first_zone = timetable[-1]["zone_id"] if timetable else list(valid_zone_ids)[0]
            timetable.insert(0, {"zone_id": first_zone, "m_offset": 0})
            _LOGGER.debug("Added missing slot at m_offset=0 with zone_id=%d", first_zone)

        # Sort and remove consecutive duplicates (API requirement)
        timetable = remove_consecutive_duplicates(timetable)
        
        # Final validation: ensure m_offset values are strictly increasing
        for i in range(1, len(timetable)):
            if timetable[i]["m_offset"] <= timetable[i-1]["m_offset"]:
                _LOGGER.error(
                    "CRITICAL: Timetable validation failed! m_offset not strictly increasing: "
                    "[%d]=%d <= [%d]=%d. Full timetable: %s",
                    i, timetable[i]["m_offset"],
                    i-1, timetable[i-1]["m_offset"],
                    [(t["m_offset"], t["zone_id"]) for t in timetable],
                )
                raise ValueError("Invalid timetable: m_offset values must be strictly increasing")
        
        _LOGGER.debug(
            "Cleaned timetable has %d entries: %s",
            len(timetable),
            [(t["m_offset"], t["zone_id"]) for t in timetable],
        )

        # Build zones payload (preserve existing zone temperatures)
        zones_payload = []
        for zone in target_schedule.zones:
            if isinstance(zone, IntuisThermZone):
                zone_data = {
                    "id": zone.id,
                    "name": zone.name,
                    "type": zone.type,
                    "rooms_temp": [
                        {"room_id": rt.room_id, "temp": rt.temp}
                        for rt in zone.rooms_temp
                    ],
                }
                zones_payload.append(zone_data)

        # Apply override temps if provided by caller (e.g. planning UI)
        final_away_temp = override_away_temp if override_away_temp is not None else target_schedule.away_temp
        final_hg_temp = override_hg_temp if override_hg_temp is not None else target_schedule.hg_temp

        _LOGGER.info(
            "sync_schedule payload: schedule_id=%s, name=%s, type=%s, "
            "timetable_count=%d, zones_count=%d, away=%s, hg=%s",
            target_schedule.id,
            target_schedule.name,
            target_schedule.type,
            len(timetable),
            len(zones_payload),
            final_away_temp,
            final_hg_temp,
        )
        _LOGGER.debug("Timetable being sent: %s", timetable)
        _LOGGER.debug("Zones being sent: %s", zones_payload)

        try:
            await api.async_sync_schedule(
                schedule_id=target_schedule.id,
                schedule_name=target_schedule.name,
                schedule_type=target_schedule.type,
                timetable=timetable,
                zones=zones_payload,
                away_temp=final_away_temp,
                hg_temp=final_hg_temp,
            )

            # Update local timetable state
            target_schedule.timetables = [
                IntuisTimetable(zone_id=t["zone_id"], m_offset=t["m_offset"])
                for t in timetable
            ]
            # Update local temps if overridden
            if override_away_temp is not None:
                target_schedule.away_temp = final_away_temp
            if override_hg_temp is not None:
                target_schedule.hg_temp = final_hg_temp

            # Refresh coordinator
            if coordinator:
                await coordinator.async_request_refresh()

            _LOGGER.info("Schedule timetable synced successfully")

        except (APIError, CannotConnect, RateLimitError) as err:
            _LOGGER.debug("Failed to sync schedule: %s", err)
            raise

    # Store for import managers (one per entry)
    if "import_managers" not in hass.data[DOMAIN]:
        hass.data[DOMAIN]["import_managers"] = {}

    async def async_handle_import_energy_history(call: ServiceCall) -> None:
        """Handle import_energy_history service call.

        Imports historical energy data from Intuis cloud to existing sensor entities.
        Uses async_import_statistics with source="recorder" to merge with entity stats.

        If home_id is specified, imports only for that home.
        Otherwise, imports for all configured homes.
        """
        days = call.data.get(ATTR_DAYS, DEFAULT_HISTORY_DAYS)
        room_name = call.data.get(ATTR_ROOM_NAME)
        target_home_id = call.data.get(ATTR_HOME_ID)
        granularity = call.data.get(ATTR_GRANULARITY, "hourly")  # Default to hourly
        clear_existing = call.data.get(ATTR_CLEAR_EXISTING, False)

        entries = _get_target_entries(target_home_id)

        if not entries:
            if target_home_id:
                raise ServiceValidationError(f"Home with ID {target_home_id} not found")
            raise ServiceValidationError("No configured homes found")

        import_count = 0
        for entry_id, data in entries:
            api: IntuisAPI = data.get("api")
            intuis_home: IntuisHome = data.get("intuis_home")

            if not api or not intuis_home:
                continue

            # Get or create import manager for this entry
            if entry_id not in hass.data[DOMAIN]["import_managers"]:
                manager = HistoryImportManager(hass, entry_id)
                await manager.async_load()
                hass.data[DOMAIN]["import_managers"][entry_id] = manager
            else:
                manager = hass.data[DOMAIN]["import_managers"][entry_id]

            if manager.is_running:
                _LOGGER.warning(
                    "Energy history import already running for home %s",
                    intuis_home.name or intuis_home.id,
                )
                continue

            _LOGGER.info(
                "Starting energy history import for home: %s (%d days, %s granularity%s%s)",
                intuis_home.name or intuis_home.id,
                days,
                granularity,
                f", room: {room_name}" if room_name else "",
                ", clearing existing stats" if clear_existing else "",
            )

            # Run import in a background task tied to the entry (cancelled on unload)
            import_entry = hass.config_entries.async_get_entry(entry_id)
            create_task = (
                partial(import_entry.async_create_background_task, hass)
                if import_entry
                else hass.async_create_background_task
            )
            create_task(
                async_import_energy_history(
                    hass=hass,
                    api=api,
                    intuis_home=intuis_home,
                    manager=manager,
                    days=days,
                    room_filter=room_name,
                    home_id=intuis_home.id,
                    granularity=granularity,
                    clear_existing=clear_existing,
                ),
                name=f"intuis_connect history import {entry_id}",
            )
            import_count += 1

        if import_count == 0:
            _LOGGER.warning("No homes available for import")
        else:
            _LOGGER.info("Started energy history import for %d home(s)", import_count)

    # Day options with English labels
    day_options = [
        {"value": str(i), "label": label}
        for i, label in enumerate(DAYS_OF_WEEK_LABELS)
    ]

    # Schedule, zone and room names are validated by the handlers against the
    # live data: options captured here would reject zones of a schedule
    # activated later. The UI dropdowns come from services.yaml.
    switch_schedule_schema = vol.Schema({
        vol.Required(ATTR_SCHEDULE_NAME): cv.string,
        vol.Optional(ATTR_HOME_ID): cv.string,
    })

    set_schedule_slot_schema = vol.Schema({
        vol.Optional(ATTR_SCHEDULE_NAME): cv.string,
        vol.Optional(ATTR_HOME_ID): cv.string,
        vol.Required(ATTR_START_DAY): SelectSelector(
            SelectSelectorConfig(
                options=day_options,
                mode=SelectSelectorMode.DROPDOWN,
            )
        ),
        vol.Required(ATTR_START_TIME): TimeSelector(TimeSelectorConfig()),
        vol.Required(ATTR_END_DAY): SelectSelector(
            SelectSelectorConfig(
                options=day_options,
                mode=SelectSelectorMode.DROPDOWN,
            )
        ),
        vol.Required(ATTR_END_TIME): TimeSelector(TimeSelectorConfig()),
        vol.Required(ATTR_ZONE_NAME): cv.string,
    })

    set_zone_temperature_schema = vol.Schema({
        vol.Optional(ATTR_SCHEDULE_ID): TextSelector(
            TextSelectorConfig(
                type=TextSelectorType.TEXT,
            )
        ),
        vol.Optional(ATTR_SCHEDULE_NAME): cv.string,
        vol.Optional(ATTR_HOME_ID): cv.string,
        vol.Required(ATTR_ZONE_NAME): cv.string,
        vol.Required(ATTR_ROOM_NAME): cv.string,
        vol.Required(ATTR_TEMPERATURE): NumberSelector(
            NumberSelectorConfig(
                min=5,
                max=30,
                step=0.5,
                unit_of_measurement="°C",
                mode=NumberSelectorMode.BOX,
            )
        ),
    })

    sync_schedule_schema = vol.Schema({
        vol.Optional(ATTR_SCHEDULE_ID): TextSelector(
            TextSelectorConfig(
                type=TextSelectorType.TEXT,
            )
        ),
        vol.Optional(ATTR_SCHEDULE_NAME): cv.string,
        vol.Optional(ATTR_HOME_ID): cv.string,
        vol.Required(ATTR_TIMETABLE): TextSelector(
            TextSelectorConfig(
                multiline=True,
                type=TextSelectorType.TEXT,
            )
        ),
        vol.Optional(ATTR_AWAY_TEMP): NumberSelector(
            NumberSelectorConfig(min=5, max=25, step=0.5, unit_of_measurement="°C")
        ),
        vol.Optional(ATTR_HG_TEMP): NumberSelector(
            NumberSelectorConfig(min=5, max=15, step=0.5, unit_of_measurement="°C")
        ),
    })

    # Granularity options for import
    granularity_options = [
        {"value": "hourly", "label": "Hourly (recommended for Energy Dashboard)"},
        {"value": "daily", "label": "Daily (faster import)"},
    ]

    import_energy_history_schema = vol.Schema({
        vol.Optional(ATTR_DAYS, default=DEFAULT_HISTORY_DAYS): NumberSelector(
            NumberSelectorConfig(
                min=1,
                max=MAX_HISTORY_DAYS,
                step=1,
                mode=NumberSelectorMode.BOX,
            )
        ),
        vol.Optional(ATTR_GRANULARITY, default="hourly"): SelectSelector(
            SelectSelectorConfig(
                options=granularity_options,
                mode=SelectSelectorMode.DROPDOWN,
            )
        ),
        vol.Optional(ATTR_CLEAR_EXISTING, default=False): BooleanSelector(),
        vol.Optional(ATTR_ROOM_NAME): cv.string,
        vol.Optional(ATTR_HOME_ID): cv.string,
    })

    # Register services (only once)
    if not hass.services.has_service(DOMAIN, SERVICE_SWITCH_SCHEDULE):
        hass.services.async_register(
            DOMAIN,
            SERVICE_SWITCH_SCHEDULE,
            _with_api_errors(async_handle_switch_schedule),
            schema=switch_schedule_schema,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_REFRESH_SCHEDULES):
        hass.services.async_register(
            DOMAIN,
            SERVICE_REFRESH_SCHEDULES,
            _with_api_errors(async_handle_refresh_schedules),
            schema=REFRESH_SCHEDULES_SCHEMA,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_SET_SCHEDULE_SLOT):
        hass.services.async_register(
            DOMAIN,
            SERVICE_SET_SCHEDULE_SLOT,
            _with_api_errors(async_handle_set_schedule_slot),
            schema=set_schedule_slot_schema,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_SET_ZONE_TEMPERATURE):
        hass.services.async_register(
            DOMAIN,
            SERVICE_SET_ZONE_TEMPERATURE,
            _with_api_errors(async_handle_set_zone_temperature),
            schema=set_zone_temperature_schema,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_SYNC_SCHEDULE):
        hass.services.async_register(
            DOMAIN,
            SERVICE_SYNC_SCHEDULE,
            _with_api_errors(async_handle_sync_schedule),
            schema=sync_schedule_schema,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_IMPORT_ENERGY_HISTORY):
        hass.services.async_register(
            DOMAIN,
            SERVICE_IMPORT_ENERGY_HISTORY,
            _with_api_errors(async_handle_import_energy_history),
            schema=import_energy_history_schema,
        )

    # --- recalculate_cost_history service -----------------------------------
    async def async_handle_recalculate_cost_history(call: ServiceCall) -> None:
        """Handle recalculate_cost_history service call."""
        days = call.data.get("days", 365)
        room_name = call.data.get("room_name") or None
        entries = [
            (entry_id, data)
            for entry_id, data in _get_target_entries(call.data.get(ATTR_HOME_ID))
            if data.get("cost_stats_updater") is not None
        ]
        if not entries:
            raise ServiceValidationError(
                "Energy cost tracking is not enabled for the targeted home(s)"
            )

        for _entry_id, data in entries:
            intuis_home: IntuisHome = data["intuis_home"]
            cost_updater = data["cost_stats_updater"]
            room_id_filter: str | None = None
            if room_name:
                room_id_filter = next(
                    (rid for rid, room in intuis_home.rooms.items()
                     if room.name.lower() == room_name.lower()),
                    None,
                )
                if room_id_filter is None:
                    raise ServiceValidationError(f"Room not found: {room_name}")

            _LOGGER.info(
                "recalculate_cost_history: starting for %s (%d days, room=%s)",
                intuis_home.name or intuis_home.id, days, room_id_filter or "all",
            )
            total = await cost_updater.async_recalculate_history(
                days=days,
                room_id_filter=room_id_filter,
            )
            cost_updater.publish_cost_base()
            _LOGGER.info("recalculate_cost_history: wrote %d cost stat points", total)

    recalculate_cost_schema = vol.Schema({
        vol.Optional("days", default=365): vol.All(vol.Coerce(int), vol.Range(min=1, max=730)),
        vol.Optional("room_name"): cv.string,
        vol.Optional(ATTR_HOME_ID): cv.string,
    })

    if not hass.services.has_service(DOMAIN, SERVICE_RECALCULATE_COST_HISTORY):
        hass.services.async_register(
            DOMAIN,
            SERVICE_RECALCULATE_COST_HISTORY,
            _with_api_errors(async_handle_recalculate_cost_history),
            schema=recalculate_cost_schema,
        )


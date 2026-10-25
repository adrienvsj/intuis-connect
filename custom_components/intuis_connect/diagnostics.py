"""Diagnostics for Intuis Connect (Settings > Devices & services > Download diagnostics)."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .entity.intuis_schedule import IntuisThermSchedule
from .utils.const import CONF_HOME_NAME, CONF_REFRESH_TOKEN, CONF_USERNAME, DOMAIN

# Credentials, account identity and location
TO_REDACT = {CONF_REFRESH_TOKEN, CONF_USERNAME, CONF_HOME_NAME, "title", "unique_id", "coordinates", "city"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return the integration state useful to debug a report."""
    data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    coordinator = data.get("coordinator")
    home = data.get("intuis_home")
    coordinator_data = (coordinator.data if coordinator else None) or {}

    rooms = {
        room_id: {
            "mode": room.mode,
            "temperature": room.temperature,
            "target_temperature": room.target_temperature,
            "heating": room.heating,
            "muller_type": room.muller_type,
            "pilot_wire_order": room.therm_setpoint_fp,
            "boost_status": room.boost_status,
            "energy_kwh": room.energy,
            "module_count": len(room.modules or []),
        }
        for room_id, room in (coordinator_data.get("rooms") or {}).items()
    }

    hourly = data.get("hourly_stats_updater")
    hourly_data = getattr(hourly, "_data", {}) or {}

    return {
        "entry": async_redact_data(entry.as_dict(), TO_REDACT),
        "home": async_redact_data(
            {
                "timezone": getattr(home, "timezone", None),
                "therm_mode": getattr(home, "therm_mode", None),
                "coordinates": getattr(home, "coordinates", None),
                "city": getattr(home, "city", None),
                "room_count": len(getattr(home, "rooms", None) or {}),
                "schedules": [
                    {
                        "selected": schedule.selected,
                        "zones": len(schedule.zones or []),
                        "timetable_entries": len(schedule.timetables or []),
                    }
                    for schedule in getattr(home, "schedules", None) or []
                    if isinstance(schedule, IntuisThermSchedule)
                ],
            },
            TO_REDACT,
        ),
        "coordinator": {
            "last_update_success": getattr(coordinator, "last_update_success", None),
            "update_interval": str(getattr(coordinator, "update_interval", None)),
        },
        "rooms": rooms,
        "hourly_statistics": {
            "enabled": hourly is not None,
            "tracked_sensors": sorted(hourly_data.get("anchors", {})),
            "last_anchors": hourly_data.get("anchors", {}),
        },
    }

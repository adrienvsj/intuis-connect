from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from ..entity.intuis_room import IntuisRoom
from ..utils.const import DOMAIN

IntuisDataUpdateCoordinator = DataUpdateCoordinator[dict[str, Any]]


def home_device_info(home_id: str, coordinator: IntuisDataUpdateCoordinator | None = None) -> DeviceInfo:
    """Return the DeviceInfo of the single device grouping a home's entities.

    The device is named after the home in the Intuis app (fallback "Intuis Home").
    """
    data = coordinator.data if coordinator is not None else None
    home = data.get("intuis_home") if isinstance(data, dict) else None
    return DeviceInfo(
        identifiers={(DOMAIN, f"{home_id}_home")},
        name=getattr(home, "name", None) or "Intuis Home",
        manufacturer="Muller Intuitiv (Netatmo)",
        model="Home Controller",
    )


class IntuisEntity(Entity):
    """Base class for Intuis entities."""

    def __init__(self, coordinator: IntuisDataUpdateCoordinator, room: IntuisRoom, home_id: str, name: str,
                 entity_type: str) -> None:
        """Initialize the Intuis entity."""
        Entity.__init__(self)
        self._coordinator = coordinator
        self._room = room
        self._home_id = home_id
        self._attr_name = name
        self._attr_unique_id = f"{self._get_id_prefix()}_{entity_type}"
        self._attr_device_info = self._build_device_info(home_id, room.id, room.name)

    def _get_room(self) -> IntuisRoom | None:
        """Get the room object by ID."""
        rooms = self._coordinator.data.get("rooms", {})
        room_id = self._room.id
        return rooms.get(room_id) or rooms.get(str(room_id))

    def _get_id_prefix(self):
        return f"intuis_{self._home_id}_{self._room.id}"

    @staticmethod
    def _build_device_info(home_id: str, room_id: str, room_name: str) -> DeviceInfo:
        """Return a consistent DeviceInfo for all entities of one room."""
        return DeviceInfo(
            identifiers={(DOMAIN, f"{home_id}_{room_id}")},
            name=room_name,
            manufacturer="Muller Intuitiv (Netatmo)",
            model="Electric Radiator",
            suggested_area=room_name,
        )

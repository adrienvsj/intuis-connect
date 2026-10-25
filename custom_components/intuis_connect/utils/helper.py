"""Helper functions for the Intuis Connect integration."""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any, Concatenate

import aiohttp
from aiohttp import ClientSession
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from ..entity.intuis_entity import IntuisDataUpdateCoordinator
from ..entity.intuis_room import IntuisRoom
from ..intuis_api.api import (
    APIError,
    CannotConnect,
    IntuisAPI,
    InvalidAuth,
    RateLimitError,
)
from ..utils.const import DOMAIN

_LOGGER = logging.getLogger(__name__)


def api_command[E: Any, **P, R](
    func: Callable[Concatenate[E, P], Awaitable[R]],
) -> Callable[Concatenate[E, P], Awaitable[R]]:
    """Decorate an entity action so Intuis API errors reach the user as HA errors.

    A refused token also starts the re-authentication flow.
    """

    @wraps(func)
    async def wrapper(self: E, *args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return await func(self, *args, **kwargs)
        except InvalidAuth as err:
            entry = getattr(self.coordinator, "config_entry", None)
            if entry is not None:
                entry.async_start_reauth(self.hass)
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


async def async_validate_api(
        username: str, password: str, session: ClientSession
) -> tuple[list[dict[str, Any]], IntuisAPI]:
    """Validate the API and return the list of homes and API instance.

    Returns:
        Tuple of (homes_list, api) where homes_list is:
        [{"id": ..., "name": ..., "timezone": ...}, ...]
    """
    api = IntuisAPI(session)
    try:
        homes = await api.async_login(username, password)
        return homes, api
    except (CannotConnect, InvalidAuth) as e:
        # Shown to the user by the config flow; a wrong password is not an error
        _LOGGER.debug("API validation failed: %s", e)
        raise
    except aiohttp.ClientError as e:
        _LOGGER.exception("Unknown error during API validation")
        raise CannotConnect("unknown") from e


def get_coordinator(hass, entry) -> IntuisDataUpdateCoordinator:
    """Get the Intuis data update coordinator from the Home Assistant instance."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator = data["coordinator"]

    return coordinator


def get_rooms(coordinator: IntuisDataUpdateCoordinator) -> dict[str, IntuisRoom]:
    """Get the rooms data from the coordinator."""
    rooms = coordinator.data.get("rooms", {})
    if not rooms:
        _LOGGER.warning("No rooms found in coordinator data")
    return rooms


def get_api(hass: HomeAssistant, entry: ConfigEntry) -> IntuisAPI:
    """Get the Intuis API instance from the Home Assistant instance."""
    if DOMAIN not in hass.data or entry.entry_id not in hass.data[DOMAIN]:
        _LOGGER.error("No data found for entry %s", entry.entry_id)
        raise ValueError("No API instance found")
    data = hass.data[DOMAIN][entry.entry_id]
    api = data["api"]
    return api


def get_home_id(hass: HomeAssistant, entry: ConfigEntry) -> str:
    """Get the home ID from the config entry."""
    if DOMAIN not in hass.data or entry.entry_id not in hass.data[DOMAIN]:
        _LOGGER.error("No data found for entry %s", entry.entry_id)
        return "unknown_home"

    return get_api(hass, entry).home_id


def get_basic_utils(hass: HomeAssistant, entry: ConfigEntry) -> tuple[
    IntuisDataUpdateCoordinator, str, dict[str, IntuisRoom], IntuisAPI]:
    """Get basic utilities from the Home Assistant instance."""
    coordinator = get_coordinator(hass, entry)
    home_id = get_home_id(hass, entry)
    rooms = get_rooms(coordinator)
    api = get_api(hass, entry)

    return coordinator, home_id, rooms, api

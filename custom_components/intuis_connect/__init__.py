"""Setup for Intuis Connect (v1.10.0)."""
from __future__ import annotations

import datetime
import logging
from pathlib import Path

from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.loader import async_get_integration
from homeassistant.util import slugify

from .cost_stats import CostStatsUpdater
from .entity.intuis_entity import IntuisDataUpdateCoordinator
from .history_import import (
    HistoryImportManager,
    async_import_energy_history,
)
from .hourly_stats import HourlyStatsUpdater
from .intuis_api.api import (
    APIError,
    CannotConnect,
    IntuisAPI,
    InvalidAuth,
    RateLimitError,
)
from .intuis_data import IntuisData
from .services import async_register_services
from .utils.const import (
    CONF_CIRCUIT_BREAKER_THRESHOLD,
    CONF_ENERGY_COST_ENABLED,
    CONF_HOME_ID,
    CONF_HOME_NAME,
    CONF_HOURLY_STATS_ENABLED,
    CONF_HOURLY_STATS_INTERVAL,
    CONF_IMPORT_HISTORY,
    CONF_IMPORT_HISTORY_DAYS,
    CONF_MAX_UPDATE_INTERVAL,
    CONF_MIN_REQUEST_DELAY,
    CONF_RATE_LIMIT_DELAY,
    CONF_REFRESH_TOKEN,
    DEFAULT_CIRCUIT_THRESHOLD,
    DEFAULT_ENERGY_COST_ENABLED,
    DEFAULT_HOURLY_STATS_ENABLED,
    DEFAULT_HOURLY_STATS_INTERVAL,
    DEFAULT_MAX_UPDATE_INTERVAL,
    DEFAULT_MIN_REQUEST_DELAY,
    DEFAULT_RATE_LIMIT_DELAY,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

# Storage for persisting overrides across restarts
STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}.overrides"

PLATFORMS: list[Platform] = [
    Platform.CALENDAR,
    Platform.CLIMATE,
    Platform.BINARY_SENSOR,
    Platform.SENSOR,
    Platform.SELECT,
    Platform.NUMBER,
]


# One-shot flags cleared by the integration itself: changing them must not reload
# the entry (a reload would cancel the history import that just started).
_OPTIONS_IGNORED_FOR_RELOAD = (CONF_IMPORT_HISTORY, CONF_IMPORT_HISTORY_DAYS)
# hass.data[DOMAIN] key holding, per entry, the options the entry was set up with
_OPTIONS_SNAPSHOTS = "_options_snapshots"


def _options_for_reload(options) -> dict:
    """Return the options whose change requires a reload."""
    return {k: v for k, v in options.items() if k not in _OPTIONS_IGNORED_FOR_RELOAD}


async def update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when its options change (data-only updates are ignored)."""
    snapshot = hass.data.get(DOMAIN, {}).get(_OPTIONS_SNAPSHOTS, {}).get(entry.entry_id)
    if snapshot is not None and _options_for_reload(entry.options) == _options_for_reload(snapshot):
        _LOGGER.debug("Entry %s updated without option changes, not reloading", entry.entry_id)
        return
    _LOGGER.debug("Reloading entry %s due to options update", entry.entry_id)
    await hass.config_entries.async_reload(entry.entry_id)


@callback
def _async_migrate_cost_entity_ids(hass: HomeAssistant, intuis_home) -> None:
    """Move cost sensors from the old "_energy_cost" unique_id to "_cost".

    Older versions named cost sensors "sensor.X_energy_cost", which clashes
    with the entity the Energy dashboard creates for an energy sensor. The
    unique_id always moves to the new suffix. The entity_id is renamed to
    "sensor.X_cost" only if it still has its default value and the target is
    free: a user-chosen entity_id is kept, and entries of other integrations
    are never removed. The recorder renames the statistics with the entity.
    """
    ent_reg = er.async_get(hass)
    migrated = 0

    for room_id, room in (intuis_home.rooms or {}).items():
        new_unique_id = f"intuis_{intuis_home.id}_{room_id}_cost"
        if ent_reg.async_get_entity_id("sensor", DOMAIN, new_unique_id):
            continue  # already migrated

        old_unique_id = f"intuis_{intuis_home.id}_{room_id}_energy_cost"
        old_entity_id = ent_reg.async_get_entity_id(
            "sensor", DOMAIN, old_unique_id
        ) or ent_reg.async_get_entity_id("sensor", DOMAIN, f"{old_unique_id}_2")
        if not old_entity_id:
            continue

        room_slug = slugify(room.name)
        changes: dict[str, str] = {"new_unique_id": new_unique_id}
        new_entity_id = f"sensor.{room_slug}_cost"
        is_default_id = old_entity_id in (
            f"sensor.{room_slug}_energy_cost",
            f"sensor.{room_slug}_energy_cost_2",
        )
        if is_default_id and ent_reg.async_get(new_entity_id) is None and hass.states.get(new_entity_id) is None:
            changes["new_entity_id"] = new_entity_id

        ent_reg.async_update_entity(old_entity_id, **changes)
        _LOGGER.info("Migration: cost sensor %s moved to %s", old_entity_id, changes)
        migrated += 1

    if migrated:
        _LOGGER.info("Migration: migrated %d cost entities to 'cost' suffix", migrated)




_PLANNING_HTML = Path(__file__).parent / "resources" / "intuis_planning.html"


class IntuisPlanningView(HomeAssistantView):
    """Serve intuis_planning.html with automatic cache-busting based on integration version."""

    url = "/api/intuis_connect/planning"
    name = "api:intuis_connect:planning"
    # The page is a static file (no data, no token): it authenticates its own
    # calls with the frontend's connection or a user-provided token.
    requires_auth = False

    def __init__(self, hass: HomeAssistant, html_path: Path, version: str) -> None:
        """Initialize."""
        self._hass = hass
        self._html_path = html_path
        self._etag = f'"{version}"'
        self._html: str | None = None

    async def get(self, request):
        """Return the planning HTML file."""
        from aiohttp.web import HTTPNotFound, Response

        # Cache-busting: if client sends matching ETag, return 304
        if request.headers.get("If-None-Match") == self._etag:
            return Response(status=304)
        if self._html is None:
            # Read once, outside the event loop
            try:
                self._html = await self._hass.async_add_executor_job(
                    self._html_path.read_text, "utf-8"
                )
            except FileNotFoundError as err:
                raise HTTPNotFound() from err
        return Response(
            body=self._html,
            content_type="text/html",
            charset="utf-8",
            headers={
                "ETag": self._etag,
                "Cache-Control": "no-cache",
            },
        )


# Set up from config entries only (no YAML configuration)
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the services once; they act on every configured home."""
    hass.data.setdefault(DOMAIN, {})
    await async_register_services(hass)
    return True


@callback
def _async_remove_legacy_home_device(hass: HomeAssistant, entry: ConfigEntry, home_id: str) -> None:
    """Remove the second "Intuis Home" device used by versions before 2.2.

    Its entities now belong to the `{home_id}_home` device. It is only removed
    once no entity references it (removing a device removes its entities).
    """
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_device_by_identifier((DOMAIN, home_id), entry.entry_id)
    if device is None:
        return
    if er.async_entries_for_device(er.async_get(hass), device.id, include_disabled_entities=True):
        _LOGGER.debug("Legacy home device %s still has entities, keeping it", device.id)
        return
    dev_reg.async_remove_device(device.id)
    _LOGGER.info("Removed the duplicate legacy home device")


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Intuis Connect from a config entry."""
    _LOGGER.debug("Setting up entry %s", entry.entry_id)
    hass.data.setdefault(DOMAIN, {})
    # Taken before anything can update the entry (e.g. a renewed refresh token)
    hass.data[DOMAIN].setdefault(_OPTIONS_SNAPSHOTS, {})[entry.entry_id] = dict(entry.options)
    entry.async_on_unload(entry.add_update_listener(update_listener))

    # ---------- setup API ----------------------------------------------------------
    session = async_get_clientsession(hass)

    # Get rate limit options from config
    rate_limit_delay = entry.options.get(CONF_RATE_LIMIT_DELAY, DEFAULT_RATE_LIMIT_DELAY)
    circuit_threshold = entry.options.get(CONF_CIRCUIT_BREAKER_THRESHOLD, DEFAULT_CIRCUIT_THRESHOLD)
    min_request_delay = entry.options.get(CONF_MIN_REQUEST_DELAY, DEFAULT_MIN_REQUEST_DELAY)
    max_update_interval = entry.options.get(CONF_MAX_UPDATE_INTERVAL, DEFAULT_MAX_UPDATE_INTERVAL)

    intuis_api = IntuisAPI(
        session,
        home_id=entry.data["home_id"],
        rate_limit_delay=rate_limit_delay,
        circuit_threshold=circuit_threshold,
        min_request_delay=min_request_delay,
    )
    intuis_api.refresh_token = entry.data[CONF_REFRESH_TOKEN]

    @callback
    def _persist_refresh_token(token: str) -> None:
        """Store a renewed refresh token so the next start does not use a stale one."""
        if entry.data.get(CONF_REFRESH_TOKEN) != token:
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_REFRESH_TOKEN: token}
            )

    intuis_api.set_refresh_token_callback(_persist_refresh_token)

    try:
        await intuis_api.async_refresh_access_token()
        intuis_home = await intuis_api.async_get_homes_data()
    except InvalidAuth as err:
        raise ConfigEntryAuthFailed from err
    except (CannotConnect, APIError, RateLimitError) as err:
        # Cloud outage or rate limit: Home Assistant retries the setup later
        raise ConfigEntryNotReady(str(err)) from err
    _LOGGER.debug("Intuis home: %s", intuis_home.__str__())

    # ---------- shared overrides (sticky intents) with persistence -----------------
    # Set up storage for persisting overrides across restarts
    store = Store(hass, STORAGE_VERSION, f"{STORAGE_KEY}.{entry.entry_id}")

    # Load persisted overrides
    stored_data = await store.async_load()
    overrides: dict[str, dict] = stored_data.get("overrides", {}) if stored_data else {}

    if overrides:
        _LOGGER.info("Loaded %d persisted overrides from storage", len(overrides))

    # Callback to save overrides to storage
    async def save_overrides() -> None:
        """Persist overrides to storage."""
        await store.async_save({"overrides": overrides})
        _LOGGER.debug("Saved %d overrides to storage", len(overrides))

    # ---------- setup coordinator --------------------------------------------------
    # Callback to get current options from config entry
    def get_options() -> dict:
        return dict(entry.options)

    intuis_data = IntuisData(
        intuis_api,
        intuis_home,
        overrides,
        get_options,
        save_overrides_callback=save_overrides,
    )

    async def _async_update_data() -> dict:
        """Map API errors to the coordinator's expected exceptions."""
        try:
            return await intuis_data.async_update()
        except InvalidAuth as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except (APIError, CannotConnect, RateLimitError) as err:
            raise UpdateFailed(str(err)) from err

    coordinator: IntuisDataUpdateCoordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        config_entry=entry,
        name=DOMAIN,
        update_method=_async_update_data,
        update_interval=datetime.timedelta(minutes=DEFAULT_UPDATE_INTERVAL),
    )

    # Set up adaptive polling callback for rate limiting
    def on_rate_limit() -> None:
        """Handle rate limit event by increasing update interval."""
        current_minutes = coordinator.update_interval.total_seconds() / 60
        new_minutes = min(current_minutes * 2, max_update_interval)
        if new_minutes > current_minutes:
            coordinator.update_interval = datetime.timedelta(minutes=new_minutes)
            _LOGGER.warning(
                "Rate limited. Increased update interval from %.0f to %.0f minutes",
                current_minutes, new_minutes
            )

    intuis_api.set_rate_limit_callback(on_rate_limit)

    # Schedule periodic recovery of update interval
    async def recover_update_interval(_now=None) -> None:
        """Gradually recover update interval after successful updates."""
        if not intuis_api.circuit_breaker.is_open:
            current_minutes = coordinator.update_interval.total_seconds() / 60
            if current_minutes > DEFAULT_UPDATE_INTERVAL:
                new_minutes = max(current_minutes - 1, DEFAULT_UPDATE_INTERVAL)
                coordinator.update_interval = datetime.timedelta(minutes=new_minutes)
                _LOGGER.info(
                    "Recovering update interval from %.0f to %.0f minutes",
                    current_minutes, new_minutes
                )

    # Store recovery function for later use
    intuis_data.set_success_callback(recover_update_interval)

    await coordinator.async_config_entry_first_refresh()

    # ---------- store everything ---------------------------------------------------
    _LOGGER.debug("Storing data for entry %s", entry.entry_id)
    hass.data[DOMAIN][entry.entry_id] = {
        "api": intuis_api,
        "coordinator": coordinator,
        "intuis_home": intuis_home,
        "overrides": overrides,
        "save_overrides": save_overrides,
    }
    _LOGGER.debug("Stored data for entry %s", entry.entry_id)

    # ---------- migrate orphaned _energy_cost entities (_2 fix) -------------------
    # Previous versions may have left a StatisticsMeta entry occupying the clean
    # entity_id (e.g. sensor.chambre_parents_energy_cost), causing the new sensor
    # to be registered as sensor.chambre_parents_energy_cost_2.
    # Migration: remove the orphaned StatisticsMeta rows so the new entity can
    # claim the clean entity_id on the next reload.
    cost_enabled = entry.options.get(CONF_ENERGY_COST_ENABLED, DEFAULT_ENERGY_COST_ENABLED)
    if cost_enabled:
        _async_migrate_cost_entity_ids(hass, intuis_home)

    # ---------- setup platforms ----------------------------------------------------
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _async_remove_legacy_home_device(hass, entry, intuis_home.id)

    # ---------- register planning HTML view (auto cache-busting) ------------------
    # Guard: register only once across all entries (route cannot be registered twice)
    if not hass.data[DOMAIN].get("_planning_view_registered"):
        integration = await async_get_integration(hass, DOMAIN)
        hass.http.register_view(
            IntuisPlanningView(hass, _PLANNING_HTML, str(integration.version))
        )
        hass.data[DOMAIN]["_planning_view_registered"] = True

    # ---------- cancel any previous import and trigger new one if requested --------
    # Cancel any existing import from previous session to release resources
    if "import_managers" in hass.data.get(DOMAIN, {}):
        existing_manager = hass.data[DOMAIN]["import_managers"].get(entry.entry_id)
        if existing_manager and existing_manager.is_running:
            _LOGGER.info("Cancelling previous import that was still running")
            existing_manager.cancel()

    import_history = entry.options.get(CONF_IMPORT_HISTORY, False)
    import_days = entry.options.get(CONF_IMPORT_HISTORY_DAYS, 0)

    if import_history and import_days > 0:
        _LOGGER.info(
            "Historical energy import requested for %d days, starting in background",
            import_days,
        )

        # Create import manager
        manager = HistoryImportManager(hass, entry.entry_id)
        await manager.async_load()

        # Store manager for potential service access
        if "import_managers" not in hass.data[DOMAIN]:
            hass.data[DOMAIN]["import_managers"] = {}
        hass.data[DOMAIN]["import_managers"][entry.entry_id] = manager

        # Start import in a background task tied to the entry (cancelled on unload)
        entry.async_create_background_task(
            hass,
            async_import_energy_history(
                hass=hass,
                api=intuis_api,
                intuis_home=intuis_home,
                manager=manager,
                days=import_days,
                room_filter=None,
                home_id=entry.data["home_id"],
            ),
            name=f"intuis_connect history import {entry.entry_id}",
        )

        # Clear the import flag so it doesn't run again on reload
        new_options = {**entry.options, CONF_IMPORT_HISTORY: False}
        hass.config_entries.async_update_entry(entry, options=new_options)

    # ---------- start hourly stats updater ------------------------------------------
    # HourlyStatsUpdater - Periodic import of hourly energy statistics
    # This runs periodically to fetch hourly energy data and inject it into HA statistics
    hourly_stats_enabled = entry.options.get(
        CONF_HOURLY_STATS_ENABLED, DEFAULT_HOURLY_STATS_ENABLED
    )
    hourly_stats_interval = entry.options.get(
        CONF_HOURLY_STATS_INTERVAL, DEFAULT_HOURLY_STATS_INTERVAL
    )
    
    if hourly_stats_enabled:
        # Get timezone from HA config or default to Europe/Paris
        timezone_str = hass.config.time_zone or "Europe/Paris"
        
        hourly_stats_updater = HourlyStatsUpdater(
            hass=hass,
            api=intuis_api,
            intuis_home=intuis_home,
            entry_id=entry.entry_id,
            home_id=entry.data["home_id"],
            update_interval_minutes=hourly_stats_interval,
            timezone_str=timezone_str,
        )
        hass.data[DOMAIN][entry.entry_id]["hourly_stats_updater"] = hourly_stats_updater
        # Load storage NOW so base_kwh is available before sensors start reporting
        await hourly_stats_updater.load_storage()

        # CostStatsUpdater — wired to HourlyStatsUpdater so cost is updated
        # at the same cadence as energy stats.
        cost_enabled = entry.options.get(CONF_ENERGY_COST_ENABLED, DEFAULT_ENERGY_COST_ENABLED)
        if cost_enabled:
            cost_updater = CostStatsUpdater(
                hass=hass,
                intuis_home=intuis_home,
                entry_id=entry.entry_id,
                options=dict(entry.options),
                timezone_str=timezone_str,
            )
            await cost_updater.load_storage()
            cost_updater.publish_cost_base()
            hourly_stats_updater.cost_updater = cost_updater
            hass.data[DOMAIN][entry.entry_id]["cost_stats_updater"] = cost_updater
            _LOGGER.info("Energy cost statistics updater enabled")

        await hourly_stats_updater.async_start()
        
        # Schedule daily rebase at reset_hour+1 local time.
        # reset_hour is the API's finalization time (default 02:00, configurable).
        # Running 1h after ensures J-1 data is fully available in the API.
        _LOGGER.info(
            "Hourly energy statistics updater started (interval: %d min, tz: %s)",
            hourly_stats_interval,
            timezone_str,
        )
    else:
        _LOGGER.debug("Hourly energy statistics updater disabled by configuration")

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    _LOGGER.debug("Unloading entry %s", entry.entry_id)
    
    # Stop hourly stats updater
    entry_data = hass.data[DOMAIN].get(entry.entry_id, {})
    hourly_stats_updater = entry_data.get("hourly_stats_updater")
    if hourly_stats_updater:
        await hourly_stats_updater.async_stop()
        _LOGGER.debug("Stopped hourly stats updater for entry %s", entry.entry_id)
    
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)
        hass.data[DOMAIN].get(_OPTIONS_SNAPSHOTS, {}).pop(entry.entry_id, None)
        _LOGGER.debug("Unloaded entry %s", entry.entry_id)
    return unload_ok


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate config entry to a newer version.

    Handles migration from V2 to V3:
    - Adds CONF_HOME_NAME if missing (for multi-home support)
    """
    _LOGGER.debug("Migrating entry %s from version %s", entry.entry_id, entry.version)

    if entry.version < 3:
        # V2 -> V3: Add home_name if missing
        new_data = {**entry.data}

        if CONF_HOME_NAME not in new_data or not new_data.get(CONF_HOME_NAME):
            home_id = new_data.get(CONF_HOME_ID, "")
            home_name = None

            # Try to fetch the home name from the API
            try:
                session = async_get_clientsession(hass)
                api = IntuisAPI(session, home_id=home_id)
                api.refresh_token = new_data.get(CONF_REFRESH_TOKEN)
                await api.async_refresh_access_token()

                homes = await api.async_get_all_homes()
                for home in homes:
                    if home["id"] == home_id:
                        home_name = home["name"]
                        break

                _LOGGER.info(
                    "Migration: Fetched home name '%s' for home %s",
                    home_name,
                    home_id,
                )
            except (InvalidAuth, CannotConnect, APIError) as err:
                _LOGGER.warning(
                    "Migration: Could not fetch home name from API: %s",
                    err,
                )
            except TimeoutError:
                _LOGGER.warning(
                    "Migration: Timeout while fetching home name from API"
                )
            except Exception as err:
                _LOGGER.warning(
                    "Migration: Unexpected error fetching home name: %s",
                    err,
                )

            # Fall back to truncated home_id if API fails
            if not home_name:
                home_name = f"Home {home_id[:8]}" if home_id else "Unknown Home"
                _LOGGER.info(
                    "Migration: Using fallback home name '%s'",
                    home_name,
                )

            new_data[CONF_HOME_NAME] = home_name

        hass.config_entries.async_update_entry(
            entry,
            data=new_data,
            version=3,
        )
        _LOGGER.info(
            "Migrated entry %s to version 3 with home_name: %s",
            entry.entry_id,
            new_data.get(CONF_HOME_NAME),
        )

    return True

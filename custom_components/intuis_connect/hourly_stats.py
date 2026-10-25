"""Hourly statistics updater for Intuis Connect.

Exact replication of the Node-RED "Prépare les imports (cumul kWh + dédup)" logic.

Architecture:
- Fetches hourly energy from Intuis API: J-1 00:00 local -> now (25-48 h),
  or from the day after the last imported hour when HA was down longer
- Only imports complete hours, IMPORT_SETTLE_MINUTES after they ended, so a
  run never writes an hour the recorder may be compiling at the same moment
- Chains cumulative sums from the recorder's own sum just before the window,
  so an entity rename or a lost/rolled-back storage file cannot restart the
  counter at 0; persistent midnight_bases are only a fallback
- Imports via recorder.async_import_statistics (source="recorder", idempotent)
- Clears the range before importing -> overwrites any recorder auto-stats
- midnight_bases[statistic_id][day_key] = cumulative sum at that day's 00:00 local
  Updated every run as midnight boundaries are crossed.  Pruned to 270 days.
  Allows partial reimport (e.g. last 3 days) to anchor correctly without DB queries.

The matching sensor (sensor.X_energy) must NOT have state_class set,
otherwise the recorder generates competing statistics and the Energy
Dashboard shows spikes.  hourly_stats is the sole writer of statistics
for these entities.

Cumulative-sum algorithm:
  sum(hour) = base + kWh since the window start, where base is the recorder
  sum before the window (fallback: midnight_bases, then the last anchor).
  state = sum = cumulative total (matches Node-RED convention)
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    async_import_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.const import UnitOfEnergy
from homeassistant.helpers.storage import Store

from .utils.const import DOMAIN, HISTORY_IMPORT_KEY

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .entity.intuis_home import IntuisHome
    from .intuis_api.api import IntuisAPI

_LOGGER = logging.getLogger(__name__)

# Storage
STORAGE_VERSION = 1
STORAGE_KEY_PREFIX = "intuis_connect.hourly_stats_v3_"

# Constants
HOUR_MS = 3_600_000   # 1 h in milliseconds
HOUR_S = 3600         # 1 h in seconds
MIDNIGHT_BASES_MAX_DAYS = 270   # prune entries older than this
# An hour is imported once it ended this long ago (the recorder compiles the
# previous hour a few seconds after the top of the hour)
IMPORT_SETTLE_MINUTES = 15
# After a long downtime, re-fetch at most this many days (older gaps need
# the import_energy_history service)
MAX_CATCHUP_DAYS = 7
# How far back to look for the recorder sum preceding the import window
# when the hours just before it are missing
BASE_LOOKBACK_DAYS = 400


def _row_start_ts(row: dict[str, Any]) -> float | None:
    """Return a statistics row start as a UTC timestamp (float or datetime rows)."""
    start = row.get("start")
    if start is None:
        return None
    return start.timestamp() if hasattr(start, "timestamp") else float(start)


class HourlyStatsUpdater:
    """Imports hourly energy statistics from Intuis API into HA recorder.

    Replicates the Node-RED flow:
      1. Fetch hourly data J-1 00:00 local -> now
      2. For each room, compute cumulative sum with continuity priority
      3. Clear + import all stats (idempotent)
      4. Persist anchors (prevSum, prevStart) and base_kwh for next run
    """

    def __init__(
        self,
        hass: HomeAssistant,
        api: IntuisAPI,
        intuis_home: IntuisHome,
        entry_id: str,
        home_id: str | None = None,
        update_interval_minutes: int = 60,
        timezone_str: str = "Europe/Paris",
    ) -> None:
        self._hass = hass
        self._api = api
        self._intuis_home = intuis_home
        self._entry_id = entry_id
        self._home_id = home_id or intuis_home.id
        self._update_interval = update_interval_minutes
        self._timezone = ZoneInfo(timezone_str)
        self._timezone_str = timezone_str

        # Persistent storage (survives restarts)
        self._store = Store(hass, STORAGE_VERSION, f"{STORAGE_KEY_PREFIX}{entry_id}")
        self._data: dict[str, Any] = {}
        self._loaded = False

        # Background task
        self._update_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._update_lock = asyncio.Lock()  # prevents concurrent async_update() runs

        # Optional cost stats updater (set after construction by __init__.py)
        self.cost_updater = None

    # -- Storage -----------------------------------------------------------

    async def load_storage(self) -> None:
        if self._loaded:
            return
        stored = await self._store.async_load()
        if stored:
            self._data = stored
            # Migrate old format (base_kwh + sensor_base) to midnight_bases
            if "base_kwh" in self._data or "sensor_base" in self._data:
                self._migrate_to_midnight_bases()
        else:
            self._data = {
                "midnight_bases": {},  # {statistic_id: {day_key: float}}
                "anchors": {},         # {statistic_id: {"prev_sum": float, "prev_start": int ms}}
            }
        self._loaded = True
        _LOGGER.debug("Loaded hourly stats storage: %s rooms tracked",
                       len(self._data.get("anchors", {})))
        # Publish immediately so sensors have data on startup
        self._publish_sensor_base()

    def _migrate_to_midnight_bases(self) -> None:
        """Migrate old base_kwh + sensor_base storage to midnight_bases dict."""
        from datetime import timedelta
        now_local = datetime.now(self._timezone)
        today_key = now_local.strftime("%Y-%m-%d")
        yesterday_key = (now_local - timedelta(days=1)).strftime("%Y-%m-%d")

        mb = self._data.setdefault("midnight_bases", {})
        base_kwh = self._data.pop("base_kwh", {})
        sensor_base = self._data.pop("sensor_base", {})

        for sid, val in sensor_base.items():
            if val:
                mb.setdefault(sid, {})[today_key] = float(val)
        for sid, val in base_kwh.items():
            if val:
                mb.setdefault(sid, {})[yesterday_key] = float(val)

        # Clean up legacy keys
        self._data.pop("rebase_done", None)
        _LOGGER.info("Migrated storage: base_kwh+sensor_base → midnight_bases for %d sensors", len(mb))

    async def _save_storage(self) -> None:
        """Save storage, pruning midnight_bases entries older than MIDNIGHT_BASES_MAX_DAYS."""
        cutoff = (datetime.now(self._timezone) - timedelta(days=MIDNIGHT_BASES_MAX_DAYS)).strftime("%Y-%m-%d")
        mb = self._data.get("midnight_bases", {})
        for sid in mb:
            mb[sid] = {k: v for k, v in mb[sid].items() if k >= cutoff}
        await self._store.async_save(self._data)

    def _publish_sensor_base(self) -> None:
        """Publish today's midnight sum to hass.data[DOMAIN]["sensor_base"].

        sensor.py reads hass.data[DOMAIN]["sensor_base"][entity_id] to compute:
            native_value = sensor_base + daily_energy

        Falls back to anchor prev_sum if today's midnight not yet captured
        (e.g. first run of the day before midnight boundary is processed).
        """
        if DOMAIN not in self._hass.data:
            self._hass.data[DOMAIN] = {}

        today_key = datetime.now(self._timezone).strftime("%Y-%m-%d")
        mb = self._data.get("midnight_bases", {})
        anchors = self._data.get("anchors", {})

        sensor_base: dict[str, float] = {}
        for sid, by_day in mb.items():
            val = by_day.get(today_key)
            if val is not None:
                sensor_base[sid] = val
            elif sid in anchors:
                # Fallback: last known anchor sum until today midnight is captured
                sensor_base[sid] = float(anchors[sid].get("prev_sum", 0))

        # Merge: other homes' updaters publish their own sensors here too
        self._hass.data[DOMAIN].setdefault("sensor_base", {}).update(sensor_base)

    # -- Entity lookup -----------------------------------------------------

    def _get_statistic_id(self, room_id: str) -> str | None:
        """Return the entity_id (= statistic_id) for a room's energy sensor."""
        from homeassistant.helpers import entity_registry as er

        unique_id = f"intuis_{self._intuis_home.id}_{room_id}_energy"
        ent_reg = er.async_get(self._hass)
        entity_id = ent_reg.async_get_entity_id("sensor", DOMAIN, unique_id)

        if not entity_id:
            room = self._intuis_home.rooms.get(room_id)
            _LOGGER.warning(
                "Entity not found: unique_id=%s, room=%s",
                unique_id, room.name if room else room_id,
            )
        return entity_id

    # -- Helpers -----------------------------------------------------------

    @staticmethod
    def _snap_to_hour_ms(timestamp_sec: float) -> int:
        """Snap a Unix timestamp (seconds) to the top of the hour, return ms."""
        ms = int(timestamp_sec * 1000)
        return (ms // HOUR_MS) * HOUR_MS

    def _local_day_key(self, timestamp_ms: int) -> str:
        """YYYY-MM-DD in local timezone from milliseconds."""
        dt = datetime.fromtimestamp(timestamp_ms / 1000, tz=self._timezone)
        return dt.strftime("%Y-%m-%d")

    def _local_hour(self, timestamp_ms: int) -> int:
        """Hour (0-23) in local timezone from milliseconds."""
        dt = datetime.fromtimestamp(timestamp_ms / 1000, tz=self._timezone)
        return dt.hour

    @staticmethod
    def _complete_hours(
        hourly_data: list[tuple[int, float]] | None, import_end_ms: int
    ) -> list[tuple[int, float]]:
        """Keep complete hours only, one point per hour, in time order.

        Duplicate points for the same hour keep the largest value: summing them
        would double count overlapping API segments.
        """
        by_hour: dict[int, tuple[int, float]] = {}
        for timestamp_sec, wh in hourly_data or []:
            start_ms = HourlyStatsUpdater._snap_to_hour_ms(timestamp_sec)
            if start_ms + HOUR_MS > import_end_ms:
                continue
            previous = by_hour.get(start_ms)
            if previous is None or (wh or 0) > (previous[1] or 0):
                by_hour[start_ms] = (timestamp_sec, wh)
        return [by_hour[k] for k in sorted(by_hour)]

    async def _async_window_and_base(
        self, statistic_id: str, default_start: datetime
    ) -> tuple[datetime, float | None]:
        """Return the import window start and the recorder sum just before it.

        The window starts at J-1 00:00, or at the local midnight following the
        last imported hour when that is older (capped to MAX_CATCHUP_DAYS).
        The base is None when the recorder has no row before the window.
        """
        instance = get_instance(self._hass)
        try:
            last = await instance.async_add_executor_job(
                get_last_statistics, self._hass, 1, statistic_id, False, {"sum"}
            )
            rows = last.get(statistic_id) or []
            window_start = default_start
            last_ts = _row_start_ts(rows[0]) if rows else None
            if last_ts is not None:
                resume = datetime.fromtimestamp(last_ts + HOUR_S, tz=self._timezone)
                resume_midnight = resume.replace(hour=0, minute=0, second=0, microsecond=0)
                earliest = default_start - timedelta(days=MAX_CATCHUP_DAYS - 1)
                if resume_midnight < window_start:
                    window_start = max(resume_midnight, earliest)
                    _LOGGER.info(
                        "%s: last imported hour is old, catching up from %s",
                        statistic_id, window_start.isoformat(),
                    )

            # Usually the previous hour exists: look back 2 days first, and
            # only scan the long range when there is a gap
            for lookback_days in (2, BASE_LOOKBACK_DAYS):
                before = await instance.async_add_executor_job(
                    statistics_during_period,
                    self._hass,
                    window_start - timedelta(days=lookback_days),
                    window_start,
                    {statistic_id},
                    "hour",
                    None,
                    {"sum"},
                )
                before_rows = before.get(statistic_id) or []
                if before_rows and before_rows[-1].get("sum") is not None:
                    return window_start, float(before_rows[-1]["sum"])
            return window_start, None
        except Exception as err:  # recorder unavailable: fall back to storage
            _LOGGER.warning("%s: could not read recorder sums: %s", statistic_id, err)
            return default_start, None

    # -- Main update -------------------------------------------------------

    async def async_update(self) -> int:
        """Fetch hourly data and import statistics. Returns count imported."""
        # Skip if history import is running
        import_flags = self._hass.data.get(DOMAIN, {}).get(HISTORY_IMPORT_KEY, {})
        if import_flags.get(self._home_id, False):
            _LOGGER.info("History import in progress - skipping hourly stats")
            return 0

        # Prevent concurrent runs (e.g. scheduled loop + immediate post-import trigger)
        if self._update_lock.locked():
            _LOGGER.debug("Hourly stats update already running, skipping")
            return 0

        async with self._update_lock:

            await self.load_storage()

            # Auto-initialize midnight_bases from DB if empty (first run or storage cleared)
            mb = self._data.get("midnight_bases", {})
            if not mb or all(not v for v in mb.values()):
                _LOGGER.info("midnight_bases is empty — auto-initializing from existing statistics")
                await self.async_initialize_base_from_statistics()

            # Time window: J-1 00:00 local -> now (earlier after a long downtime)
            now_local = datetime.now(self._timezone)
            today_midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
            yesterday_midnight = today_midnight - timedelta(days=1)
            date_end = int(now_local.timestamp())
            import_end_ms = int((now_local.timestamp() - IMPORT_SETTLE_MINUTES * 60) * 1000)

            _LOGGER.info("Fetching hourly data %s -> %s",
                          yesterday_midnight.isoformat(), now_local.isoformat())

            # Let imports queued by a previous run or a history import reach the
            # database before reading the sums this run chains from.
            await get_instance(self._hass).async_block_till_done()

            total_imported = 0
            room_hourly_data: dict[str, list[tuple[int, float]]] = {}
            try:
                for room_id, room in self._intuis_home.rooms.items():
                    try:
                        statistic_id = self._get_statistic_id(room_id)
                        if not statistic_id:
                            continue
                        window_start, base_sum = await self._async_window_and_base(
                            statistic_id, yesterday_midnight
                        )
                        hourly_data = await self._api.async_get_room_energy_hourly(
                            room_id=room_id,
                            date_begin=int(window_start.timestamp()),
                            date_end=date_end,
                        )
                        hourly_data = self._complete_hours(hourly_data, import_end_ms)
                        if not hourly_data:
                            _LOGGER.debug("No complete hourly data for room %s", room_id)
                            continue
                        room_hourly_data[room_id] = hourly_data
                        total_imported += await self._process_room(
                            room_id, hourly_data, base_sum=base_sum
                        )
                    except Exception as room_err:
                        _LOGGER.warning("Error for room %s: %s", room_id, room_err)

                await self._save_storage()

                # Publish today midnight sums to hass.data so sensors can read them
                self._publish_sensor_base()

                _LOGGER.info("Imported %d hourly statistics", total_imported)
                # Signal sensors that LTS is fresh — they can now publish their state.
                self._hass.data[DOMAIN].setdefault("hourly_stats_ready", set()).add(self._home_id)

                # Update cost statistics if a cost updater is configured
                if self.cost_updater is not None and room_hourly_data:
                    try:
                        cost_count = await self.cost_updater.async_update(room_hourly_data)
                        self.cost_updater.publish_cost_base()
                        if cost_count:
                            _LOGGER.info("Imported %d cost statistics", cost_count)
                    except Exception as cost_err:
                        _LOGGER.warning("Cost stats update failed: %s", cost_err)

                return total_imported

            except Exception as err:
                _LOGGER.error("Error in hourly stats update: %s", err, exc_info=True)
                return 0

    # -- Process one room (Node-RED "Prepare les imports" logic) -----------

    async def _process_room(
        self,
        room_id: str,
        hourly_data: list[tuple[int, float]],
        base_sum: float | None = None,
    ) -> int:
        """Process hourly data for a single room.

          - The first day of the window starts from base_sum, the recorder sum
            just before the window; without it, from midnight_bases, then the
            last anchor, then 0
          - Later days chain from the sum reached at the end of the previous day
          - state = sum = cumulative total (Node-RED convention)
          - change is auto-calculated by HA as sum[n]-sum[n-1]
          - Always rewrite all hours (idempotent)

        midnight_bases is updated for every midnight boundary encountered; the
        dict is shared with history_import and sensor.py.
        """
        if not hourly_data:
            return 0

        statistic_id = self._get_statistic_id(room_id)
        if not statistic_id:
            return 0

        # --- Persistent anchors (from previous run) ---
        anchors = self._data.get("anchors", {}).get(statistic_id, {})
        prev_sum = anchors.get("prev_sum")
        prev_start = anchors.get("prev_start")  # ms

        # --- midnight_bases for this sensor ---
        mb_all = self._data.setdefault("midnight_bases", {})
        mb = mb_all.setdefault(statistic_id, {})   # {day_key: float}

        if prev_sum is not None:
            prev_sum = float(prev_sum)
        if prev_start is not None:
            prev_start = int(prev_start)

        _LOGGER.debug(
            "%s: prev_sum=%s, prev_start=%s",
            statistic_id,
            f"{prev_sum:.3f}" if prev_sum is not None else "None",
            prev_start,
        )

        # --- Build stats ---
        run_kwh_from_midnight = 0.0
        last_day_key = None
        day_base_cache: dict[str, float] = {}
        stats = []
        # Sum reached by the previous point of this run (None before the first)
        loop_prev_sum: float | None = None

        for timestamp_sec, wh in hourly_data:
            # Validate
            if wh is None or not isinstance(wh, (int, float)) or wh != wh:
                continue
            if wh < 0:
                continue

            # Snap to top of hour (HA requires mm:ss = 00:00)
            start_ms = self._snap_to_hour_ms(timestamp_sec)
            start_dt = datetime.fromtimestamp(start_ms / 1000, tz=self._timezone)

            # Local day management
            day_key = self._local_day_key(start_ms)

            if last_day_key is None or day_key != last_day_key:
                run_kwh_from_midnight = 0.0
                last_day_key = day_key

            if day_key not in day_base_cache:
                if loop_prev_sum is not None:
                    # Crossed midnight in this run: the day starts where the
                    # previous one ended (missing hours count as 0 kWh)
                    day_base = loop_prev_sum
                elif base_sum is not None:
                    # First day: continue from what the recorder already holds
                    day_base = base_sum
                else:
                    # No recorder history: persistent midnight base, then anchor
                    fallback = float(mb.get(day_key, 0.0))
                    if fallback > 0:
                        day_base = fallback
                    elif prev_sum is not None and prev_sum > 0:
                        day_base = prev_sum
                    else:
                        day_base = 0.0
                day_base_cache[day_key] = day_base
                # The window starts at a local midnight, so every day base is
                # that day's midnight sum
                if day_base > 0:
                    mb[day_key] = round(day_base, 3)

            # Hourly consumption Wh -> kWh
            kwh = wh / 1000.0
            run_kwh_from_midnight += kwh

            cumul_sum = round(day_base_cache[day_key] + run_kwh_from_midnight, 3)

            stats.append(StatisticData(
                start=start_dt,
                state=cumul_sum,
                sum=cumul_sum,
            ))

            # Advance anchor for next point
            loop_prev_sum = cumul_sum
            prev_sum = cumul_sum
            prev_start = start_ms

        if not stats:
            _LOGGER.debug("%s: no valid data points", statistic_id)
            return 0

        # --- Update persistent anchors ---
        last_stat = stats[-1]
        if "anchors" not in self._data:
            self._data["anchors"] = {}
        self._data["anchors"][statistic_id] = {
            "prev_sum": last_stat["sum"],
            "prev_start": int(last_stat["start"].timestamp() * 1000),
        }

        # --- Import (upsert) ---
        # async_import_statistics with source="recorder" does UPSERT on (metadata_id, start_ts).
        # No need to clear first: existing records at the same timestamps are updated in place.
        # Clearing before import caused "database is locked" errors because the recorder's
        # thread pool has multiple workers (DbWorker_0/1/2) - the DELETE and the pending
        # INSERTs from the previous room would race on the SQLite write lock.
        room = self._intuis_home.rooms.get(room_id)
        room_name = room.name if room else ("Room " + room_id)

        # mean_type + unit_class required from HA 2026.11 - try/except for backward compat
        try:
            from homeassistant.components.recorder.models import StatisticMeanType
            metadata = StatisticMetaData(
                mean_type=StatisticMeanType.NONE,
                has_sum=True,
                name=(room_name + " Energy"),
                source="recorder",
                statistic_id=statistic_id,
                unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
                unit_class="energy",
            )
        except (ImportError, TypeError):
            metadata = StatisticMetaData(
                has_mean=False,
                has_sum=True,
                name=(room_name + " Energy"),
                source="recorder",
                statistic_id=statistic_id,
                unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            )

        try:
            async_import_statistics(self._hass, metadata, stats)
            _LOGGER.info(
                "%s: imported %d hours [%s sum=%.3f -> %s sum=%.3f]",
                statistic_id, len(stats),
                stats[0]["start"].isoformat(), stats[0]["sum"],
                stats[-1]["start"].isoformat(), stats[-1]["sum"],
            )
            return len(stats)
        except Exception as err:
            _LOGGER.error("Import failed for %s: %s", statistic_id, err)
            return 0

    # -- Daily rebase (REMOVED) --------------------------------------------
    # midnight_bases is updated incrementally by _process_room at every
    # midnight boundary.  No separate daily rebase needed.

    # -- Initialize base from history import --------------------------------

    async def async_set_bases_from_import(
        self, midnight_sums: dict[str, dict[str, float]]
    ) -> None:
        """Populate midnight_bases after history_import finishes.

        Args:
            midnight_sums: {statistic_id: {
                "by_day": {day_key: float, ...},   # all midnight sums from import
                "sensor_base": float,  # today midnight (legacy key, optional)
                "base_kwh": float,     # J-1 midnight (legacy key, optional)
                "final_sum": float,
            }}
        """
        await self.load_storage()

        now_local = datetime.now(self._timezone)
        today_key = now_local.strftime("%Y-%m-%d")
        yesterday_key = (now_local - timedelta(days=1)).strftime("%Y-%m-%d")
        today_midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        today_midnight_ms = int(today_midnight.astimezone(ZoneInfo("UTC")).timestamp() * 1000)

        mb_all = self._data.setdefault("midnight_bases", {})

        for statistic_id, sums in midnight_sums.items():
            mb = mb_all.setdefault(statistic_id, {})

            # Prefer new "by_day" dict (all midnights from import window)
            by_day = sums.get("by_day", {})
            if by_day:
                mb.update({k: round(v, 3) for k, v in by_day.items() if v})
            else:
                # Legacy keys (base_kwh = J-1 midnight, sensor_base = today midnight)
                base = sums.get("base_kwh")
                sb = sums.get("sensor_base")
                if base:
                    mb[yesterday_key] = round(base, 3)
                if sb:
                    mb[today_key] = round(sb, 3)

            # Anchor: set to today midnight so next cycle continues cleanly
            today_sb = mb.get(today_key, sums.get("sensor_base", 0.0) or sums.get("final_sum", 0.0))
            self._data.setdefault("anchors", {})[statistic_id] = {
                "prev_sum": round(float(today_sb), 3),
                "prev_start": today_midnight_ms,
            }

            _LOGGER.info(
                "Post-import %s: %d midnight bases loaded, anchor at today midnight=%.3f",
                statistic_id, len(mb), float(today_sb),
            )

        await self._save_storage()
        self._publish_sensor_base()
        _LOGGER.info("Post-import: midnight_bases set for %d sensors", len(midnight_sums))

    async def async_initialize_base_from_statistics(self) -> None:
        """Initialize midnight_bases from existing HA statistics.

        Scans backwards through midnights (today back to J-4) to populate
        midnight_bases[statistic_id][day_key] for all found midnights.
        This is called once when storage is empty (first run, or after
        storage was cleared).  After this, _process_room maintains
        midnight_bases incrementally.
        """
        from homeassistant.components.recorder.statistics import (
            statistics_during_period,
        )

        await self.load_storage()

        now = datetime.now(self._timezone)
        start_time = now - timedelta(days=200)
        today_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)

        # Scan last 5 midnights (today back to J-4)
        midnights = []
        for days_ago in range(0, 5):
            m = today_midnight - timedelta(days=days_ago)
            label = ["today", "J-1", "J-2", "J-3", "J-4"][days_ago]
            midnights.append((label, m, m.strftime("%Y-%m-%d")))

        _LOGGER.info("Initializing midnight_bases from statistics")

        mb_all = self._data.setdefault("midnight_bases", {})

        for room_id in self._intuis_home.rooms:
            statistic_id = self._get_statistic_id(room_id)
            if not statistic_id:
                continue

            try:
                stats = await get_instance(self._hass).async_add_executor_job(
                    statistics_during_period,
                    self._hass,
                    start_time,
                    None,
                    {statistic_id},
                    "hour",
                    None,
                    {"sum"},
                )

                if statistic_id not in stats or not stats[statistic_id]:
                    _LOGGER.warning("No stats found for %s", statistic_id)
                    continue

                all_stats = stats[statistic_id]

                # Index sums by UTC timestamp
                sum_by_ts: dict[int, float] = {}
                last_sum = 0.0
                for s in all_stats:
                    s_start = s.get("start")
                    s_sum = s.get("sum", 0) or 0
                    if s_start is None:
                        continue
                    s_ts = s_start.timestamp() if hasattr(s_start, "timestamp") else float(s_start)
                    sum_by_ts[int(s_ts)] = float(s_sum)
                    last_sum = float(s_sum)

                mb = mb_all.setdefault(statistic_id, {})
                found_today = None

                for label, local_midnight, day_key in midnights:
                    midnight_utc_ts = int(local_midnight.astimezone(ZoneInfo("UTC")).timestamp())
                    # Stat ending AT midnight = stat starting 1h before midnight
                    stat_start_ts = midnight_utc_ts - HOUR_S
                    midnight_sum = sum_by_ts.get(stat_start_ts) or sum_by_ts.get(midnight_utc_ts)

                    if midnight_sum is None:
                        # Nearest prior stat fallback
                        for ts_k in sorted(sum_by_ts):
                            if ts_k <= stat_start_ts:
                                midnight_sum = sum_by_ts[ts_k]
                            else:
                                break

                    if midnight_sum is not None:
                        mb[day_key] = round(midnight_sum, 3)
                        _LOGGER.debug("Init %s: %s midnight=%.3f", statistic_id, label, midnight_sum)
                        if label == "today":
                            found_today = midnight_sum

                # Fallback anchor if today midnight not found
                anchor_base = found_today if found_today is not None else last_sum
                today_midnight_ms = int(today_midnight.astimezone(ZoneInfo("UTC")).timestamp() * 1000)
                self._data.setdefault("anchors", {})[statistic_id] = {
                    "prev_sum": round(anchor_base, 3),
                    "prev_start": today_midnight_ms,
                }

                _LOGGER.info("Init %s: %d midnight bases, anchor=%.3f",
                             statistic_id, len(mb), anchor_base)

            except Exception as err:
                _LOGGER.warning("Could not init %s: %s", statistic_id, err)

        await self._save_storage()
        self._publish_sensor_base()
        _LOGGER.info("Base initialization completed")

    # -- Lifecycle ---------------------------------------------------------

    async def async_start(self) -> None:
        if self._update_task is not None:
            _LOGGER.warning("Hourly stats updater already running")
            return
        self._stop_event.clear()
        # Tracked by Home Assistant, so it is cancelled cleanly at shutdown
        self._update_task = self._hass.async_create_background_task(
            self._update_loop(), name=f"intuis_connect hourly stats {self._entry_id}"
        )
        _LOGGER.info("Hourly stats updater started (interval: %d min)", self._update_interval)

    async def async_stop(self) -> None:
        if self._update_task is None:
            return
        self._stop_event.set()
        self._update_task.cancel()
        try:
            await self._update_task
        except asyncio.CancelledError:
            pass
        self._update_task = None
        _LOGGER.info("Hourly stats updater stopped")

    async def _update_loop(self) -> None:
        # Initial delay to let HA stabilise
        await asyncio.sleep(60)

        while not self._stop_event.is_set():
            try:
                await self.async_update()
            except asyncio.CancelledError:
                raise
            except Exception as err:
                _LOGGER.error("Error in hourly stats loop: %s", err, exc_info=True)

            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._update_interval * 60,
                )
                break
            except TimeoutError:
                pass

    async def async_rebase_daily(self) -> None:
        """No-op: midnight_bases is maintained incrementally by _process_room."""
        _LOGGER.debug("async_rebase_daily: no-op (midnight_bases maintained incrementally)")

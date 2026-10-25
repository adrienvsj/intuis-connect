"""Setup, unload and reload behaviour of the config entry on a real Home Assistant."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.intuis_connect.entity.intuis_home import IntuisHome
from custom_components.intuis_connect.intuis_api.api import (
    APIError,
    CannotConnect,
    IntuisAPI,
    InvalidAuth,
)
from custom_components.intuis_connect.utils.const import DOMAIN

HOME_ID = "home_1"

HOMESDATA = {
    "id": HOME_ID,
    "name": "Maison",
    "country": "FR",
    "timezone": "Europe/Paris",
    "coordinates": [2.35, 48.85],
    "rooms": [
        {"id": "room_1", "name": "Salon", "type": "livingroom", "module_ids": ["rad_1"]},
        {"id": "room_2", "name": "Bureau", "type": "office", "module_ids": ["rad_2"]},
    ],
    "schedules": [
        {
            "id": "sched_1",
            "name": "Semaine",
            "type": "therm",
            "selected": True,
            "away_temp": 12,
            "hg_temp": 7,
            "zones": [
                {"id": 0, "name": "Confort", "type": 0, "rooms_temp": [{"room_id": "room_1", "temp": 20}]},
                {"id": 1, "name": "Nuit", "type": 1, "rooms_temp": [{"room_id": "room_1", "temp": 17}]},
            ],
            "timetable": [{"zone_id": 1, "m_offset": 0}, {"zone_id": 0, "m_offset": 420}],
        }
    ],
}

HOMESTATUS = {
    "id": HOME_ID,
    "rooms": [
        {
            "id": "room_1",
            "therm_setpoint_mode": "home",
            "therm_setpoint_temperature": 20,
            "therm_measured_temperature": 19.5,
            # Intuitiv radiators can report an FP type and still take temperatures
            "muller_type": "FP4",
        },
        {
            # Pilot-wire radiator: no measured temperature
            "id": "room_2",
            "therm_setpoint_mode": "home",
            "therm_setpoint_fp": "comfort",
            "muller_type": "FP4",
        },
    ],
    "modules": [
        {"id": "gw_1", "type": "NMG"},
        {"id": "rad_1", "type": "NMH", "bridge": "gw_1", "radiator_state": "auto"},
        {"id": "rad_2", "type": "NMH", "bridge": "gw_1"},
    ],
}


@pytest.fixture(autouse=True)
def _custom_integrations(recorder_mock, enable_custom_integrations):
    """Let Home Assistant load the integration from custom_components."""


@pytest.fixture
def mock_api():
    """An API double whose async methods return the fixture payloads."""
    api = MagicMock(spec=IntuisAPI)
    api.home_id = HOME_ID
    api.async_refresh_access_token = AsyncMock()
    api.async_get_homes_data = AsyncMock(return_value=IntuisHome.from_api(HOMESDATA))
    api.async_get_home_status = AsyncMock(return_value=HOMESTATUS)
    api.async_get_config = AsyncMock(return_value={"modules": [{"id": "gw_1"}]})
    api.async_get_energy_measures = AsyncMock(return_value={})
    api.circuit_breaker = MagicMock(is_open=False)
    with patch("custom_components.intuis_connect.IntuisAPI", return_value=api):
        yield api


@pytest.fixture
def no_http(hass):
    """Satisfy the http dependency without starting a web server."""
    hass.config.components.add("http")
    hass.http = MagicMock()


def _entry(hass, **options) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=3,
        data={"username": "user@example.com", "refresh_token": "r", "home_id": HOME_ID, "home_name": "Maison"},
        options=options,
    )
    entry.add_to_hass(hass)
    return entry


async def test_setup_and_unload(hass, mock_api, no_http):
    """All platforms load from realistic payloads and the entry unloads cleanly."""
    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    climates = hass.states.async_entity_ids("climate")
    assert len(climates) == 2
    assert all(hass.states.get(c).state != "unavailable" for c in climates)
    # Temperature radiator keeps its dial; the pilot-wire one gets order presets
    by_target = {c: hass.states.get(c).attributes.get("temperature") for c in climates}
    assert sorted(v for v in by_target.values() if v is not None) == [20]
    assert sum(v is None for v in by_target.values()) == 1
    assert hass.services.has_service(DOMAIN, "set_schedule_slot")

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED


@pytest.mark.parametrize("error", [CannotConnect("down"), APIError("500")])
async def test_cloud_errors_retry_setup(hass, mock_api, no_http, error):
    """A cloud outage at startup is retried instead of failing for good."""
    mock_api.async_get_homes_data.side_effect = error
    entry = _entry(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_refused_token_starts_reauth(hass, mock_api, no_http):
    mock_api.async_refresh_access_token.side_effect = InvalidAuth("refused")
    entry = _entry(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert any(flow["context"]["source"] == "reauth" for flow in hass.config_entries.flow.async_progress())


async def test_only_the_options_flow_reloads(hass, mock_api, no_http):
    """Token updates and the history-import flag never reload; saving changed options does."""
    from homeassistant.data_entry_flow import FlowResultType

    entry = _entry(hass, import_history=True, import_history_days=0)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        hass.config_entries.async_update_entry(entry, data={**entry.data, "refresh_token": "new"})
        hass.config_entries.async_update_entry(entry, options={**entry.options, "import_history": False})
        await hass.async_block_till_done()
        reload.assert_not_called()

        result = await hass.config_entries.options.async_init(entry.entry_id)
        for _step in ("indefinite", "energy", "rate_limit"):
            result = await hass.config_entries.options.async_configure(result["flow_id"], {})
            if result["step_id"] == "overrides":
                result = await hass.config_entries.options.async_configure(
                    result["flow_id"], {"manual_duration": "120"}
                )
        while result["type"] is FlowResultType.FORM:
            result = await hass.config_entries.options.async_configure(result["flow_id"], {})
        assert result["type"] is FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done()
        reload.assert_called_once_with(entry.entry_id)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_reauth_updates_the_token_and_reloads(hass, mock_api, no_http):
    """A refused token leads to a reauth flow that stores the new token."""
    from homeassistant.data_entry_flow import FlowResultType

    mock_api.async_refresh_access_token.side_effect = InvalidAuth("refused")
    entry = _entry(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    flow = next(f for f in hass.config_entries.flow.async_progress() if f["context"]["source"] == "reauth")

    new_api = MagicMock(refresh_token="fresh")
    mock_api.async_refresh_access_token.side_effect = None
    with patch(
        "custom_components.intuis_connect.config_flow.async_validate_api",
        return_value=([{"id": HOME_ID, "name": "Maison"}], new_api),
    ):
        result = await hass.config_entries.flow.async_configure(flow["flow_id"], {"password": "new"})
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data["refresh_token"] == "fresh"
    assert entry.data["home_id"] == HOME_ID
    assert entry.state is ConfigEntryState.LOADED

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_schedule_devices_link_to_home(hass, mock_api, no_http):
    """Schedule devices hang under the home device without deprecated parameters."""
    from homeassistant.helpers import device_registry as dr

    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    dev_reg = dr.async_get(hass)
    home = dev_reg.async_get_device_by_identifier((DOMAIN, f"{HOME_ID}_home"), entry.entry_id)
    schedule = dev_reg.async_get_device_by_identifier(
        (DOMAIN, f"{HOME_ID}_schedule_sched_1"), entry.entry_id
    )
    assert home is not None and schedule is not None
    assert schedule.via_device_id == home.id

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_existing_installation_keeps_ids_and_cleans_devices(hass, mock_api, no_http):
    """2.2 display changes keep entity_ids; the duplicate home device goes away."""
    from homeassistant.const import EntityCategory
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er

    entry = _entry(hass)
    ent_reg, dev_reg = er.async_get(hass), dr.async_get(hass)
    legacy_device = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, HOME_ID)}, name="Intuis Home"
    )
    previous = {
        ("climate", f"intuis_{HOME_ID}_room_1_climate"): "salon_salon_climate",
        ("select", f"intuis_{HOME_ID}_schedule_select"): "intuis_home_active_schedule",
        ("sensor", f"intuis_{HOME_ID}_schedule_data"): "intuis_home_intuis_schedule_data",
    }
    for (domain, unique_id), object_id in previous.items():
        ent_reg.async_get_or_create(
            domain, DOMAIN, unique_id, suggested_object_id=object_id, config_entry=entry,
            device_id=legacy_device.id if domain == "sensor" else None,
        )

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    for (domain, unique_id), object_id in previous.items():
        assert ent_reg.async_get_entity_id(domain, DOMAIN, unique_id) == f"{domain}.{object_id}"
    assert hass.states.get("climate.salon_salon_climate").name == "Salon"

    home = dev_reg.async_get_device_by_identifier((DOMAIN, f"{HOME_ID}_home"), entry.entry_id)
    assert home.name == "Maison"
    assert dev_reg.async_get(legacy_device.id) is None
    data_sensor = ent_reg.async_get("sensor.intuis_home_intuis_schedule_data")
    assert data_sensor.device_id == home.id
    wifi = ent_reg.async_get_entity_id("sensor", DOMAIN, f"intuis_{HOME_ID}_gateway_wifi_strength")
    assert ent_reg.async_get(wifi).entity_category is EntityCategory.DIAGNOSTIC

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_diagnostics_redact_credentials(hass, mock_api, no_http):
    from custom_components.intuis_connect.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    entry = _entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    diag = await async_get_config_entry_diagnostics(hass, entry)
    text = str(diag)
    assert "user@example.com" not in text
    assert diag["entry"]["data"]["refresh_token"] == "**REDACTED**"
    assert set(diag["rooms"]) == {"room_1", "room_2"}
    assert diag["rooms"]["room_1"]["target_temperature"] == 20

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

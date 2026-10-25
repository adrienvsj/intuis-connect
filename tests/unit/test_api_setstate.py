"""Tests for the room setstate payload sent by IntuisAPI."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from freezegun import freeze_time

from custom_components.intuis_connect.intuis_api.api import IntuisAPI

NOW = 1791374400  # 2026-10-07 12:00:00 UTC


@pytest.fixture
def api() -> IntuisAPI:
    client = IntuisAPI(MagicMock(), home_id="home_1")
    client._async_request = AsyncMock()
    return client


def _sent_room(api: IntuisAPI) -> dict:
    return api._async_request.call_args.kwargs["json"]["home"]["rooms"][0]


@pytest.mark.asyncio
@freeze_time("2026-10-07 12:00:00")
async def test_manual_with_fp_sends_pilot_wire_order(api):
    await api.async_set_room_state("room_1", "manual", duration=120, fp="away")

    assert _sent_room(api) == {
        "id": "room_1",
        "therm_setpoint_mode": "manual",
        "therm_setpoint_fp": "away",
        "therm_setpoint_end_time": NOW + 120 * 60,
    }


@pytest.mark.asyncio
@freeze_time("2026-10-07 12:00:00")
async def test_manual_with_temperature_is_unchanged(api):
    await api.async_set_room_state("room_1", "manual", 21.0, 60)

    assert _sent_room(api) == {
        "id": "room_1",
        "therm_setpoint_mode": "manual",
        "therm_setpoint_temperature": 21.0,
        "therm_setpoint_end_time": NOW + 60 * 60,
    }

"""The imported statistic must be visible in the Energy Dashboard picker."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    list_statistic_ids,
)
from homeassistant.const import UnitOfEnergy
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.eredes.eredes_api.models import ConsumptionData
from custom_components.eredes.historical import (
    async_import_historical_data,
    statistic_id,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.typing import (
        RecorderInstanceContextManager,
    )

CPE = "PT0002000012345678AB"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    recorder_mock: RecorderInstanceContextManager,
    enable_custom_integrations: None,
) -> None:
    """Start the recorder before hass (overrides the conftest fixture)."""


async def _unit_class(hass: HomeAssistant) -> str | None:
    ids = await get_instance(hass).async_add_executor_job(
        list_statistic_ids, hass, {statistic_id(CPE)}
    )
    return ids[0]["unit_class"]


async def test_legacy_metadata_is_fixed_without_new_hours(
    hass: HomeAssistant,
    recorder_mock: RecorderInstanceContextManager,  # noqa: ARG001
) -> None:
    """A statistic stored with unit_class=None gets unit_class=energy."""
    start = (datetime.now(UTC) - timedelta(hours=2)).replace(
        minute=0, second=0, microsecond=0
    )
    async_add_external_statistics(
        hass,
        {
            "has_mean": False,
            "has_sum": True,
            "mean_type": StatisticMeanType.NONE,
            "name": "E-REDES Energy (345678AB)",
            "source": "eredes",
            "statistic_id": statistic_id(CPE),
            "unit_class": None,
            "unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
        },
        [{"start": start, "state": 1.0, "sum": 10.0}],
    )
    await async_wait_recording_done(hass)
    assert await _unit_class(hass) is None

    now = datetime.now()
    coordinator = MagicMock(cpe=CPE)
    coordinator.client.get_consumption = AsyncMock(
        return_value=ConsumptionData(cpe=CPE, readings=[], start_date=now, end_date=now)
    )
    await async_import_historical_data(hass, coordinator)
    await async_wait_recording_done(hass)

    assert await _unit_class(hass) == "energy"

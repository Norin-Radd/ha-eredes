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
    async_import_statistics,
    get_last_statistics,
    list_statistic_ids,
)
from homeassistant.const import UnitOfEnergy
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.eredes.eredes_api.models import (
    ConsumptionData,
    ConsumptionReading,
)
from custom_components.eredes.historical import (
    PriceConfig,
    _price_factor,
    async_import_historical_data,
    cost_statistic_id,
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


async def test_cost_is_priced_with_the_price_sensor_history(
    hass: HomeAssistant,
) -> None:
    """Each hour's kWh is multiplied by the price sensor's mean for that hour."""
    hour0 = (datetime.now(UTC) - timedelta(days=2)).replace(
        minute=0, second=0, microsecond=0
    )
    hour1 = hour0 + timedelta(hours=1)
    hour2 = hour0 + timedelta(hours=2)  # no price recorded for this one
    hass.states.async_set("sensor.preco", "0.2", {"unit_of_measurement": "€/kWh"})
    async_import_statistics(
        hass,
        {
            "has_mean": True,
            "has_sum": False,
            "mean_type": StatisticMeanType.ARITHMETIC,
            "name": "Preço",
            "source": "recorder",
            "statistic_id": "sensor.preco",
            "unit_class": None,
            "unit_of_measurement": "€/kWh",
        },
        [
            {"start": hour0, "mean": 0.10, "min": 0.10, "max": 0.10},
            {"start": hour1, "mean": 0.20, "min": 0.20, "max": 0.20},
        ],
    )
    await async_wait_recording_done(hass)

    readings = [
        ConsumptionReading(timestamp=h + timedelta(minutes=m), value_wh=500.0)
        for h in (hour0, hour1, hour2)
        for m in (0, 15, 30, 45)
    ]  # 2 kWh per hour
    now = datetime.now()
    coordinator = MagicMock(cpe=CPE)
    coordinator.client.get_consumption = AsyncMock(
        return_value=ConsumptionData(
            cpe=CPE, readings=readings, start_date=now, end_date=now
        )
    )
    await async_import_historical_data(
        hass, coordinator, PriceConfig(source="sensor", entity="sensor.preco")
    )
    await async_wait_recording_done(hass)

    cost = await get_instance(hass).async_add_executor_job(
        get_last_statistics, hass, 1, cost_statistic_id(CPE), True, {"sum"}
    )
    row = cost[cost_statistic_id(CPE)][0]
    assert row["start"] == hour1.timestamp()  # hour2 skipped: no price
    assert row["sum"] == pytest.approx(2 * 0.10 + 2 * 0.20)


@pytest.mark.parametrize(
    ("unit", "factor"),
    [
        ("€/kWh", 1.0),
        ("EUR/kWh", 1.0),
        ("€/MWh", 0.001),
        ("c€/kWh", 0.01),
        ("ct/kWh", 0.01),
        (None, 1.0),
    ],
)
def test_price_factor(unit: str | None, factor: float) -> None:
    """Price units are normalised to <currency>/kWh."""
    assert _price_factor(unit) == pytest.approx(factor)

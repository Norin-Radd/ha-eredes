"""Backfill of E-REDES history into a real-time sensor, and the live price."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    async_import_statistics,
    statistics_during_period,
)
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.eredes.backfill import build_backfill
from custom_components.eredes.const import CONF_ACCESS_TOKEN, CONF_CPE, DOMAIN
from custom_components.eredes.eredes_api import ERedesConnectionError
from custom_components.eredes.historical import statistic_id
from custom_components.eredes.price_sensor import (
    monthly_allowance_kwh,
    quarter_start,
    vat_rate,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.typing import (
        RecorderInstanceContextManager,
    )

H = 3600
T0 = int(datetime(2026, 10, 1, tzinfo=UTC).timestamp())


def _src(*hours: tuple[int, float]) -> list[dict[str, float]]:
    return [{"start": T0 + h * H, "state": kwh} for h, kwh in hours]


def _tgt(*rows: tuple[int, float, float]) -> list[dict[str, float]]:
    return [{"start": T0 + h * H, "sum": s, "state": st} for h, s, st in rows]


def test_backfill_keeps_continuity_with_later_rows() -> None:
    """The last rewritten row carries the target's sum before the junction."""
    source = _src((0, 1.0), (1, 2.0), (2, 3.0))  # junction at hour 3
    # HAN: bad history at 1-2 (an outage spike), good data from hour 3 on.
    target = _tgt((1, 50.0, 1050.0), (2, 100.0, 1100.0), (3, 101.0, 1101.0))
    stats, summary = build_backfill(source, target)

    assert [s["start"].timestamp() for s in stats] == [T0, T0 + H, T0 + 2 * H]
    assert [s["sum"] for s in stats] == [95.0, 97.0, 100.0]  # 100 - 3 - 2
    assert stats[-1]["sum"] == 100.0  # hour 3 (kept) still adds 101 - 100 = 1
    assert [s["state"] for s in stats] == [1095.0, 1097.0, 1100.0]
    assert summary["hours_from_eredes"] == 3
    assert summary["junction"] == datetime.fromtimestamp(T0 + 3 * H, UTC).isoformat()


def test_backfill_keeps_target_values_where_eredes_has_none() -> None:
    """An hour missing in E-REDES keeps the target's own consumption."""
    source = _src((0, 1.0), (2, 3.0))
    target = _tgt((0, 10.0, 10.0), (1, 15.0, 15.0), (2, 20.0, 20.0))
    stats, summary = build_backfill(source, target)
    deltas = [b["sum"] - a["sum"] for a, b in itertools.pairwise(stats)]
    assert deltas == [5.0, 3.0]  # hour 1 from HAN, hour 2 from E-REDES
    assert summary["hours_kept_from_target"] == 1


def test_backfill_converts_units_and_needs_data() -> None:
    """kWh from E-REDES becomes Wh for a Wh sensor; no data is an error."""
    stats, _ = build_backfill(_src((0, 1.5)), [], factor=1000.0)
    assert stats[0]["sum"] == 0.0
    with pytest.raises(HomeAssistantError):
        build_backfill([], [])


def test_vat_rule_and_allowance() -> None:
    """6% below the monthly allowance, 23% above; no reading -> 23%."""
    october = datetime(2026, 10, 15, tzinfo=UTC)
    assert monthly_allowance_kwh(october, large_family=False) == pytest.approx(
        200 / 30 * 31
    )
    assert monthly_allowance_kwh(october, large_family=True) == pytest.approx(
        300 / 30 * 31
    )
    assert vat_rate("auto", 100.0, 206.7) == 0.06
    assert vat_rate("auto", 210.0, 206.7) == 0.23
    assert vat_rate("auto", None, 206.7) == 0.23
    assert vat_rate("full", 0.0, 206.7) == 0.23
    assert vat_rate("none", 0.0, 206.7) == 0.0


@pytest.fixture
def auto_enable_custom_integrations(
    recorder_mock: RecorderInstanceContextManager,
    enable_custom_integrations: None,
) -> None:
    """Start the recorder before hass (overrides the conftest fixture)."""


CPE = "PT0002000012345678AB"


async def test_backfill_action_end_to_end(
    hass: HomeAssistant,
    auto_enable_custom_integrations: None,  # noqa: ARG001
) -> None:
    """The action rewrites the HAN sensor's history from E-REDES."""
    start = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(
        days=3
    )
    async_add_external_statistics(
        hass,
        {
            "has_mean": False,
            "has_sum": True,
            "mean_type": StatisticMeanType.NONE,
            "name": "E-REDES Energy",
            "source": DOMAIN,
            "statistic_id": statistic_id(CPE),
            "unit_class": "energy",
            "unit_of_measurement": "kWh",
        },
        [
            {"start": start + timedelta(hours=h), "state": 2.0, "sum": 2.0 * (h + 1)}
            for h in range(3)
        ],
    )
    async_import_statistics(
        hass,
        {
            "has_mean": False,
            "has_sum": True,
            "mean_type": StatisticMeanType.NONE,
            "name": "HAN import",
            "source": "recorder",
            "statistic_id": "sensor.han_import",
            "unit_class": "energy",
            "unit_of_measurement": "kWh",
        },
        [
            {"start": start + timedelta(hours=2), "state": 5000.0, "sum": 0.0},
            {"start": start + timedelta(hours=3), "state": 5001.5, "sum": 1.5},
        ],
    )
    await async_wait_recording_done(hass)

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        data={CONF_ACCESS_TOKEN: "x", CONF_CPE: CPE},
        unique_id=CPE,
    )
    entry.add_to_hass(hass)
    with patch("custom_components.eredes.async_setup_entry", return_value=True):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    dry = await hass.services.async_call(
        DOMAIN,
        "backfill_statistics",
        {"entity_id": "sensor.han_import", "dry_run": True},
        blocking=True,
        return_response=True,
    )
    assert dry["hours_from_eredes"] == 3
    await hass.services.async_call(
        DOMAIN,
        "backfill_statistics",
        {"entity_id": "sensor.han_import", "dry_run": False},
        blocking=True,
        return_response=True,
    )
    await async_wait_recording_done(hass)

    rows = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        start - timedelta(hours=1),
        None,
        {"sensor.han_import"},
        "hour",
        None,
        {"sum"},
    )
    sums = [r["sum"] for r in rows["sensor.han_import"]]
    # E-REDES 2 kWh/h for hours 0-2, anchored at the HAN sum before hour 3 (0),
    # then HAN's own +1.5 at hour 3.
    assert sums == pytest.approx([-4.0, -2.0, 0.0, 1.5])


async def test_live_price_switches_vat_at_the_allowance(
    hass: HomeAssistant,
    auto_enable_custom_integrations: None,  # noqa: ARG001
) -> None:
    """The live price uses 6% until the month's kWh pass the allowance."""
    now = datetime.now(UTC)
    prices = {quarter_start(now): 0.10, quarter_start(now) + 900: 0.10}
    hass.states.async_set("sensor.han_import", "5000", {"unit_of_measurement": "kWh"})
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        data={CONF_ACCESS_TOKEN: "x", CONF_CPE: CPE},
        options={
            "price_source": "coopernico_tf",
            "tariff_cycle": "bi_diario",
            "vat_mode": "auto",
            "live_energy_entity": "sensor.han_import",
        },
        unique_id=CPE,
    )
    entry.add_to_hass(hass)
    with (
        patch(
            "custom_components.eredes.coordinator.ERedesClient.get_consumption",
            new=AsyncMock(side_effect=ERedesConnectionError("offline")),
        ),
        patch(
            "custom_components.eredes.price_sensor.async_coopernico_lookup",
            new=AsyncMock(return_value=(prices, {})),
        ),
        patch(
            "custom_components.eredes.historical.async_import_historical_data",
            new=AsyncMock(),
        ),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        entity_id = next(
            s.entity_id
            for s in hass.states.async_all("sensor")
            if s.entity_id.endswith("preco_coopernico")
            or s.entity_id.endswith("coopernico_price")
        )
        state = hass.states.get(entity_id)
        assert float(state.state) == pytest.approx(0.106)
        assert state.attributes["vat_rate"] == 0.06

        hass.states.async_set(
            "sensor.han_import", "5300", {"unit_of_measurement": "kWh"}
        )
        await hass.async_block_till_done()
        state = hass.states.get(entity_id)
        assert float(state.state) == pytest.approx(0.123)
        assert state.attributes["month_kwh"] == pytest.approx(300.0)


async def test_auto_backfill_after_import_and_button(
    hass: HomeAssistant,
    auto_enable_custom_integrations: None,  # noqa: ARG001
) -> None:
    """Configured sensors are corrected after each import and by the button."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        data={CONF_ACCESS_TOKEN: "x", CONF_CPE: CPE},
        options={
            "backfill_energy_entity": "sensor.han_import",
            "backfill_cost_entity": "sensor.han_import_cost",
        },
        unique_id=CPE,
    )
    entry.add_to_hass(hass)
    backfill = AsyncMock(return_value={"hours_written": 1})
    with (
        patch(
            "custom_components.eredes.coordinator.ERedesClient.get_consumption",
            new=AsyncMock(side_effect=ERedesConnectionError("offline")),
        ),
        patch(
            "custom_components.eredes.historical.async_import_historical_data",
            new=AsyncMock(),
        ),
        patch("custom_components.eredes.backfill.async_backfill", new=backfill),
        patch(
            "custom_components.eredes.coordinator.ERedesCoordinator.token_usable",
            new=True,
        ),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        # After the (mocked) import: both sensors, from the right statistics.
        called = {(c.args[1], c.args[2]) for c in backfill.call_args_list}
        assert called == {
            ("eredes:energy_345678ab", "sensor.han_import"),
            ("eredes:cost_345678ab", "sensor.han_import_cost"),
        }

        backfill.reset_mock()
        button = next(
            s.entity_id
            for s in hass.states.async_all("button")
            if s.entity_id.startswith("button.e_redes")
        )
        await hass.services.async_call(
            "button", "press", {"entity_id": button}, blocking=True
        )
        assert backfill.call_count == 2


async def test_auto_backfill_retries_sensor_not_ready(
    hass: HomeAssistant,
    auto_enable_custom_integrations: None,  # noqa: ARG001
) -> None:
    """A sensor without statistics yet is retried an hour later, alone."""
    from homeassistant.util import dt as dt_util  # noqa: PLC0415
    from pytest_homeassistant_custom_component.common import (  # noqa: PLC0415
        async_fire_time_changed,
    )

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        data={CONF_ACCESS_TOKEN: "x", CONF_CPE: CPE},
        options={
            "backfill_energy_entity": "sensor.han_import",
            "backfill_cost_entity": "sensor.han_import_cost",
        },
        unique_id=CPE,
    )
    entry.add_to_hass(hass)

    calls: list[str] = []

    async def fake_backfill(_hass: object, _source: str, target: str) -> dict:
        calls.append(target)
        if target == "sensor.han_import_cost" and calls.count(target) == 1:
            msg = "sensor.han_import_cost has no long-term statistics with a sum"
            raise HomeAssistantError(msg)
        return {"hours_written": 1}

    with (
        patch(
            "custom_components.eredes.coordinator.ERedesClient.get_consumption",
            new=AsyncMock(side_effect=ERedesConnectionError("offline")),
        ),
        patch(
            "custom_components.eredes.historical.async_import_historical_data",
            new=AsyncMock(),
        ),
        patch(
            "custom_components.eredes.backfill.async_backfill",
            side_effect=fake_backfill,
        ),
        patch(
            "custom_components.eredes.coordinator.ERedesCoordinator.token_usable",
            new=True,
        ),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert sorted(calls) == ["sensor.han_import", "sensor.han_import_cost"]

        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=61))
        await hass.async_block_till_done()
        # Only the cost sensor is retried, and this time it succeeds.
        assert calls[2:] == ["sensor.han_import_cost"]

        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=125))
        await hass.async_block_till_done()
        assert len(calls) == 3  # nothing left to retry

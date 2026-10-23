"""Live Coopérnico price sensor (Tiago Felícia data), with the 6%/23% VAT rule.

The Energy Dashboard's "entity with current price" multiplies each kWh by the
price at that moment, so this sensor must change exactly at every quarter-hour
and, with automatic VAT, switch from 6% to 23% the moment the month's
consumption passes 200 kWh/30 days (300 for large families). The month's
consumption comes from a real-time meter sensor (e.g. a HAN reader).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, EventStateChangedData, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_change,
    async_track_time_interval,
)

from .const import SENSOR_COOPERNICO_PRICE, VAT_AUTO, VAT_NONE
from .tariff_tf import LISBON, async_coopernico_lookup

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.device_registry import DeviceInfo

_LOGGER = logging.getLogger(__name__)

VAT_REDUCED = 0.06
VAT_NORMAL = 0.23
REFRESH_INTERVAL = timedelta(hours=3)
QUARTER_SECONDS = 900


def quarter_start(moment: datetime) -> int:
    """Return the UTC epoch of the quarter-hour containing ``moment``."""
    ts = int(moment.timestamp())
    return ts - ts % QUARTER_SECONDS


def monthly_allowance_kwh(moment: datetime, *, large_family: bool) -> float:
    """kWh at 6% VAT in the (Lisbon) month of ``moment``: 200 (300)/30 x days."""
    local = moment.astimezone(LISBON)
    first = datetime(local.year, local.month, 1)
    nxt = datetime(local.year + (local.month == 12), local.month % 12 + 1, 1)
    return (300.0 if large_family else 200.0) / 30 * (nxt - first).days


def vat_rate(mode: str, month_kwh: float | None, allowance: float) -> float:
    """Return the VAT rate to apply now."""
    if mode == VAT_NONE:
        return 0.0
    if mode != VAT_AUTO:
        return VAT_NORMAL
    if month_kwh is None:
        return VAT_NORMAL  # no meter reading: assume the higher rate
    return VAT_REDUCED if month_kwh < allowance else VAT_NORMAL


def _to_kwh(value: float, unit: str | None) -> float:
    if unit == "Wh":
        return value / 1000
    if unit == "MWh":
        return value * 1000
    return value


class ERedesCoopernicoPriceSensor(SensorEntity):
    """Current Coopérnico Único price in €/kWh (VAT per the chosen rule)."""

    _attr_has_entity_name = True
    _attr_translation_key = SENSOR_COOPERNICO_PRICE
    _attr_name = "Coopérnico price"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 4
    _attr_should_poll = False
    _unrecorded_attributes = frozenset({"today", "tomorrow"})

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        cpe: str,
        device_info: DeviceInfo,
        cycle: str,
        vat_mode: str,
        large_family: bool,
        energy_entity: str | None,
    ) -> None:
        """Initialize the sensor."""
        self.hass = hass
        self._cycle = cycle
        self._vat_mode = vat_mode
        self._large_family = large_family
        self._energy_entity = energy_entity
        self._prices: dict[int, float] = {}
        self._baseline: tuple[tuple[int, int], float] | None = None
        self._attr_unique_id = f"{cpe}_{SENSOR_COOPERNICO_PRICE}"
        self._attr_device_info = device_info
        self._attr_native_unit_of_measurement = f"{hass.config.currency}/kWh"

    async def async_added_to_hass(self) -> None:
        """Start the timers and load the prices."""
        self.async_on_remove(
            async_track_time_change(
                self.hass, self._async_tick, minute=[0, 15, 30, 45], second=0
            )
        )
        self.async_on_remove(
            async_track_time_interval(self.hass, self._async_refresh, REFRESH_INTERVAL)
        )
        if self._energy_entity:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass, [self._energy_entity], self._async_energy_changed
                )
            )
        self.hass.async_create_task(self._async_refresh())

    async def _async_refresh(self, _now: datetime | None = None) -> None:
        """Download today's and tomorrow's prices (no VAT)."""
        now = datetime.now(LISBON)
        years = {now.year, (now + timedelta(days=1)).year}
        try:
            self._prices, _ = await async_coopernico_lookup(
                async_get_clientsession(self.hass), years, self._cycle
            )
        except Exception:
            _LOGGER.warning("Could not refresh Coopérnico prices; keeping old ones")
        await self._async_update_state()

    async def _async_tick(self, _now: datetime) -> None:
        await self._async_update_state()

    @callback
    def _async_energy_changed(self, _event: Event[EventStateChangedData]) -> None:
        # Only matters for the 6% -> 23% switch; cheap to recompute.
        self.hass.async_create_task(self._async_update_state())

    def _meter_kwh(self) -> float | None:
        if not self._energy_entity:
            return None
        state = self.hass.states.get(self._energy_entity)
        if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            return None
        try:
            value = float(state.state)
        except ValueError:
            return None
        return _to_kwh(value, state.attributes.get("unit_of_measurement"))

    async def _async_month_kwh(self, now: datetime) -> float | None:
        """Consumption so far this month: meter now minus meter at month start."""
        meter = self._meter_kwh()
        if meter is None:
            return None
        local = now.astimezone(LISBON)
        month = (local.year, local.month)
        if self._baseline is None or self._baseline[0] != month:
            start = datetime(local.year, local.month, 1, tzinfo=LISBON)
            baseline = await self._async_meter_at(start)
            # No recorded reading for the month start (sensor too new or HA
            # down): count from now, which can only under-count the month.
            self._baseline = (month, baseline if baseline is not None else meter)
        return max(meter - self._baseline[1], 0.0)

    async def _async_meter_at(self, start: datetime) -> float | None:
        """First recorded meter value (long-term statistics) from ``start``."""
        from homeassistant.components.recorder import (  # type: ignore[attr-defined]  # noqa: PLC0415
            get_instance,
        )
        from homeassistant.components.recorder.statistics import (  # noqa: PLC0415
            statistics_during_period,
        )

        entity = self._energy_entity
        if entity is None:
            return None

        def _query() -> float | None:
            rows = statistics_during_period(
                self.hass,
                start.astimezone(UTC),
                start.astimezone(UTC) + timedelta(days=2),
                {entity},
                "hour",
                None,
                {"state"},
            ).get(entity, [])
            for row in rows:
                if (value := row.get("state")) is not None:
                    return float(value)
            return None

        try:
            value = await get_instance(self.hass).async_add_executor_job(_query)
        except Exception:
            _LOGGER.debug("Could not read %s statistics", entity, exc_info=True)
            return None
        if value is None:
            return None
        state = self.hass.states.get(entity)
        unit = state.attributes.get("unit_of_measurement") if state else None
        return _to_kwh(value, unit)

    def _day_prices(self, day: datetime, factor: float) -> list[dict[str, Any]]:
        start = datetime(day.year, day.month, day.day, tzinfo=LISBON)
        end = start + timedelta(days=1)
        result = []
        ts = int(start.timestamp())
        while ts < int(end.timestamp()):
            if (price := self._prices.get(ts)) is not None:
                result.append(
                    {
                        "start": datetime.fromtimestamp(ts, LISBON).isoformat(),
                        "price": round(price * factor, 5),
                    }
                )
            ts += QUARTER_SECONDS
        return result

    async def _async_update_state(self) -> None:
        now = datetime.now(UTC)
        net = self._prices.get(quarter_start(now))
        allowance = monthly_allowance_kwh(now, large_family=self._large_family)
        month_kwh = (
            await self._async_month_kwh(now) if self._vat_mode == VAT_AUTO else None
        )
        rate = vat_rate(self._vat_mode, month_kwh, allowance)
        factor = 1 + rate
        self._attr_native_value = None if net is None else round(net * factor, 5)
        today = now.astimezone(LISBON)
        self._attr_extra_state_attributes = {
            "price_without_vat": None if net is None else round(net, 5),
            "vat_rate": rate,
            "month_kwh": None if month_kwh is None else round(month_kwh, 3),
            "reduced_vat_allowance_kwh": round(allowance, 2),
            "cycle": self._cycle,
            "source": "dados.tiagofelicia.pt (CC BY 4.0)",
            "today": self._day_prices(today, factor),
            "tomorrow": self._day_prices(today + timedelta(days=1), factor),
        }
        self.async_write_ha_state()

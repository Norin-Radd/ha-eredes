"""Historical data import for E-REDES integration."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal

from homeassistant.components.recorder import get_instance  # type: ignore[attr-defined]
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.const import UnitOfEnergy
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util.unit_conversion import EnergyConverter

from .const import (
    DOMAIN,
    PRICE_SOURCE_COOPERNICO,
    PRICE_SOURCE_SENSOR,
    VAT_AUTO,
    VAT_NONE,
)
from .tariff_tf import LISBON, async_coopernico_lookup

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.core import HomeAssistant

    from .coordinator import ERedesCoordinator
    from .eredes_api.models import ConsumptionReading

_LOGGER = logging.getLogger(__name__)

# Maximum days to fetch in a single request (API limit)
MAX_DAYS_PER_REQUEST = 31

# Total days of history to import
TOTAL_HISTORY_DAYS = 365  # 1 year

# When resuming, start the fetch a couple of days before the last stored hour
# so the window comfortably covers it despite any timezone skew. Aggregation
# then drops everything up to and including that hour, so nothing is re-counted.
REFETCH_BUFFER_DAYS = 2


@dataclass(frozen=True)
class PriceConfig:
    """How to price the imported consumption."""

    source: str  # PRICE_SOURCE_SENSOR or PRICE_SOURCE_COOPERNICO
    entity: str | None = None
    cycle: str = "bi_diario"
    vat_mode: str = VAT_NONE  # VAT_NONE, VAT_FULL (23%) or VAT_AUTO (6%/23%)
    large_family: bool = False  # 300 instead of 200 kWh at 6% (VAT_AUTO)


VAT_REDUCED = 0.06
VAT_NORMAL = 0.23


class VatRule:
    """VAT on electricity energy in mainland Portugal (BTN up to 6.9 kVA).

    With VAT_AUTO the first 200 kWh per 30 days (300 for large families) pay
    6% and the rest 23%. The allowance is taken per calendar month (200/30 x
    days in the month) and consumed in chronological order, hour by hour.
    """

    def __init__(self, mode: str, large_family: bool = False) -> None:
        """Initialize the rule."""
        self.mode = mode
        self.per_30_days = 300.0 if large_family else 200.0
        self._month: tuple[int, int] | None = None
        self._used = 0.0

    def factor(self, hour_start: datetime, kwh: float) -> float:
        """Account ``kwh`` consumed in this hour and return its VAT multiplier.

        Must be called for every hour in chronological order (also hours that
        are not priced or already imported), so the allowance is tracked.
        """
        if self.mode == VAT_NONE:
            return 1.0
        if self.mode != VAT_AUTO:
            return 1 + VAT_NORMAL
        local = hour_start.astimezone(LISBON)
        month = (local.year, local.month)
        if month != self._month:
            self._month, self._used = month, 0.0
        days = _days_in_month(*month)
        allowance = self.per_30_days / 30 * days
        reduced = min(max(allowance - self._used, 0.0), kwh)
        self._used += kwh
        if kwh <= 0:
            return 1 + VAT_REDUCED if self._used < allowance else 1 + VAT_NORMAL
        share = reduced / kwh
        return 1 + share * VAT_REDUCED + (1 - share) * VAT_NORMAL


def _days_in_month(year: int, month: int) -> int:
    nxt = datetime(year + (month == 12), month % 12 + 1, 1)
    return (nxt - datetime(year, month, 1)).days


def _month_start_naive(moment: datetime) -> datetime:
    local = moment.astimezone(LISBON)
    return datetime(local.year, local.month, 1)


def _metadata(cpe: str) -> StatisticMetaData:
    """Return the metadata of a CPE's energy statistic.

    ``unit_class`` must be ``energy``: Home Assistant's Energy Dashboard only
    offers statistics of that unit class as grid sources, so ``None`` hides it.
    """
    return StatisticMetaData(
        has_mean=False,
        has_sum=True,
        mean_type=StatisticMeanType.NONE,
        name=f"E-REDES Energy ({cpe[-8:]})",
        source=DOMAIN,
        statistic_id=statistic_id(cpe),
        unit_class=EnergyConverter.UNIT_CLASS,
        unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    )


def statistic_id(cpe: str) -> str:
    """Return the external long-term-statistics id for a CPE's energy history.

    External statistics must be ``<source>:<object_id>`` (colon-separated), not
    an entity id — see CONTEXT.md and docs/adr/0002.
    """
    return f"{DOMAIN}:energy_{cpe[-8:].lower()}"


async def async_import_historical_data(
    hass: HomeAssistant,
    coordinator: ERedesCoordinator,
    price: PriceConfig | None = None,
) -> None:
    """Import historical energy (and optionally cost) data from E-REDES.

    Energy: up to 1 year on first run, then resumes after the last stored hour.
    Cost (when ``price`` is set): each 15-minute reading is priced for its own
    interval — from Tiago Felícia's Coopérnico data, or from a price sensor's
    recorded statistics — because the readings arrive ~24h late and the
    *current* price would be the wrong one.
    """
    cpe = coordinator.cpe
    stat_id = statistic_id(cpe)
    cost_id = cost_statistic_id(cpe)
    _LOGGER.debug("Historical import starting for %s", stat_id)

    full_start = datetime.now() - timedelta(days=TOTAL_HISTORY_DAYS)
    after, initial_sum = await _async_last_stat(hass, stat_id)
    start_date = _resume_start(full_start, after)
    if after is not None:
        _LOGGER.debug("Resuming historical import after %s", after.isoformat())
    else:
        _LOGGER.debug("Importing full history window from %s", start_date.isoformat())

    cost_after: datetime | None = None
    cost_sum = 0.0
    if price:
        cost_after, cost_sum = await _async_last_stat(hass, cost_id)
        start_date = min(start_date, _resume_start(full_start, cost_after))
        if price.vat_mode == VAT_AUTO and cost_after is not None:
            # The 6% allowance is per month: re-read the month so far.
            start_date = max(
                full_start, min(start_date, _month_start_naive(cost_after))
            )

    all_readings = await _async_fetch_readings(coordinator, start_date)

    if not all_readings:
        _LOGGER.debug("No historical data found to import")
        _async_refresh_metadata(hass, cpe, stat_id, after)
        return

    # Chunk boundaries may be returned by both neighbouring requests; keep one
    # reading per instant (timestamps are aware UTC, so DST repeats differ).
    all_readings = sorted(
        {r.timestamp: r for r in all_readings}.values(), key=lambda r: r.timestamp
    )

    statistics = _aggregate_to_hourly_statistics(all_readings, initial_sum, after)
    if statistics:
        try:
            async_add_external_statistics(hass, _metadata(cpe), statistics)
            _LOGGER.debug(
                "Imported %d hourly stats (%.3f kWh) for %s",
                len(statistics),
                statistics[-1]["sum"],
                stat_id,
            )
        except Exception:
            _LOGGER.exception("Failed to add external statistics for %s", stat_id)
    else:
        _LOGGER.debug("No statistics generated from %d readings", len(all_readings))
        _async_refresh_metadata(hass, cpe, stat_id, after)

    if price:
        await _async_import_cost(
            hass,
            cpe,
            price=price,
            readings=all_readings,
            initial_sum=cost_sum,
            after=cost_after,
        )


async def _async_last_stat(
    hass: HomeAssistant, stat_id: str
) -> tuple[datetime | None, float]:
    """Return (start of last stored hour as aware UTC, cumulative sum)."""
    last_stats = await get_instance(hass).async_add_executor_job(
        get_last_statistics, hass, 1, stat_id, True, {"sum"}
    )
    if last_stats and stat_id in last_stats:
        row = last_stats[stat_id][0]
        return datetime.fromtimestamp(row["start"], tz=UTC), row.get("sum") or 0.0
    return None, 0.0


def _resume_start(full_start: datetime, after: datetime | None) -> datetime:
    """Return the naive fetch start for a statistic resuming after ``after``.

    Fetch from a couple of days before the cutoff (window safety against
    timezone skew); aggregation drops anything up to and including ``after``,
    so already-counted hours are never re-added.
    """
    if after is None:
        return full_start
    return max(
        full_start, after.replace(tzinfo=None) - timedelta(days=REFETCH_BUFFER_DAYS)
    )


async def _async_fetch_readings(
    coordinator: ERedesCoordinator, start_date: datetime
) -> list[ConsumptionReading]:
    """Fetch readings from ``start_date`` until now, in API-sized chunks."""
    end_date = datetime.now()
    all_readings: list[ConsumptionReading] = []
    current_start = start_date

    while current_start < end_date:
        current_end = min(
            current_start + timedelta(days=MAX_DAYS_PER_REQUEST),
            end_date,
        )
        try:
            _LOGGER.debug(
                "Fetching %s to %s",
                current_start.isoformat(),
                current_end.isoformat(),
            )
            consumption = await coordinator.client.get_consumption(
                coordinator.cpe,
                current_start,
                current_end,
            )
            _LOGGER.debug("Got %d readings", len(consumption.readings))
            all_readings.extend(consumption.readings)
        except Exception as ex:
            _LOGGER.error(
                "Failed to fetch history %s - %s: %s",
                current_start.isoformat(),
                current_end.isoformat(),
                ex,
            )
        current_start = current_end

    return all_readings


def cost_statistic_id(cpe: str) -> str:
    """Return the external statistic id for a CPE's energy cost."""
    return f"{DOMAIN}:cost_{cpe[-8:].lower()}"


def _price_factor(unit: str | None) -> float:
    """Return the factor that converts the price sensor's unit to <currency>/kWh."""
    if not unit:
        return 1.0
    lowered = unit.lower()
    factor = 1.0
    if "mwh" in lowered:
        factor /= 1000
    if lowered.startswith(("c", "ct")) and not lowered.startswith("chf"):
        factor /= 100  # cents (c€/kWh, ct/kWh)
    return factor


async def _async_price_lookup(
    hass: HomeAssistant, entity_id: str, start: datetime, end: datetime
) -> Callable[[datetime], float | None]:
    """Build a function returning the price for a 15-minute reading.

    Uses the 5-minute statistics of the price sensor when still available
    (kept ~10 days by the recorder), else its hourly mean (kept forever).
    Requires the price sensor to have long-term statistics (a state_class).
    """
    instance = get_instance(hass)

    def _fetch(period: Literal["5minute", "hour"]) -> dict[int, float]:
        rows = statistics_during_period(
            hass, start, end, {entity_id}, period, None, {"mean"}
        ).get(entity_id, [])
        return {
            int(row["start"]): float(mean)
            for row in rows
            if (mean := row.get("mean")) is not None
        }

    five_minute = await instance.async_add_executor_job(_fetch, "5minute")
    hourly = await instance.async_add_executor_job(_fetch, "hour")
    state = hass.states.get(entity_id)
    factor = _price_factor(
        state.attributes.get("unit_of_measurement") if state else None
    )
    _LOGGER.debug(
        "Price data for %s: %d 5-minute and %d hourly means (factor %s)",
        entity_id,
        len(five_minute),
        len(hourly),
        factor,
    )

    def lookup(timestamp: datetime) -> float | None:
        ts = int(timestamp.timestamp())
        quarter = [five_minute[t] for t in (ts, ts + 300, ts + 600) if t in five_minute]
        if quarter:
            return sum(quarter) / len(quarter) * factor
        hour = ts - ts % 3600
        if hour in hourly:
            return hourly[hour] * factor
        return None

    return lookup


def _aggregate_hourly_cost(
    readings: list[ConsumptionReading],
    price_at: Callable[[datetime], float | None],
    initial_sum: float = 0.0,
    after: datetime | None = None,
    vat: VatRule | None = None,
) -> tuple[list[StatisticData], int]:
    """Price every reading and roll the cost up into hourly statistics.

    An hour with any unpriced quarter is skipped entirely (counted in the
    returned number) rather than stored as a misleadingly low cost. VAT is
    applied per hour; with the 6%/23% rule every hour (also skipped or already
    imported ones) consumes the monthly allowance, in chronological order.
    """
    vat = vat or VatRule(VAT_NONE)
    net: dict[datetime, float] = {}
    kwh: dict[datetime, float] = {}
    unpriced: set[datetime] = set()
    for reading in readings:
        hour_start = reading.timestamp.replace(minute=0, second=0, microsecond=0)
        kwh[hour_start] = kwh.get(hour_start, 0.0) + reading.value_kwh
        if after is not None and hour_start <= after:
            continue
        price = price_at(reading.timestamp)
        if price is None:
            unpriced.add(hour_start)
            continue
        net[hour_start] = net.get(hour_start, 0.0) + reading.value_kwh * price

    statistics: list[StatisticData] = []
    cumulative = initial_sum
    for hour_start in sorted(kwh):
        factor = vat.factor(hour_start, kwh[hour_start])
        if hour_start not in net or hour_start in unpriced:
            continue
        cost = net[hour_start] * factor
        cumulative += cost
        statistics.append(StatisticData(start=hour_start, state=cost, sum=cumulative))
    return statistics, len(unpriced)


async def _async_build_price_lookup(
    hass: HomeAssistant,
    price: PriceConfig,
    readings: list[ConsumptionReading],
) -> tuple[Callable[[datetime], float | None], list[ConsumptionReading]]:
    """Return a price function and the readings it can be applied to."""
    if price.source == PRICE_SOURCE_COOPERNICO:
        # Only the current TAR is published, so price the current year only.
        year_start = datetime(datetime.now(LISBON).year, 1, 1, tzinfo=LISBON)
        priced = [r for r in readings if r.timestamp >= year_start]
        if not priced:
            return (lambda _ts: None), []
        prices, _ = await async_coopernico_lookup(
            async_get_clientsession(hass),
            {r.timestamp.astimezone(LISBON).year for r in priced},
            price.cycle,
        )
        _LOGGER.debug(
            "Coopérnico (%s) prices for %d quarter-hours", price.cycle, len(prices)
        )
        return (lambda ts: prices.get(int(ts.timestamp()))), priced

    if price.source == PRICE_SOURCE_SENSOR and price.entity:
        start = readings[0].timestamp - timedelta(hours=1)
        end = readings[-1].timestamp + timedelta(hours=1)
        return await _async_price_lookup(hass, price.entity, start, end), readings

    return (lambda _ts: None), []


async def _async_import_cost(
    hass: HomeAssistant,
    cpe: str,
    *,
    price: PriceConfig,
    readings: list[ConsumptionReading],
    initial_sum: float,
    after: datetime | None,
) -> None:
    """Import the cost statistic for the given readings."""
    cost_id = cost_statistic_id(cpe)
    try:
        price_at, readings = await _async_build_price_lookup(hass, price, readings)
    except Exception:
        _LOGGER.exception("Could not get prices for %s", cost_id)
        return
    if not readings:
        _LOGGER.debug("No readings to price for %s", cost_id)
        return

    statistics, unpriced = _aggregate_hourly_cost(
        readings,
        price_at,
        initial_sum,
        after,
        VatRule(price.vat_mode, price.large_family),
    )
    if unpriced:
        _LOGGER.debug("Skipped %d hours without price data", unpriced)
    if not statistics:
        _LOGGER.debug("No cost statistics generated for %s", cost_id)
        return

    currency = hass.config.currency
    metadata = StatisticMetaData(
        has_mean=False,
        has_sum=True,
        mean_type=StatisticMeanType.NONE,
        name=f"E-REDES Cost ({cpe[-8:]})",
        source=DOMAIN,
        statistic_id=cost_id,
        unit_class=None,
        unit_of_measurement=currency,
    )
    try:
        async_add_external_statistics(hass, metadata, statistics)
        _LOGGER.debug(
            "Imported %d hourly cost stats (%.2f %s) for %s",
            len(statistics),
            statistics[-1]["sum"],
            currency,
            cost_id,
        )
    except Exception:
        _LOGGER.exception("Failed to add cost statistics for %s", cost_id)


def _async_refresh_metadata(
    hass: HomeAssistant, cpe: str, stat_id: str, after: datetime | None
) -> None:
    """Rewrite an existing statistic's metadata even when no new hours arrived.

    Statistics created by older versions carry ``unit_class=None`` and are
    therefore invisible in the Energy Dashboard picker; the recorder updates
    metadata on every import, so an empty import is enough to fix them.
    """
    if after is None:
        return  # nothing stored yet, so there is no metadata to fix
    try:
        async_add_external_statistics(hass, _metadata(cpe), [])
        _LOGGER.debug("Refreshed metadata for %s", stat_id)
    except Exception:
        _LOGGER.exception("Failed to refresh metadata for %s", stat_id)


def _aggregate_to_hourly_statistics(
    readings: list[ConsumptionReading],
    initial_sum: float = 0.0,
    after: datetime | None = None,
) -> list[StatisticData]:
    """Aggregate 15-minute readings to hourly statistics.

    Args:
        readings: 15-minute interval readings (timestamps are aware UTC).
        initial_sum: cumulative sum of previously imported hours; the returned
            stats continue from here so the series stays monotonic across runs.
        after: if set, hours at or before this instant are skipped (they were
            already imported). Must be timezone-aware UTC.
    """
    if not readings:
        _LOGGER.debug("No readings to aggregate")
        return []

    statistics: list[StatisticData] = []
    cumulative_sum = initial_sum

    # Group readings by hour
    hourly_data: dict[datetime, float] = {}

    for reading in readings:
        # Timestamps arrive as aware UTC (converted from Lisbon local time at
        # parse time), so this only truncates — stamping tzinfo here would
        # reinterpret a local wall clock as UTC and shift every summer reading
        # an hour late.
        hour_start = reading.timestamp.replace(minute=0, second=0, microsecond=0)

        if after is not None and hour_start <= after:
            continue

        if hour_start not in hourly_data:
            hourly_data[hour_start] = 0.0

        hourly_data[hour_start] += reading.value_kwh

    _LOGGER.debug(
        "Aggregated %d readings into %d hourly buckets",
        len(readings),
        len(hourly_data),
    )

    # Convert to sorted list of statistics
    for hour_start in sorted(hourly_data.keys()):
        hour_kwh = hourly_data[hour_start]
        cumulative_sum += hour_kwh

        statistics.append(
            StatisticData(
                start=hour_start,
                state=hour_kwh,
                sum=cumulative_sum,
            )
        )

    _LOGGER.debug(
        "Created %d statistics, final cumulative sum: %.3f kWh",
        len(statistics),
        cumulative_sum,
    )

    return statistics

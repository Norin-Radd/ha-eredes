"""Copy E-REDES history into another sensor's long-term statistics.

Use case: the meter is now read in real time (e.g. a HAN reader) and that
sensor feeds the Energy Dashboard, but its own history is short or has gaps.
This rewrites the target's hourly statistics *before* the last E-REDES hour
with the official E-REDES values, so the dashboard has one source: official
data for the past, the real-time sensor from then on.

Continuity: the target's later rows keep their cumulative ``sum``, so the
rewritten rows are anchored backwards from the target's own sum just before
the junction. Hours E-REDES does not have keep the target's own consumption.
Running it again later just extends the official part (e.g. over HAN gaps).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from homeassistant.components.recorder import (  # type: ignore[attr-defined]
    get_instance,
)
from homeassistant.components.recorder.statistics import (
    async_import_statistics,
    get_metadata,
    statistics_during_period,
)
from homeassistant.exceptions import HomeAssistantError

if TYPE_CHECKING:
    from homeassistant.components.recorder.models import (
        StatisticData,
        StatisticMetaData,
    )
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

EPOCH = datetime(2000, 1, 1, tzinfo=UTC)
HOUR = 3600

_ENERGY_FACTORS = {"Wh": 1000.0, "kWh": 1.0, "MWh": 0.001}


def _unit_factor(source_unit: str | None, target_unit: str | None) -> float:
    """Factor converting source values into the target's unit."""
    if source_unit == target_unit:
        return 1.0
    if source_unit in _ENERGY_FACTORS and target_unit in _ENERGY_FACTORS:
        return _ENERGY_FACTORS[target_unit] / _ENERGY_FACTORS[source_unit]
    msg = f"Units do not match: {source_unit} -> {target_unit}"
    raise HomeAssistantError(msg)


def build_backfill(
    source_rows: list[dict[str, Any]],
    target_rows: list[dict[str, Any]],
    factor: float = 1.0,
) -> tuple[list[StatisticData], dict[str, Any]]:
    """Compute the rewritten target rows (pure function, see module doc).

    ``source_rows``: E-REDES hourly rows with ``start`` (epoch) and ``state``
    (that hour's amount). ``target_rows``: the target's hourly rows with
    ``start``, ``sum`` and ``state`` (meter reading), any order.
    """
    if not source_rows:
        msg = "The E-REDES statistic has no data yet"
        raise HomeAssistantError(msg)

    source = {
        int(r["start"]): float(r["state"]) * factor
        for r in source_rows
        if r.get("state") is not None
    }
    junction = max(source) + HOUR  # first hour that stays untouched

    target = sorted(
        (r for r in target_rows if r.get("sum") is not None),
        key=lambda r: r["start"],
    )
    before = [r for r in target if int(r["start"]) < junction]
    target_delta: dict[int, float] = {}
    previous: float | None = None
    for row in before:
        current = float(row["sum"])
        if previous is not None:
            target_delta[int(row["start"])] = current - previous
        previous = current

    base_sum = float(before[-1]["sum"]) if before else 0.0
    base_state = (
        float(before[-1]["state"])
        if before and before[-1].get("state") is not None
        else None
    )

    hours = sorted(
        h for h in set(source) | {int(r["start"]) for r in before} if h < junction
    )
    statistics: list[StatisticData] = []
    running = base_sum
    from_eredes = 0
    for hour in reversed(hours):
        if hour in source:
            delta = source[hour]
            from_eredes += 1
        else:
            delta = target_delta.get(hour, 0.0)
        row_sum = running
        state = row_sum if base_state is None else base_state - (base_sum - row_sum)
        statistics.append(
            {
                "start": datetime.fromtimestamp(hour, UTC),
                "state": state,
                "sum": row_sum,
            }
        )
        running -= delta
    statistics.reverse()

    summary = {
        "hours_written": len(statistics),
        "hours_from_eredes": from_eredes,
        "hours_kept_from_target": len(statistics) - from_eredes,
        "first_hour": statistics[0]["start"].isoformat() if statistics else None,
        "junction": datetime.fromtimestamp(junction, UTC).isoformat(),
        "eredes_total": round(sum(source.values()), 3),
    }
    return statistics, summary


async def async_backfill(
    hass: HomeAssistant,
    source_id: str,
    target_id: str,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Rewrite ``target_id``'s statistics before the last E-REDES hour."""
    instance = get_instance(hass)

    def _load() -> tuple[
        StatisticMetaData | None,
        StatisticMetaData | None,
        list[dict[str, Any]],
        list[dict[str, Any]],
    ]:
        meta = get_metadata(hass, statistic_ids={source_id, target_id})
        src_meta = meta.get(source_id, (0, None))[1]
        tgt_meta = meta.get(target_id, (0, None))[1]
        src = statistics_during_period(
            hass, EPOCH, None, {source_id}, "hour", None, {"state"}
        ).get(source_id, [])
        tgt = statistics_during_period(
            hass, EPOCH, None, {target_id}, "hour", None, {"sum", "state"}
        ).get(target_id, [])
        return src_meta, tgt_meta, [dict(r) for r in src], [dict(r) for r in tgt]

    src_meta, tgt_meta, src_rows, tgt_rows = await instance.async_add_executor_job(
        _load
    )
    if src_meta is None:
        msg = f"{source_id} does not exist yet; let the E-REDES import run first"
        raise HomeAssistantError(msg)
    if tgt_meta is None or not tgt_meta.get("has_sum"):
        msg = (
            f"{target_id} has no long-term statistics with a sum "
            "(it needs state_class total or total_increasing)"
        )
        raise HomeAssistantError(msg)

    factor = _unit_factor(
        src_meta.get("unit_of_measurement"), tgt_meta.get("unit_of_measurement")
    )
    statistics, summary = build_backfill(src_rows, tgt_rows, factor)
    summary.update({"source": source_id, "target": target_id, "dry_run": dry_run})
    if not dry_run and statistics:
        async_import_statistics(hass, tgt_meta, statistics)
        _LOGGER.info("Backfilled %s from %s: %s", target_id, source_id, summary)
    return summary


async def async_run_configured_backfills(
    hass: HomeAssistant,
    cpe: str,
    targets: dict[str, str | None],
    *,
    raise_errors: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Backfill the sensors chosen in the options (``energy``/``cost`` -> entity).

    Waits for the recorder first, so statistics just imported are included.
    Returns the summaries and the targets that failed (to retry later).
    """
    from .historical import cost_statistic_id, statistic_id  # noqa: PLC0415

    sources = {"energy": statistic_id(cpe), "cost": cost_statistic_id(cpe)}
    await get_instance(hass).async_block_till_done()
    results: list[dict[str, Any]] = []
    failed: dict[str, str] = {}
    for kind, target in targets.items():
        if not target:
            continue
        try:
            results.append(await async_backfill(hass, sources[kind], target))
        except HomeAssistantError as err:
            _LOGGER.warning("Could not backfill %s (%s): %s", target, kind, err)
            if raise_errors:
                raise
            failed[kind] = target
    return results, failed

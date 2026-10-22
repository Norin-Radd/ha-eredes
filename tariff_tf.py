"""Coopérnico indexed (quarter-hourly) prices from Tiago Felícia's open data.

Source: https://github.com/tiagofelicia/dados-energia (CC BY 4.0), the data
behind the simulators at www.tiagofelicia.pt. Two files are used:

* ``OMIE_PERDAS_CICLOS_<year>.csv`` — one row per quarter-hour (Lisbon wall
  clock, *end* of the interval, dates as MM/DD/YYYY): the OMIE PT price
  (€/MWh), the ERSE loss factor, the regulation costs ERC (€/MWh) and the
  BTN period (V/F/C/P) of each cycle.
* ``Constantes.csv`` — the tariff constants, among them ``Coop_K``,
  ``Coop_CS`` (used when ERC is missing), the TAR per period and the TSE.

Coopérnico Único, per quarter-hour (€/kWh, without VAT), as on
tiagofelicia.pt/formulas-tarifarios-indexados:

    energia = (OMIE + ERC) * Perdas + K
    preço   = energia + TAR(período do ciclo) + TSE

Checked against the published ``precos-horarios.csv`` (e.g. 22/10/2026
00:00, Simples: 0.33147 €/kWh).
"""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final
from zoneinfo import ZoneInfo

from aiohttp import ClientTimeout

if TYPE_CHECKING:
    from aiohttp import ClientSession

_LOGGER = logging.getLogger(__name__)

LISBON = ZoneInfo("Europe/Lisbon")

BASES: Final = (
    "https://dados.tiagofelicia.pt/data/simuladores/simulador-tarifarios-eletricidade/csv",
    "https://raw.githubusercontent.com/tiagofelicia/dados-energia/main/data/simuladores/simulador-tarifarios-eletricidade/csv",
)

# cycle option -> (period column in OMIE_PERDAS_CICLOS, {period: TAR constant})
CYCLES: Final[dict[str, tuple[str, dict[str, str]]]] = {
    "simples": ("Simp", {"S": "TAR_Energia_Simples"}),
    "bi_diario": (
        "BD",
        {"V": "TAR_Energia_Bi_Vazio", "F": "TAR_Energia_Bi_ForaVazio"},
    ),
    "bi_semanal": (
        "BS",
        {"V": "TAR_Energia_Bi_Vazio", "F": "TAR_Energia_Bi_ForaVazio"},
    ),
    "tri_diario": (
        "TD",
        {
            "V": "TAR_Energia_Tri_Vazio",
            "C": "TAR_Energia_Tri_Cheias",
            "P": "TAR_Energia_Tri_Ponta",
        },
    ),
    "tri_semanal": (
        "TS",
        {
            "V": "TAR_Energia_Tri_Vazio",
            "C": "TAR_Energia_Tri_Cheias",
            "P": "TAR_Energia_Tri_Ponta",
        },
    ),
}

QUARTER = timedelta(minutes=15)


@dataclass(frozen=True)
class QuarterHour:
    """Market data of one quarter-hour."""

    omie: float  # €/MWh
    perdas: float  # loss factor (e.g. 1.15)
    erc: float | None  # €/MWh, None when not (yet) published
    periods: dict[str, str]  # cycle column -> period letter


def parse_constants(text: str) -> dict[str, float]:
    """Parse ``Constantes.csv`` into {name: value}."""
    constants: dict[str, float] = {}
    for row in csv.reader(io.StringIO(text.lstrip("\ufeff"))):
        if len(row) < 2 or not row[0]:
            continue
        try:
            constants[row[0].strip()] = float(row[1])
        except ValueError:
            continue
    return constants


def parse_quarter_hours(text: str) -> dict[int, QuarterHour]:
    """Parse ``OMIE_PERDAS_CICLOS_<year>.csv`` keyed by interval start (UTC epoch).

    ``Hora`` is the *end* of the interval in Lisbon time (``23:59`` closes the
    day). On the autumn DST day the repeated end times appear twice, in order:
    the first copy is summer time (fold 0), the second winter time (fold 1).
    """
    result: dict[int, QuarterHour] = {}
    seen: set[datetime] = set()
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    for row in reader:
        try:
            month, day, year = (int(p) for p in row["Data"].split("/"))
            hour, minute = (int(p) for p in row["Hora"].split(":"))
            omie = float(row["OMIE"])
            perdas = float(row["Perdas"])
        except KeyError, ValueError, AttributeError:
            continue
        end = (
            datetime(year, month, day) + timedelta(days=1)
            if (hour, minute) == (23, 59)
            else datetime(year, month, day, hour, minute)
        )
        # Resolve the wall-clock *end* (fold for the repeated autumn hour) and
        # step back 15 minutes in UTC: on the spring day the interval ending
        # at 02:00 starts at 00:45, which wall-clock arithmetic would miss.
        local_end = end.replace(tzinfo=LISBON, fold=1 if end in seen else 0)
        seen.add(end)
        start_utc = local_end.astimezone(UTC) - QUARTER
        erc_raw = (row.get("ERC") or "").strip()
        try:
            erc = float(erc_raw) if erc_raw else None
        except ValueError:
            erc = None
        result[int(start_utc.timestamp())] = QuarterHour(
            omie=omie,
            perdas=perdas,
            erc=erc,
            periods={c: (row.get(c) or "").strip() for c, _ in CYCLES.values()},
        )
    return result


def coopernico_price(
    qh: QuarterHour, constants: dict[str, float], cycle: str
) -> float | None:
    """Return the Coopérnico Único price (€/kWh, no VAT) for one quarter-hour."""
    column, tar_names = CYCLES[cycle]
    period = qh.periods.get(column, "")
    tar_name = tar_names.get(period)
    if tar_name is None or tar_name not in constants:
        return None
    erc_kwh = qh.erc / 1000 if qh.erc is not None else constants.get("Coop_CS", 0.0)
    energia = (qh.omie / 1000 + erc_kwh) * qh.perdas + constants.get("Coop_K", 0.0)
    return energia + constants[tar_name] + constants.get("Financiamento_TSE", 0.0)


async def _async_fetch_text(session: ClientSession, name: str) -> str:
    """Download one CSV, trying each mirror in turn."""
    last_error: Exception | None = None
    for base in BASES:
        url = f"{base}/{name}.csv"
        try:
            async with session.get(url, timeout=ClientTimeout(total=60)) as response:
                response.raise_for_status()
                return (await response.read()).decode("utf-8-sig")
        except Exception as err:
            _LOGGER.debug("Could not fetch %s: %s", url, err)
            last_error = err
    msg = f"Could not download {name} from any mirror"
    raise RuntimeError(msg) from last_error


async def async_coopernico_lookup(
    session: ClientSession,
    years: set[int],
    cycle: str,
    vat_percent: float = 0.0,
) -> tuple[dict[int, float], dict[str, float]]:
    """Return ({interval start UTC epoch: price €/kWh incl. VAT}, constants)."""
    constants = parse_constants(await _async_fetch_text(session, "Constantes"))
    factor = 1 + vat_percent / 100
    prices: dict[int, float] = {}
    for year in sorted(years):
        try:
            text = await _async_fetch_text(session, f"OMIE_PERDAS_CICLOS_{year}")
        except RuntimeError:
            _LOGGER.warning("No Tiago Felícia price data for %s", year)
            continue
        for ts, qh in parse_quarter_hours(text).items():
            price = coopernico_price(qh, constants, cycle)
            if price is not None:
                prices[ts] = price * factor
    return prices, constants

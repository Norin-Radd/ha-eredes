"""Coopérnico prices from Tiago Felícia's data (checked against his own output)."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import get_last_statistics
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.eredes.eredes_api.models import (
    ConsumptionData,
    ConsumptionReading,
)
from custom_components.eredes.historical import (
    PriceConfig,
    async_import_historical_data,
    cost_statistic_id,
)
from custom_components.eredes.tariff_tf import (
    LISBON,
    coopernico_price,
    parse_constants,
    parse_quarter_hours,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.typing import (
        RecorderInstanceContextManager,
    )

CONSTANTES = """\ufeffconstante,valor_unitário,,,
Coop_K,0.009,,,
Coop_CS,0.01673,,,
TAR_Energia_Simples,0.0607,,,
TAR_Energia_Bi_Vazio,0.0158,,,
TAR_Energia_Bi_ForaVazio,0.0835,,,
Financiamento_TSE,0.0020666,,,
"""

# Real rows of OMIE_PERDAS_CICLOS_2026.csv (Hora = end of the interval).
OMIE = """\ufeffData,Hora,Simp,BD,BS,TD,TS,BTN_A,BTN_B,BTN_C,OMIE,Perdas,ERC
10/21/2026,23:59,S,V,F,V,C,0.020528,0.0232556,0.0225998,214.0,1.141391956144038,10.98
10/22/2026,00:15,S,V,V,V,V,0.0197442,0.0222434,0.0213144,219.4,1.138254702277693,8.76
10/22/2026,12:15,S,F,F,P,P,0.0401389,0.0332355,0.028747,1.81,1.160623895350383,29.48
10/22/2026,14:15,S,F,F,C,C,0.038192,0.0315387,0.0265413,0.51,1.152441185056406,30.19
"""


def _ts(*args: int) -> int:
    return int(datetime(*args, tzinfo=LISBON).astimezone(UTC).timestamp())


@pytest.mark.parametrize(
    ("start", "cycle", "expected"),
    [
        # Published in precos-horarios.csv for "Coopérnico Único" (col).
        ((2026, 10, 22, 0, 0), "simples", 0.33147),
        ((2026, 10, 22, 12, 0), "bi_diario", 0.13088),
        ((2026, 10, 22, 14, 0), "bi_diario", 0.12995),
        ((2026, 10, 21, 23, 45), "bi_diario", None),  # value checked below
    ],
)
def test_matches_published_prices(
    start: tuple[int, ...], cycle: str, expected: float | None
) -> None:
    """The formula reproduces Tiago Felícia's published Coopérnico prices."""
    constants = parse_constants(CONSTANTES)
    quarters = parse_quarter_hours(OMIE)
    price = coopernico_price(quarters[_ts(*start)], constants, cycle)
    assert price is not None
    if expected is not None:
        assert round(price, 5) == expected


def test_parses_end_of_interval_and_day_close() -> None:
    """``23:59`` closes the day: it is the 23:45-24:00 interval."""
    quarters = parse_quarter_hours(OMIE)
    assert _ts(2026, 10, 21, 23, 45) in quarters
    assert _ts(2026, 10, 22, 0, 0) in quarters


def test_dst_days_are_continuous() -> None:
    """Spring (92) and autumn (100) days map to distinct, gapless UTC quarters."""
    header = OMIE.splitlines()[0]
    spring = ["03/29/2026,00:15", "03/29/2026,00:30", "03/29/2026,00:45"]
    spring += ["03/29/2026,02:00", "03/29/2026,02:15"]
    autumn = ["10/25/2026,00:45", "10/25/2026,01:00", "10/25/2026,01:15"]
    autumn += ["10/25/2026,01:30", "10/25/2026,01:45", "10/25/2026,01:00"]
    autumn += ["10/25/2026,01:15", "10/25/2026,01:30", "10/25/2026,01:45"]
    autumn += ["10/25/2026,02:00"]
    rest = ",S,V,V,V,V,0,0,0,100,1.1,10"
    for rows, n in ((spring, 5), (autumn, 10)):
        text = "\n".join([header] + [r + rest for r in rows])
        keys = sorted(parse_quarter_hours(text))
        assert len(keys) == n
        assert all(b - a == 900 for a, b in itertools.pairwise(keys))


@pytest.fixture
def auto_enable_custom_integrations(
    recorder_mock: RecorderInstanceContextManager,
    enable_custom_integrations: None,
) -> None:
    """Start the recorder before hass (overrides the conftest fixture)."""


async def test_cost_from_coopernico_data(
    hass: HomeAssistant,
    auto_enable_custom_integrations: None,  # noqa: ARG001
) -> None:
    """Consumption is priced quarter by quarter with VAT, into eredes:cost_*."""
    cpe = "PT0002000012345678AB"
    starts = [(2026, 10, 22, 12, 0), (2026, 10, 22, 14, 0)]
    readings = [
        ConsumptionReading(
            timestamp=datetime(*s, tzinfo=LISBON).astimezone(UTC), value_wh=1000.0
        )
        for s in starts
    ]
    now = datetime.now()
    coordinator = MagicMock(cpe=cpe)
    coordinator.client.get_consumption = AsyncMock(
        return_value=ConsumptionData(
            cpe=cpe, readings=readings, start_date=now, end_date=now
        )
    )

    async def fake_fetch(_session: object, name: str) -> str:
        return CONSTANTES if name == "Constantes" else OMIE

    with (
        patch(
            "custom_components.eredes.tariff_tf._async_fetch_text",
            side_effect=fake_fetch,
        ),
        patch(
            "custom_components.eredes.historical.datetime",
            wraps=datetime,
        ) as fake_dt,
    ):
        fake_dt.now.return_value = datetime(2026, 10, 23, 12, 0, tzinfo=LISBON)
        await async_import_historical_data(
            hass,
            coordinator,
            PriceConfig(source="coopernico_tf", cycle="bi_diario", vat_percent=23),
        )
        await async_wait_recording_done(hass)

    cost = await get_instance(hass).async_add_executor_job(
        get_last_statistics, hass, 1, cost_statistic_id(cpe), True, {"sum"}
    )
    expected = (0.13088 + 0.12995) * 1.23  # 1 kWh in each quarter
    assert cost[cost_statistic_id(cpe)][0]["sum"] == pytest.approx(expected, abs=1e-4)

"""Actions (services) of the E-REDES integration."""

from __future__ import annotations

from typing import TYPE_CHECKING

import voluptuous as vol
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv

from .backfill import async_backfill
from .const import CONF_CPE, DOMAIN
from .historical import cost_statistic_id, statistic_id

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

SERVICE_BACKFILL = "backfill_statistics"
ATTR_KIND = "kind"
ATTR_CPE = "cpe"
ATTR_DRY_RUN = "dry_run"

BACKFILL_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENTITY_ID): cv.entity_id,
        vol.Optional(ATTR_KIND, default="energy"): vol.In(["energy", "cost"]),
        vol.Optional(ATTR_CPE): cv.string,
        vol.Optional(ATTR_DRY_RUN, default=False): cv.boolean,
    }
)


def _pick_cpe(hass: HomeAssistant, wanted: str | None) -> str:
    cpes = [e.data[CONF_CPE] for e in hass.config_entries.async_entries(DOMAIN)]
    if wanted:
        wanted = wanted.strip().upper()
        matches = [c for c in cpes if c.upper().endswith(wanted)]
    else:
        matches = cpes
    if len(matches) != 1:
        msg = (
            "Indica o CPE (ou os seus últimos 8 caracteres)"
            if matches
            else "Nenhuma entrada E-REDES com esse CPE"
        )
        raise HomeAssistantError(msg)
    return str(matches[0])


def async_register_services(hass: HomeAssistant) -> None:
    """Register the integration's actions."""

    async def _backfill(call: ServiceCall) -> ServiceResponse:
        cpe = _pick_cpe(hass, call.data.get(ATTR_CPE))
        source = (
            cost_statistic_id(cpe)
            if call.data[ATTR_KIND] == "cost"
            else statistic_id(cpe)
        )
        return await async_backfill(
            hass,
            source,
            call.data[ATTR_ENTITY_ID],
            dry_run=call.data[ATTR_DRY_RUN],
        )

    hass.services.async_register(
        DOMAIN,
        SERVICE_BACKFILL,
        _backfill,
        schema=BACKFILL_SCHEMA,  # type: ignore[arg-type]
        supports_response=SupportsResponse.OPTIONAL,
    )

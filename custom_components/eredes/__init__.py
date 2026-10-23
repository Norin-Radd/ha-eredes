"""The E-REDES integration."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_call_later

from .const import (
    CONF_ACCESS_TOKEN,
    CONF_BACKFILL_COST_ENTITY,
    CONF_BACKFILL_ENERGY_ENTITY,
    CONF_LARGE_FAMILY,
    CONF_PRICE_ENTITY,
    CONF_PRICE_SOURCE,
    CONF_TARIFF_CYCLE,
    CONF_VAT_MODE,
    CONF_VAT_PERCENT,
    DOMAIN,
    LEGACY_TOKEN_KEYS,
    PRICE_SOURCE_NONE,
    PRICE_SOURCE_SENSOR,
    VAT_FULL,
    VAT_NONE,
)
from .coordinator import ERedesCoordinator
from .services import async_register_services
from .webhook import async_register_token_webhook

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.typing import ConfigType

    from .eredes_api import ERedesClient

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.BUTTON, Platform.SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, _config: ConfigType) -> bool:
    """Set up the integration (actions are registered once, not per entry)."""
    async_register_services(hass)
    return True


@dataclass
class ERedesData:
    """Runtime data for E-REDES integration."""

    coordinator: ERedesCoordinator
    client: ERedesClient


type ERedesConfigEntry = ConfigEntry[ERedesData]


async def async_setup_entry(hass: HomeAssistant, entry: ERedesConfigEntry) -> bool:
    """Set up E-REDES from a config entry."""
    _LOGGER.debug("Setting up E-REDES integration")

    # Register the token webhook first, so a new token can always be delivered,
    # even when the stored one is long dead.
    async_register_token_webhook(hass, entry)

    coordinator = ERedesCoordinator(hass, entry)

    # Fetch initial data. A failure (E-REDES down) must not block the entry:
    # the webhook and the live price sensor do not depend on the E-REDES API,
    # and the coordinator keeps retrying on its own schedule.
    await coordinator.async_refresh()

    # Store runtime data
    entry.runtime_data = ERedesData(
        coordinator=coordinator,
        client=coordinator.client,
    )

    # Set up platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Backfill history while the token is alive. With a dead token there is
    # nothing to do: the next token delivered by webhook reloads the entry and
    # the import resumes from the last stored hour.
    if not coordinator.token_usable:
        _LOGGER.debug("Skipping historical import: no usable E-REDES token")
        return True

    hass.async_create_task(
        _async_import_historical_data(hass, entry, coordinator),
        name="eredes_historical_import",
    )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ERedesConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate an old config entry to the current version.

    Version 1 stored the access token under a legacy key (``session_cookie``,
    or ``aat_token`` in the earliest builds). Version 2 stores it under
    ``access_token`` (see CONTEXT.md → Authentication).
    """
    if entry.version > 2:
        # Downgrade from a future version is not supported.
        return False

    if entry.version == 1:
        data = dict(entry.data)
        for legacy_key in LEGACY_TOKEN_KEYS:
            if legacy_key in data:
                data[CONF_ACCESS_TOKEN] = data.pop(legacy_key)
                break
        hass.config_entries.async_update_entry(entry, data=data, version=2)
        _LOGGER.debug("Migrated E-REDES config entry to version 2")

    return True


async def _async_import_historical_data(
    hass: HomeAssistant,
    entry: ERedesConfigEntry,
    coordinator: ERedesCoordinator,
) -> None:
    """Import historical data in the background."""
    from .historical import (  # noqa: PLC0415
        PriceConfig,
        async_import_historical_data,
    )

    options = entry.options
    source = options.get(CONF_PRICE_SOURCE) or (
        PRICE_SOURCE_SENSOR if options.get(CONF_PRICE_ENTITY) else PRICE_SOURCE_NONE
    )
    price = (
        None
        if source == PRICE_SOURCE_NONE
        else PriceConfig(
            source=source,
            entity=options.get(CONF_PRICE_ENTITY),
            cycle=options.get(CONF_TARIFF_CYCLE, "bi_diario"),
            vat_mode=options.get(CONF_VAT_MODE)
            or (VAT_FULL if float(options.get(CONF_VAT_PERCENT, 0)) else VAT_NONE),
            large_family=bool(options.get(CONF_LARGE_FAMILY, False)),
        )
    )

    _LOGGER.debug("Starting historical data import for CPE %s", coordinator.cpe[-8:])
    try:
        await async_import_historical_data(hass, coordinator, price)
        _LOGGER.debug("Historical data import completed")
    except Exception:
        _LOGGER.exception("Failed to import historical data")
        return

    targets = backfill_targets(entry)
    if any(targets.values()):
        await _async_auto_backfill(hass, entry, coordinator.cpe, targets, attempt=1)


BACKFILL_RETRY_DELAY = timedelta(hours=1)
BACKFILL_MAX_ATTEMPTS = 6


async def _async_auto_backfill(
    hass: HomeAssistant,
    entry: ERedesConfigEntry,
    cpe: str,
    targets: dict[str, str | None],
    *,
    attempt: int,
) -> None:
    """Correct the chosen sensors; retry hourly the ones that are not ready.

    A sensor can fail because it has no statistics yet (e.g. the Energy
    Dashboard's _cost sensor right after it was created): try again later
    instead of waiting for the next E-REDES login.
    """
    from .backfill import async_run_configured_backfills  # noqa: PLC0415

    results, failed = await async_run_configured_backfills(hass, cpe, targets)
    _LOGGER.debug("Automatic backfill (attempt %d): %s", attempt, results)
    if not failed:
        return
    if attempt >= BACKFILL_MAX_ATTEMPTS:
        _LOGGER.warning(
            "Giving up correcting %s after %d attempts; use the button or the "
            "eredes.backfill_statistics action",
            ", ".join(failed.values()),
            attempt,
        )
        return

    retry_targets: dict[str, str | None] = dict(failed)

    async def _retry(_now: datetime) -> None:
        await _async_auto_backfill(hass, entry, cpe, retry_targets, attempt=attempt + 1)

    _LOGGER.info(
        "Will retry correcting %s in %s",
        ", ".join(failed.values()),
        BACKFILL_RETRY_DELAY,
    )
    entry.async_on_unload(async_call_later(hass, BACKFILL_RETRY_DELAY, _retry))


def backfill_targets(entry: ERedesConfigEntry) -> dict[str, str | None]:
    """Sensors to correct with E-REDES data after each import (options)."""
    return {
        "energy": entry.options.get(CONF_BACKFILL_ENERGY_ENTITY),
        "cost": entry.options.get(CONF_BACKFILL_COST_ENTITY),
    }

"""The E-REDES integration."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform

from .const import (
    CONF_ACCESS_TOKEN,
    CONF_PRICE_ENTITY,
    CONF_PRICE_SOURCE,
    CONF_TARIFF_CYCLE,
    CONF_VAT_PERCENT,
    LEGACY_TOKEN_KEYS,
    PRICE_SOURCE_NONE,
    PRICE_SOURCE_SENSOR,
)
from .coordinator import ERedesCoordinator
from .webhook import async_register_token_webhook

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .eredes_api import ERedesClient

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]


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

    # Fetch initial data (never fails on an expired token — see coordinator)
    await coordinator.async_config_entry_first_refresh()

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
            vat_percent=float(options.get(CONF_VAT_PERCENT, 0)),
        )
    )

    _LOGGER.debug("Starting historical data import for CPE %s", coordinator.cpe[-8:])
    try:
        await async_import_historical_data(hass, coordinator, price)
        _LOGGER.debug("Historical data import completed")
    except Exception:
        _LOGGER.exception("Failed to import historical data")

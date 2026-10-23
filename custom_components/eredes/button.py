"""Button to correct real-time sensors with the E-REDES history."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.exceptions import HomeAssistantError

from . import backfill_targets
from .backfill import async_run_configured_backfills
from .sensor import _device_info

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from . import ERedesConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001
    entry: ERedesConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add the button when at least one sensor to correct is configured."""
    if any(backfill_targets(entry).values()):
        async_add_entities([ERedesBackfillButton(entry)])


class ERedesBackfillButton(ButtonEntity):
    """Rewrite the configured sensors' history with the E-REDES data now."""

    _attr_has_entity_name = True
    _attr_translation_key = "backfill"
    _attr_icon = "mdi:history"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry: ERedesConfigEntry) -> None:
        """Initialize the button."""
        coordinator = entry.runtime_data.coordinator
        self._entry = entry
        self._cpe = coordinator.cpe
        self._attr_unique_id = f"{coordinator.cpe}_backfill"
        self._attr_device_info = _device_info(coordinator)

    async def async_press(self) -> None:
        """Run the backfill for the configured sensors."""
        results, _ = await async_run_configured_backfills(
            self.hass, self._cpe, backfill_targets(self._entry), raise_errors=True
        )
        if not results:
            msg = "Nenhum sensor para corrigir nas opções da integração"
            raise HomeAssistantError(msg)

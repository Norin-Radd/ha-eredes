"""DataUpdateCoordinator for E-REDES integration.

The ``aat`` access token lives a hard 91 minutes and can only be minted by a
captcha-gated browser login (see CONTEXT.md and docs/adr/0001). This fork
therefore treats the token as a one-shot batch credential:

* while the token is alive, poll normally;
* once it has expired (or the API rejects it), stop calling the API, keep the
  last known values and expose ``token_valid = False`` — no hourly reauth
  notifications;
* a fresh token arrives through the webhook (see ``webhook.py``), the entry is
  reloaded and the historical import backfills everything that was missed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ACCESS_TOKEN,
    CONF_CPE,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    TOKEN_EXPIRY_MARGIN_SECONDS,
)
from .eredes_api import (
    ConsumptionData,
    ERedesAuthenticationError,
    ERedesClient,
    ERedesConnectionError,
    ERedesError,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .eredes_api.models import ConsumptionReading

_LOGGER = logging.getLogger(__name__)

TOKEN_MARGIN = timedelta(seconds=TOKEN_EXPIRY_MARGIN_SECONDS)


@dataclass
class ERedesCoordinatorData:
    """Data class for coordinator data."""

    consumption: ConsumptionData | None
    today_kwh: float | None
    current_power_w: float | None
    last_reading: ConsumptionReading | None
    last_update: datetime
    period_start: datetime | None = None
    token_valid: bool = True


class ERedesCoordinator(DataUpdateCoordinator[ERedesCoordinatorData]):
    """Coordinator for fetching E-REDES data."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )
        self.config_entry = entry
        self._cpe: str = entry.data[CONF_CPE]
        self._token_rejected = False

        session = async_get_clientsession(hass)
        self._client = ERedesClient(session, str(entry.data[CONF_ACCESS_TOKEN]))

    @property
    def cpe(self) -> str:
        """Return the CPE code."""
        return self._cpe

    @property
    def client(self) -> ERedesClient:
        """Return the API client."""
        return self._client

    @property
    def token_usable(self) -> bool:
        """Return True if the current token is worth sending to the API."""
        return not self._token_rejected and not self._client.token_is_expired(
            TOKEN_MARGIN
        )

    def update_access_token(self, access_token: str) -> None:
        """Update the access token in the client."""
        self._client.update_access_token(access_token)
        self._token_rejected = False

    def _stale_data(self) -> ERedesCoordinatorData:
        """Return the last known data flagged as having no usable token."""
        # ``data`` is typed non-optional by HA but is None before the first fetch.
        previous: ERedesCoordinatorData | None = self.data
        if previous is not None:
            return replace(previous, token_valid=False)
        return ERedesCoordinatorData(
            consumption=None,
            today_kwh=None,
            current_power_w=None,
            last_reading=None,
            last_update=dt_util.now(),
            token_valid=False,
        )

    async def _async_update_data(self) -> ERedesCoordinatorData:
        """Fetch data from E-REDES."""
        if not self.token_usable:
            _LOGGER.debug(
                "E-REDES token expired/rejected for CPE %s; waiting for a new one",
                self._cpe[-8:],
            )
            return self._stale_data()

        try:
            # Fetch yesterday's data (E-REDES data is delayed ~24h)
            now = datetime.now()
            today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            yesterday_start = today_start - timedelta(days=1)

            consumption = await self._client.get_consumption(
                self._cpe,
                yesterday_start,
                today_start,
            )

            today_kwh = consumption.total_kwh
            last_reading = consumption.readings[-1] if consumption.readings else None

            # Convert 15-min energy to power (W)
            current_power_w = 0.0
            if last_reading:
                current_power_w = last_reading.value_kwh * 4 * 1000

            _LOGGER.debug(
                "Fetched %d readings, yesterday: %.3f kWh, power: %.0f W",
                len(consumption.readings),
                today_kwh,
                current_power_w,
            )

            return ERedesCoordinatorData(
                consumption=consumption,
                today_kwh=today_kwh,
                current_power_w=current_power_w,
                last_reading=last_reading,
                last_update=dt_util.now(),
                period_start=dt_util.as_utc(
                    yesterday_start.replace(tzinfo=dt_util.get_default_time_zone())
                ),
                token_valid=True,
            )

        except ERedesAuthenticationError:
            # Do not raise ConfigEntryAuthFailed: that would fire a reauth
            # notification every ~91 minutes. Go dormant until a new token
            # arrives through the webhook (or reconfigure).
            _LOGGER.info(
                "E-REDES rejected the token for CPE %s; waiting for a new one",
                self._cpe[-8:],
            )
            self._token_rejected = True
            return self._stale_data()
        except ERedesConnectionError as err:
            raise UpdateFailed(f"Error communicating with E-REDES: {err}") from err
        except ERedesError as err:
            raise UpdateFailed(f"Error fetching data: {err}") from err

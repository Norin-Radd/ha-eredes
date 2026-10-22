"""Tests for the token-expiry handling and the token webhook (fork feature)."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.eredes.const import (
    CONF_ACCESS_TOKEN,
    CONF_CPE,
    CONF_WEBHOOK_ID,
    DOMAIN,
)
from custom_components.eredes.eredes_api import ERedesAuthenticationError
from custom_components.eredes.eredes_api.client import ERedesClient
from custom_components.eredes.eredes_api.models import ConsumptionData

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

CPE = "PT0002000012345678AB"
GET_CONSUMPTION = (
    "custom_components.eredes.eredes_api.client.ERedesClient.get_consumption"
)


def make_jwt(exp: datetime) -> str:
    """Build an unsigned JWT-shaped token with the given expiry."""

    def b64(obj: dict) -> str:
        raw = json.dumps(obj).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{b64({'alg': 'none'})}.{b64({'exp': int(exp.timestamp())})}.sig"


def empty_consumption() -> ConsumptionData:
    now = datetime.now()
    return ConsumptionData(cpe=CPE, readings=[], start_date=now, end_date=now)


def test_token_expiry_is_read_from_jwt() -> None:
    """The exp claim is decoded, also from a full Cookie header."""
    exp = datetime.now(UTC).replace(microsecond=0) + timedelta(minutes=30)
    client = ERedesClient(AsyncMock(), f"PHPSESSID=x; aat={make_jwt(exp)}")
    assert client.token_expires_at == exp
    assert not client.token_is_expired()
    assert client.token_is_expired(timedelta(hours=1))


def test_unreadable_token_is_assumed_alive() -> None:
    """A token that is not a JWT has no known expiry."""
    client = ERedesClient(AsyncMock(), "not-a-jwt")
    assert client.token_expires_at is None
    assert not client.token_is_expired()


async def test_setup_with_expired_token_stays_loaded(hass: HomeAssistant) -> None:
    """An expired token does not fail setup nor call the API."""
    old = make_jwt(datetime.now(UTC) - timedelta(hours=3))
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        data={CONF_ACCESS_TOKEN: old, CONF_CPE: CPE},
        unique_id=CPE,
    )
    entry.add_to_hass(hass)

    with patch(GET_CONSUMPTION, new=AsyncMock()) as get:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    get.assert_not_called()
    assert CONF_WEBHOOK_ID in entry.data
    assert not entry.runtime_data.coordinator.data.token_valid
    assert not hass.config_entries.flow.async_progress()  # no reauth flow


async def test_rejected_token_goes_dormant(hass: HomeAssistant) -> None:
    """A 401/403 marks the token unusable instead of starting reauth."""
    alive = make_jwt(datetime.now(UTC) + timedelta(hours=1))
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        data={CONF_ACCESS_TOKEN: alive, CONF_CPE: CPE},
        unique_id=CPE,
    )
    entry.add_to_hass(hass)

    with patch(
        GET_CONSUMPTION, new=AsyncMock(side_effect=ERedesAuthenticationError("no"))
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert not entry.runtime_data.coordinator.token_usable
    assert not hass.config_entries.flow.async_progress()


async def test_webhook_stores_new_token_and_reloads(
    hass: HomeAssistant, hass_client_no_auth: ClientSessionGenerator
) -> None:
    """A valid token posted to the webhook replaces the stored one."""
    await async_setup_component(hass, "webhook", {})
    old = make_jwt(datetime.now(UTC) - timedelta(hours=3))
    new = make_jwt(datetime.now(UTC) + timedelta(minutes=90))
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        data={CONF_ACCESS_TOKEN: old, CONF_CPE: CPE, CONF_WEBHOOK_ID: "hook123"},
        unique_id=CPE,
    )
    entry.add_to_hass(hass)

    with (
        patch(GET_CONSUMPTION, new=AsyncMock(return_value=empty_consumption())),
        patch(
            "custom_components.eredes.historical.async_import_historical_data",
            new=AsyncMock(),
        ) as backfill,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        backfill.assert_not_called()  # old token is dead: nothing to import

        client = await hass_client_no_auth()
        resp = await client.post("/api/webhook/hook123", json={"token": new})
        body = await resp.json()
        await hass.async_block_till_done()

        assert resp.status == 200
        assert body["status"] == "updated"
        assert entry.data[CONF_ACCESS_TOKEN] == new
        assert entry.state is ConfigEntryState.LOADED
        assert entry.runtime_data.coordinator.token_usable
        backfill.assert_called_once()  # reload backfilled the missed hours

        # Posting the same token again is a no-op.
        resp = await client.post("/api/webhook/hook123", json={"token": new})
        assert (await resp.json())["status"] == "unchanged"


async def test_webhook_rejects_bad_tokens(
    hass: HomeAssistant, hass_client_no_auth: ClientSessionGenerator
) -> None:
    """Expired, invalid or missing tokens are refused and nothing is stored."""
    await async_setup_component(hass, "webhook", {})
    old = make_jwt(datetime.now(UTC) - timedelta(hours=3))
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        data={CONF_ACCESS_TOKEN: old, CONF_CPE: CPE, CONF_WEBHOOK_ID: "hook456"},
        unique_id=CPE,
    )
    entry.add_to_hass(hass)

    with patch(
        GET_CONSUMPTION, new=AsyncMock(side_effect=ERedesAuthenticationError("no"))
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        client = await hass_client_no_auth()

        resp = await client.post("/api/webhook/hook456", json={})
        assert resp.status == 400

        expired = make_jwt(datetime.now(UTC) - timedelta(minutes=1))
        resp = await client.post("/api/webhook/hook456", json={"token": expired})
        assert (await resp.json())["error"] == "token_expired"

        forged = make_jwt(datetime.now(UTC) + timedelta(hours=1))
        resp = await client.post("/api/webhook/hook456", json={"token": forged})
        assert resp.status == 401

    assert entry.data[CONF_ACCESS_TOKEN] == old

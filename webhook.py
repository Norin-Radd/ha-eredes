"""Webhook that receives a fresh E-REDES access token.

A human still logs into the Balcão Digital (the login is reCAPTCHA-protected and
is *not* automated). Whatever then holds the new ``aat`` — the companion browser
extension in ``browser-extension/``, an n8n flow, a curl — POSTs it here:

    POST /api/webhook/<webhook_id>
    {"token": "<aat value or full Cookie header>"}

The token is validated against E-REDES for this entry's CPE before it is
stored, so the endpoint cannot be used to plant an arbitrary credential. On
success the entry is reloaded, which runs the historical import and backfills
every hour since the last one stored.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiohttp import web
from homeassistant.components import persistent_notification
from homeassistant.components import webhook as ha_webhook
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_ACCESS_TOKEN, CONF_CPE, CONF_WEBHOOK_ID, DOMAIN
from .eredes_api import ERedesClient

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

MAX_TOKEN_LENGTH = 8192


def _error(code: str, status: int) -> web.Response:
    return web.json_response({"ok": False, "error": code}, status=status)


def async_ensure_webhook_id(hass: HomeAssistant, entry: ConfigEntry) -> str:
    """Return the entry's webhook id, creating (and announcing) it if missing."""
    if webhook_id := entry.data.get(CONF_WEBHOOK_ID):
        return str(webhook_id)

    webhook_id = ha_webhook.async_generate_id()
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_WEBHOOK_ID: webhook_id}
    )
    persistent_notification.async_create(
        hass,
        (
            "Foi criado um webhook para receber tokens novos da E-REDES para o "
            f"CPE …{entry.data[CONF_CPE][-8:]}.\n\n"
            f"Caminho: `/api/webhook/{webhook_id}`\n\n"
            "Junta-o ao URL do teu Home Assistant (ex.: "
            f"`http://homeassistant.local:8123/api/webhook/{webhook_id}`) e "
            "coloca-o nas opções da extensão do browser. Trata este URL como uma "
            "palavra-passe."
        ),
        title="E-REDES: webhook do token",
        notification_id=f"{DOMAIN}_webhook_{entry.entry_id}",
    )
    return webhook_id


def async_register_token_webhook(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Register the token webhook for a config entry."""
    webhook_id = async_ensure_webhook_id(hass, entry)
    entry_id = entry.entry_id

    async def _handle(
        hass: HomeAssistant, _webhook_id: str, request: web.Request
    ) -> web.Response:
        target = hass.config_entries.async_get_entry(entry_id)
        if target is None:
            return _error("unknown_entry", 404)

        try:
            body = await request.json()
        except ValueError:
            body = {}
        token = ""
        if isinstance(body, dict):
            token = str(body.get("token") or body.get("cookie") or "").strip()
        if not token or len(token) > MAX_TOKEN_LENGTH:
            return _error("missing_token", 400)

        client = ERedesClient(async_get_clientsession(hass), token)
        if client.token_is_expired():
            return _error("token_expired", 400)

        current = ERedesClient(
            async_get_clientsession(hass), str(target.data[CONF_ACCESS_TOKEN])
        )
        if client.access_token == current.access_token:
            return web.json_response({"ok": True, "status": "unchanged"})

        if not await client.validate_token(target.data[CONF_CPE]):
            _LOGGER.warning("E-REDES token received by webhook was rejected")
            return _error("invalid_token", 401)

        hass.config_entries.async_update_entry(
            target, data={**target.data, CONF_ACCESS_TOKEN: client.access_token}
        )
        # Reload after this response is sent: the reload unregisters this webhook.
        hass.config_entries.async_schedule_reload(entry_id)

        expires_at = client.token_expires_at
        _LOGGER.info(
            "New E-REDES token stored for CPE %s via webhook",
            target.data[CONF_CPE][-8:],
        )
        return web.json_response(
            {
                "ok": True,
                "status": "updated",
                "expires_at": expires_at.isoformat() if expires_at else None,
            }
        )

    ha_webhook.async_register(
        hass,
        DOMAIN,
        f"E-REDES token ({entry.data[CONF_CPE][-8:]})",
        webhook_id,
        _handle,
        allowed_methods=["POST"],
    )
    entry.async_on_unload(lambda: ha_webhook.async_unregister(hass, webhook_id))

"""Mi Fitness CN(小米运动健康 CN)Home Assistant integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .api import MiFitnessApiError, MiFitnessClient
from .const import (
    CONF_C_USER_ID,
    CONF_PHONE_ID,
    CONF_REGION,
    CONF_SERVICE_TOKEN,
    CONF_SSECURITY,
    CONF_USER_ID,
    DEFAULT_REGION,
    DOMAIN,
)
from .coordinator import MiFitnessCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Mi Fitness CN from a config entry."""
    data = entry.data

    client = MiFitnessClient(
        user_id=data[CONF_USER_ID],
        c_user_id=data[CONF_C_USER_ID],
        ssecurity=data[CONF_SSECURITY],
        service_token=data[CONF_SERVICE_TOKEN],
        region=data.get(CONF_REGION, DEFAULT_REGION),
        phone_id=data.get(CONF_PHONE_ID, ""),
    )

    coordinator = MiFitnessCoordinator(hass, client, entry)

    # Load stored watermark before first refresh
    await coordinator.async_load_stored_state()

    try:
        await coordinator.async_config_entry_first_refresh()
    except MiFitnessApiError as exc:
        raise ConfigEntryNotReady(f"API not available: {exc}") from exc

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok

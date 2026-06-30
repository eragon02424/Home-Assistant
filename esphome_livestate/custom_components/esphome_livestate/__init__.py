"""ESPHome LiveState - creates Online/Offline binary_sensor entities for ESPHome devices.

Communicates with the MCP ESPHome addon (port 8090) to get device states.
If the addon is not running, the integration will show a configuration error.

Entities are attached to the correct HA device via MAC address matching,
exactly like PowerCalc attaches power sensors to existing devices.
"""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .coordinator import ESPHomeLiveStateCoordinator

_LOGGER = logging.getLogger(__name__)

DOMAIN = "esphome_livestate"
PLATFORMS = ["binary_sensor"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up ESPHome LiveState from a config entry."""
    coordinator = ESPHomeLiveStateCoordinator(hass, entry)

    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception as err:
        raise ConfigEntryNotReady(
            f"Cannot connect to MCP ESPHome addon: {err}"
        ) from err

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload ESPHome LiveState config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok

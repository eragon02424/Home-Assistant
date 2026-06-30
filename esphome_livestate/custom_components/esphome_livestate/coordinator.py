"""DataUpdateCoordinator for ESPHome LiveState."""
from __future__ import annotations

import logging
from datetime import timedelta

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

_LOGGER = logging.getLogger(__name__)

UPDATE_INTERVAL = timedelta(seconds=15)


class ESPHomeLiveStateCoordinator(DataUpdateCoordinator):
    """Fetches device list from MCP ESPHome addon every 15 seconds."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.addon_url = entry.data["addon_url"].rstrip("/")
        self.bearer_token = entry.data.get("bearer_token", "")
        super().__init__(
            hass,
            _LOGGER,
            name="ESPHome LiveState",
            update_interval=UPDATE_INTERVAL,
        )

    async def _async_update_data(self) -> list[dict]:
        """Fetch device list from addon."""
        headers = {}
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self.addon_url}/devices",
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        raise UpdateFailed(f"Addon returned HTTP {resp.status}")
                    return await resp.json()
        except aiohttp.ClientError as err:
            raise UpdateFailed(f"Cannot connect to MCP ESPHome addon: {err}") from err

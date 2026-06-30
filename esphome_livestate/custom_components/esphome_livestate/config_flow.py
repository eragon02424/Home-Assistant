"""Config flow for ESPHome LiveState."""
from __future__ import annotations

import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import HomeAssistant

from . import DOMAIN

DEFAULT_URL = "http://localhost:8090"
DEFAULT_TOKEN = ""


async def _test_connection(url: str, token: str) -> bool:
    """Test if the MCP ESPHome addon is reachable."""
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{url.rstrip('/')}/health",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                return resp.status == 200
    except Exception:
        return False


class ESPHomeLiveStateConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for ESPHome LiveState."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors = {}

        if user_input is not None:
            url = user_input["addon_url"].rstrip("/")
            token = user_input.get("bearer_token", "")

            ok = await _test_connection(url, token)
            if ok:
                return self.async_create_entry(
                    title="ESPHome LiveState",
                    data={"addon_url": url, "bearer_token": token},
                )
            errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required("addon_url", default=DEFAULT_URL): str,
                vol.Optional("bearer_token", default=DEFAULT_TOKEN): str,
            }),
            errors=errors,
        )

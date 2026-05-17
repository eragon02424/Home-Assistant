"""Repairs flow for MCP Proxy.

Surfaces a "click submit to restart" card in HA's Repairs UI when the addon
has detected that OAuth is enabled but the integration code currently loaded
in HA doesn't enforce it.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import voluptuous as vol
from homeassistant import data_entry_flow
from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

_LOGGER = logging.getLogger(__name__)

ISSUE_ID = "oauth_restart_required"
RESTART_MARKER_FILE = Path("/config/.mcp_proxy_oauth_restart_required")


class OAuthRestartRepairFlow(RepairsFlow):
    """Single-step confirmation flow that restarts Home Assistant."""

    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> data_entry_flow.FlowResult:
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, str] | None = None
    ) -> data_entry_flow.FlowResult:
        if user_input is not None:
            await self.hass.async_add_executor_job(_clear_marker)
            await self.hass.services.async_call(
                "homeassistant", "restart", {}, blocking=False
            )
            return self.async_create_entry(data={})
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
        )


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, Any] | None,
) -> RepairsFlow:
    """Factory hook called by the repairs platform."""
    return OAuthRestartRepairFlow()


def _clear_marker() -> None:
    """Delete the marker file if present."""
    try:
        RESTART_MARKER_FILE.unlink(missing_ok=True)
    except OSError as e:
        _LOGGER.warning(
            "MCP Proxy: could not delete OAuth restart marker at %s "
            "(%s: %s) — Repair card may re-appear on next HA boot.",
            RESTART_MARKER_FILE,
            type(e).__name__,
            e,
        )


def _delete_issue_only(hass: HomeAssistant, domain: str) -> None:
    """Dismiss the Repair issue without touching the marker file."""
    ir.async_delete_issue(hass, domain, ISSUE_ID)


def marker_present() -> bool:
    """Sync helper for use under `hass.async_add_executor_job`."""
    return RESTART_MARKER_FILE.exists()


def maybe_create_issue(hass: HomeAssistant, domain: str) -> None:
    """Register the repair issue iff the marker file is present."""
    if not marker_present():
        return
    ir.async_create_issue(
        hass,
        domain,
        ISSUE_ID,
        is_fixable=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_ID,
    )


def clear_issue(hass: HomeAssistant, domain: str) -> None:
    """Dismiss the repair issue and delete the marker file."""
    _clear_marker()
    ir.async_delete_issue(hass, domain, ISSUE_ID)

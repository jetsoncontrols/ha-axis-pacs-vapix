"""Constants for the Axis PACS integration."""

from __future__ import annotations

from homeassistant.const import Platform

DOMAIN = "axis_pacs"
PLATFORMS = [Platform.LOCK]

CONF_USE_HTTPS = "use_https"

DEFAULT_USERNAME = "root"
DEFAULT_PORT = 0  # 0 = use the scheme default (80/443)

MANUFACTURER = "Axis Communications"

"""Constants for the Axis PACS integration."""

from __future__ import annotations

from homeassistant.const import Platform

DOMAIN = "axis_pacs"
PLATFORMS = [Platform.LOCK]

CONF_USE_HTTPS = "use_https"
# Per-instance opt-in for managing the cluster-wide access-code database. Default
# off so only a designated controller exposes the (shared) credential services.
CONF_MANAGE_CODES = "manage_codes"

DEFAULT_USERNAME = "root"
DEFAULT_PORT = 0  # 0 = use the scheme default (80/443)
DEFAULT_MANAGE_CODES = False

MANUFACTURER = "Axis Communications"

# --- Access-code management services (cluster-wide; routed via a controller) ---
SERVICE_ADD_PIN = "add_pin"
SERVICE_REMOVE_CREDENTIAL = "remove_credential"
SERVICE_SET_CREDENTIAL_ENABLED = "set_credential_enabled"
SERVICE_LIST_CREDENTIALS = "list_credentials"
SERVICE_LIST_ACCESS_PROFILES = "list_access_profiles"

ATTR_CONFIG_ENTRY_ID = "config_entry_id"
ATTR_NAME = "name"
ATTR_PIN = "pin"
ATTR_ACCESS_PROFILE_TOKENS = "access_profile_tokens"
ATTR_ENABLED = "enabled"
ATTR_CREDENTIAL_TOKEN = "credential_token"
ATTR_INCLUDE_PINS = "include_pins"

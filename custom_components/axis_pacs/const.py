"""Constants for the Axis PACS integration."""

from __future__ import annotations

from homeassistant.const import Platform

DOMAIN = "axis_pacs"
PLATFORMS = [Platform.LOCK]

CONF_USE_HTTPS = "use_https"
# Per-instance opt-in for managing the cluster-wide access-code database. Default
# off so only a designated controller exposes the (shared) credential services.
CONF_MANAGE_CODES = "manage_codes"
# When False (default), code management requires an HA admin (UI + services).
# When True, ANY logged-in user may manage codes — use the dashboard section's
# `visibility` to limit who sees the card (e.g. property managers who should NOT
# be HA admins). Server-side gate + the card's controls both honour this.
CONF_MANAGE_ALLOW_NON_ADMIN = "manage_allow_non_admin"

DEFAULT_USERNAME = "root"
DEFAULT_PORT = 0  # 0 = use the scheme default (80/443)
DEFAULT_MANAGE_CODES = False
DEFAULT_MANAGE_ALLOW_NON_ADMIN = False

MANUFACTURER = "Axis Communications"

# --- Access-code management services (cluster-wide; routed via a controller) ---
SERVICE_ADD_PIN = "add_pin"
SERVICE_ADD_CREDENTIAL = "add_credential"
SERVICE_GENERATE_CODE = "generate_code"
SERVICE_REMOVE_CREDENTIAL = "remove_credential"
SERVICE_SET_CREDENTIAL_ENABLED = "set_credential_enabled"
SERVICE_LIST_CREDENTIALS = "list_credentials"
SERVICE_LIST_ACCESS_PROFILES = "list_access_profiles"
SERVICE_LIST_USERS = "list_users"
SERVICE_LIST_DOORS = "list_doors"
SERVICE_LIST_SCHEDULES = "list_schedules"
SERVICE_SET_USER = "set_user"
SERVICE_REMOVE_USER = "remove_user"
SERVICE_SET_CREDENTIAL_ACCESS_PROFILES = "set_credential_access_profiles"
SERVICE_SET_CREDENTIAL_CODE = "set_credential_code"
SERVICE_SET_CREDENTIAL_VALIDITY = "set_credential_validity"
SERVICE_ENSURE_DOOR_PROFILE = "ensure_door_profile"

ATTR_CONFIG_ENTRY_ID = "config_entry_id"
ATTR_NAME = "name"
ATTR_PIN = "pin"
ATTR_ACCESS_PROFILE_TOKENS = "access_profile_tokens"
ATTR_ENABLED = "enabled"
ATTR_CREDENTIAL_TOKEN = "credential_token"
ATTR_INCLUDE_PINS = "include_pins"
ATTR_USER_TOKEN = "user_token"
ATTR_FIRST_NAME = "first_name"
ATTR_LAST_NAME = "last_name"
ATTR_DESCRIPTION = "description"
ATTR_KIND = "kind"  # credential type: "pin" | "card"
ATTR_CODE = "code"  # the PIN digits or card number
ATTR_LENGTH = "length"  # generated-code length override
ATTR_DOOR_TOKEN = "door_token"
ATTR_SCHEDULE_TOKEN = "schedule_token"
# Validity window (start/end dates) + what to do once the end date passes.
ATTR_VALID_FROM = "valid_from"  # "YYYY-MM-DD" or "" to clear (date only)
ATTR_VALID_TO = "valid_to"  # "YYYY-MM-DD" or "" to clear (date only)
ATTR_EXPIRE_ACTION = "expire_action"  # "disable" | "delete"

# The device exposes no configured code length (attribute lists are empty), so
# generated codes derive their length from the existing codes; this is the
# fallback when there are none. SI2 uses 5-digit numbers.
DEFAULT_CODE_LENGTH = 5

# Built-in 24/7 schedule; the default for a per-door grant ("all the time").
ALWAYS_SCHEDULE_TOKEN = "standard_always"

# --- Validity window + expiry reaper ---
# The controller natively enforces a per-credential ValidFrom/ValidTo window
# (ONVIF tcr credential service, CredentialValiditySupported=true) — DATE ONLY
# (ValiditySupportsTimeValue=false), so an end date means "valid through that
# calendar day". Outside the window the controller DENIES the credential on its
# own (works even with HA down). The Axis-native `pacsaxis` schema has no
# validity fields, so the window is set/read via the ONVIF `tcr` service.
#
# Disabling/deleting a credential AFTER its end date has NO on-device mechanism
# (no native purge; the only on-device actor would be a heavyweight ACAP), so a
# small daily HA "reaper" applies it. The reaper acts ONLY on credentials the
# card explicitly configured an expiry action for (recorded below) — it never
# touches a credential whose ValidTo was set out-of-band.
EXPIRE_ACTION_DISABLE = "disable"  # keep the credential (code stays reserved)
EXPIRE_ACTION_DELETE = "delete"  # remove the credential (+ orphaned cardholder)
EXPIRE_ACTIONS = (EXPIRE_ACTION_DISABLE, EXPIRE_ACTION_DELETE)
# Default disable: an expired-by-ValidTo credential is already denied AND still
# in the DB, so generate_code's uniqueness scan keeps avoiding its code (never
# re-issued). Delete frees the code for reuse but declutters.
DEFAULT_EXPIRE_ACTION = EXPIRE_ACTION_DISABLE

# Per-credential expiry action, persisted (token -> "disable"|"delete"). Acts as
# the reaper's opt-in set. Worst-case loss = falls back to the safe default and
# the native window still denies.
EXPIRE_ACTIONS_STORAGE_KEY = "axis_pacs_expire_actions"
EXPIRE_ACTIONS_STORAGE_VERSION = 1
EXPIRE_ACTIONS_SAVE_DELAY = 5  # seconds — debounce store writes
DATA_EXPIRE_ACTIONS = "expire_actions"  # {credential_token: "disable"|"delete"}
DATA_EXPIRE_ACTIONS_STORE = "expire_actions_store"

# Reaper cadence: once daily (date-granularity matches) at a quiet hour, plus
# once shortly after startup so a restart doesn't skip a day.
REAPER_HOUR = 3
REAPER_MINUTE = 17
REAPER_STARTUP_DELAY = 180  # seconds after setup before the first run

# Credential type -> the Axis IdData key that holds the value. PINs are raw
# ASCII digits under "PIN"; cards store the number under "CardNr" (the "Card"
# key sits empty on this controller).
CREDENTIAL_KIND_ID_KEY = {"pin": "PIN", "card": "CardNr"}

# --- Access-event "last used" tracking ---
# Access events on these topics name the credential that was used (CredentialToken
# + CredentialHolderName) and the reader (AccessPointToken). We record a per-
# credential "last used" timestamp from them. AccessGranted fires when a code is
# accepted; AccessTaken when the door is actually opened — either counts as "used".
ACCESS_USED_TOPIC_SUFFIXES = (
    "AccessControl/AccessGranted/Credential",
    "AccessControl/AccessTaken/Credential",
)
# Shared, persisted across restarts; forward-only (fills in as cards are used).
LAST_USED_STORAGE_KEY = "axis_pacs_last_used"
LAST_USED_STORAGE_VERSION = 1
LAST_USED_SAVE_DELAY = 30  # seconds — debounce rapid event bursts
# hass.data[DOMAIN] keys.
DATA_LAST_USED = "last_used"  # {credential_token: {"time", "door", "holder"}}
DATA_LAST_USED_STORE = "last_used_store"
DATA_BACKFILL_DONE = "last_used_backfilled"  # one-time-per-session guard

# One-time historical backfill of "last used" from the controller's event log
# (JSON EventLogger API). Runs once on the manage_codes controller (its log is
# cluster-wide via global event distribution). The device caps each response
# (~1000) and exposes no topic/time/pagination filter, so the backfill samples
# the NEWEST and OLDEST windows and keeps the latest access per credential —
# best-effort: credentials with no access in those windows stay blank, and live
# events always override. ``Descending`` is the ONLY way to reach recent events.
BACKFILL_EVENT_LIMIT = 1000

# --- Frontend (bundled Lovelace management card) ---
# Served as a static path off the integration and injected as an extra JS module
# so the custom card auto-registers wherever this integration is installed (the
# browser_mod pattern). No HACS frontend entry / Lovelace resource needed.
FRONTEND_URL_BASE = "/axis_pacs/frontend"
FRONTEND_DIR = "frontend"
FRONTEND_CARD_FILENAME = "axis-pacs-codes-card.js"

# WebSocket command the card uses to self-discover which controller(s) expose the
# cluster-wide code services (i.e. have manage_codes enabled).
WS_TYPE_MANAGERS = "axis_pacs/managers"

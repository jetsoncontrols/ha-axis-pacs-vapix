#!/usr/bin/env bash
# Reconnaissance probe for the Axis ACCESS-MANAGEMENT (credential / PIN / card)
# stack on an Axis controller (A1001 / A1601 / A1610 / A1210).
#
# Where probe.sh answers "how do we control doors?", this answers "how do we
# manage who can open them?" — the ONVIF Profile-C management services that back
# the AXIS Entry Manager: Credential (cardholders + PINs/cards), AccessRules
# (who-opens-what-when), Schedule (time profiles), AccessControl (access points).
#
# READ-ONLY: issues NO create/modify/delete. Only Get* / *InfoList enumerations.
#
# Usage:
#   AXIS_HOST=192.168.1.50 AXIS_USER=root AXIS_PASS=secret ./tools/probe-credentials.sh
#   ./tools/probe-credentials.sh 192.168.1.50 root secret
#
# Optional: AXIS_PROTO=https to probe over TLS (self-signed certs are accepted).
set -uo pipefail

HOST="${1:-${AXIS_HOST:-}}"
AXUSER="${2:-${AXIS_USER:-root}}"
AXPASS="${3:-${AXIS_PASS:-}}"

if [[ -z "$HOST" || -z "$AXPASS" ]]; then
  echo "Usage: AXIS_HOST=<ip> AXIS_USER=<user> AXIS_PASS=<pass> $0" >&2
  echo "   or: $0 <ip> <user> <pass>" >&2
  exit 2
fi

PROTO="${AXIS_PROTO:-http}"
BASE="$PROTO://$HOST"
CURL=(curl -ksS --anyauth -u "$AXUSER:$AXPASS" --connect-timeout 5 --max-time 20)

hr()   { printf '\n========== %s ==========\n' "$1"; }
note() { printf '    (%s)\n' "$1"; }

# post_soap <path> <body-xml>
post_soap() {
  "${CURL[@]}" -H 'Content-Type: application/soap+xml; charset=utf-8' -X POST --data "$2" "$BASE$1" \
    -w '\n[HTTP %{http_code}] %{url_effective}\n'
}
# env <inner-xml> -> full SOAP 1.2 envelope (no WS-Addressing; these mgmt calls don't need it)
env() { printf '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Body>%s</s:Body></s:Envelope>' "$1"; }

DEV="http://www.onvif.org/ver10/device/wsdl"
TCR="http://www.onvif.org/ver10/credential/wsdl"
TAR="http://www.onvif.org/ver10/accessrules/wsdl"
TSC="http://www.onvif.org/ver10/schedule/wsdl"
TAC="http://www.onvif.org/ver10/accesscontrol/wsdl"

# ---------------------------------------------------------------------------- #
hr "MASTER LIST — ONVIF GetServices  (-> /vapix/services)"
note "the key call: one response lists every ONVIF service this firmware implements"
note "look for credential/accessrules/schedule/accesscontrol namespaces below"
post_soap /vapix/services "$(env "<GetServices xmlns=\"$DEV\"><IncludeCapability>true</IncludeCapability></GetServices>")"

hr "GetServices — fallback endpoint  (-> /onvif/device_service)"
note "Axis sometimes hosts ONVIF device-mgmt here instead of /vapix/services"
post_soap /onvif/device_service "$(env "<GetServices xmlns=\"$DEV\"><IncludeCapability>true</IncludeCapability></GetServices>")"

# ---------------------------------------------------------------------------- #
hr "CREDENTIAL — GetServiceCapabilities  (-> /vapix/services)"
note "200 => PIN/card management is available; capabilities show max PIN length etc."
post_soap /vapix/services "$(env "<GetServiceCapabilities xmlns=\"$TCR\"/>")"

hr "CREDENTIAL — GetCredentialInfoList"
note "existing cardholders/credentials (PINs live as CredentialIdentifier format=PIN)"
post_soap /vapix/services "$(env "<GetCredentialInfoList xmlns=\"$TCR\"><Limit>50</Limit></GetCredentialInfoList>")"

hr "CREDENTIAL — GetSupportedFormatTypes (PIN format)"
note "does the device accept a PIN identifier format? what min/max length?"
post_soap /vapix/services "$(env "<GetSupportedFormatTypes xmlns=\"$TCR\"><CredentialIdentifierTypeName>PIN</CredentialIdentifierTypeName></GetSupportedFormatTypes>")"

# ---------------------------------------------------------------------------- #
hr "ACCESS RULES — GetServiceCapabilities"
note "AccessProfiles bind a credential to (door + schedule); needed for a PIN to work"
post_soap /vapix/services "$(env "<GetServiceCapabilities xmlns=\"$TAR\"/>")"

hr "ACCESS RULES — GetAccessProfileInfoList"
post_soap /vapix/services "$(env "<GetAccessProfileInfoList xmlns=\"$TAR\"><Limit>50</Limit></GetAccessProfileInfoList>")"

# ---------------------------------------------------------------------------- #
hr "SCHEDULE — GetServiceCapabilities"
note "time schedules / special days for time-bounded codes"
post_soap /vapix/services "$(env "<GetServiceCapabilities xmlns=\"$TSC\"/>")"

hr "SCHEDULE — GetScheduleInfoList"
post_soap /vapix/services "$(env "<GetScheduleInfoList xmlns=\"$TSC\"><Limit>50</Limit></GetScheduleInfoList>")"

# ---------------------------------------------------------------------------- #
hr "ACCESS CONTROL — GetServiceCapabilities"
note "access points (door+reader pairings) the credentials are evaluated at"
post_soap /vapix/services "$(env "<GetServiceCapabilities xmlns=\"$TAC\"/>")"

hr "ACCESS CONTROL — GetAccessPointInfoList"
post_soap /vapix/services "$(env "<GetAccessPointInfoList xmlns=\"$TAC\"><Limit>50</Limit></GetAccessPointInfoList>")"

hr "ACCESS CONTROL — GetAreaInfoList"
post_soap /vapix/services "$(env "<GetAreaInfoList xmlns=\"$TAC\"><Limit>50</Limit></GetAreaInfoList>")"

# ---------------------------------------------------------------------------- #
hr "DONE"
echo "Which services returned HTTP 200 with real data tells us what the access-code"
echo "model can be. 200 + data => usable; SOAP Fault 'not implemented' / 404 => absent"
echo "(and we'd fall back to Axis param.cgi Pacs groups or the thirdpartycredential WSDL)."

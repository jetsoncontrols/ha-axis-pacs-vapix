#!/usr/bin/env bash
# Reconnaissance probe for an Axis access controller (A1001 / A1601 / A1610 / A1210).
#
# Discovers the firmware track, which VAPIX APIs are exposed, the doors defined on
# the controller, and whether the WebSocket event stream is advertised — so we can
# decide the integration's API approach before writing the client.
#
# READ-ONLY: this issues NO lock/unlock/access commands. It only reads state.
#
# Usage:
#   AXIS_HOST=192.168.1.50 AXIS_USER=root AXIS_PASS=secret ./tools/probe.sh
#   ./tools/probe.sh 192.168.1.50 root secret
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
# -k: accept self-signed TLS  --anyauth: negotiate digest vs basic  short timeouts
CURL=(curl -ksS --anyauth -u "$AXUSER:$AXPASS" --connect-timeout 5 --max-time 20)

hr()   { printf '\n========== %s ==========\n' "$1"; }
note() { printf '    (%s)\n' "$1"; }

post_json() { # path  json
  "${CURL[@]}" -H 'Content-Type: application/json' -X POST --data "$2" "$BASE$1" \
    -w '\n[HTTP %{http_code}] %{url_effective}\n'
}
post_soap() { # path  xml
  "${CURL[@]}" -H 'Content-Type: application/soap+xml; charset=utf-8' -X POST --data "$2" "$BASE$1" \
    -w '\n[HTTP %{http_code}] %{url_effective}\n'
}
get() { # path
  "${CURL[@]}" "$BASE$1" -w '\n[HTTP %{http_code}] %{url_effective}\n'
}

hr "Device identity  (basicdeviceinfo.cgi)"
note "model / product / serial / firmware version"
post_json /axis-cgi/basicdeviceinfo.cgi '{"apiVersion":"1.0","method":"getAllProperties"}'

hr "Properties  (param.cgi)"
note "fallback identity + firmware; serial == MAC w/o colons on Axis"
get '/axis-cgi/param.cgi?action=list&group=Brand,Properties.System,Properties.Firmware'

hr "API discovery  (apidiscovery.cgi)"
note "look for id 'event-streaming-over-websocket' -> we can use WS push"
post_json /axis-cgi/apidiscovery.cgi '{"apiVersion":"1.0","method":"getApiList"}'

hr "Door Control — GetServiceCapabilities  (SOAP -> /vapix/services)"
note "200 + capabilities here means the VAPIX door-control API is present"
post_soap /vapix/services \
'<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Body><GetServiceCapabilities xmlns="http://www.onvif.org/ver10/doorcontrol/wsdl"/></s:Body></s:Envelope>'

hr "Door Control — GetDoorInfoList  (SOAP -> /vapix/services)"
note "THE key call: lists doors + tokens + per-door capabilities"
post_soap /vapix/services \
'<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Body><GetDoorInfoList xmlns="http://www.onvif.org/ver10/doorcontrol/wsdl"/></s:Body></s:Envelope>'

hr "Door Control — JSON form probe  (-> /vapix/doorcontrol)"
note "if this 200s, the device offers a JSON encoding we may prefer over SOAP"
post_json /vapix/doorcontrol '{"apiVersion":"1.0","method":"getDoorInfoList"}'

hr "PACS / Access Control — JSON form probe  (-> /vapix/pacs)"
note "access points / credential subsystem availability (future features)"
post_json /vapix/pacs '{"apiVersion":"1.0","method":"getAccessPointList"}'

hr "DONE"
echo "Read the output above: which endpoints returned HTTP 200 with real data"
echo "vs 401 (auth) / 404 (not present) / SOAP Fault. Paste it back to decide the"
echo "client approach. Next pass will read per-door state with GetDoorState <token>."

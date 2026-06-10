"""SOAP/ONVIF envelope builders and response parsers for Axis VAPIX.

Door control is exposed only as SOAP/ONVIF on the VAPIX-OS firmware track
(``POST /vapix/services``); events use ONVIF WS-Eventing PullPoint on the same
endpoint. Responses use varying namespace prefixes, so parsing matches on the
stable namespace *URIs* rather than prefixes.

Home Assistant independent.
"""

from __future__ import annotations

import uuid
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

from .models import (
    DeviceIdentity,
    Door,
    DoorCapabilities,
    DoorMode,
    DoorState,
    Notification,
)

# --- Namespace URIs (stable across firmware; prefixes vary) ---
S = "http://www.w3.org/2003/05/soap-envelope"
A = "http://www.w3.org/2005/08/addressing"
TDC = "http://www.onvif.org/ver10/doorcontrol/wsdl"
TEV = "http://www.onvif.org/ver10/events/wsdl"
WSNT = "http://docs.oasis-open.org/wsn/b-2"
TT = "http://www.onvif.org/ver10/schema"
AXEVENT = "http://www.axis.com/2009/event"
ANON = "http://www.w3.org/2005/08/addressing/anonymous"

# --- WS-Addressing actions ---
ACTION_CREATE_PULLPOINT = f"{TEV}/EventPortType/CreatePullPointSubscriptionRequest"
ACTION_PULL = f"{TEV}/PullPointSubscription/PullMessagesRequest"
ACTION_RENEW = "http://docs.oasis-open.org/wsn/bw-2/SubscriptionManager/RenewRequest"
ACTION_UNSUBSCRIBE = (
    "http://docs.oasis-open.org/wsn/bw-2/SubscriptionManager/UnsubscribeRequest"
)


class SoapFault(Exception):
    """Raised when a 200 response body carries a SOAP fault."""


def _envelope(body: str, header: str = "") -> str:
    head = f"<s:Header>{header}</s:Header>" if header else ""
    return f'<s:Envelope xmlns:s="{S}" xmlns:a="{A}">{head}<s:Body>{body}</s:Body></s:Envelope>'


def _addr_header(action: str, to: str, extra: str = "") -> str:
    return (
        f'<a:Action s:mustUnderstand="1">{action}</a:Action>'
        f"<a:MessageID>urn:uuid:{uuid.uuid4()}</a:MessageID>"
        f'<a:To s:mustUnderstand="1">{escape(to)}</a:To>'
        f"<a:ReplyTo><a:Address>{ANON}</a:Address></a:ReplyTo>"
        f"{extra}"
    )


def _sub_id(subscription_id: str) -> str:
    return (
        f'<dom0:SubscriptionId xmlns:dom0="{AXEVENT}">'
        f"{escape(subscription_id)}</dom0:SubscriptionId>"
    )


# --------------------------------------------------------------------------- #
# Door control requests
# --------------------------------------------------------------------------- #
def get_service_capabilities() -> str:
    return _envelope(f'<GetServiceCapabilities xmlns="{TDC}"/>')


def get_door_info_list() -> str:
    return _envelope(f'<GetDoorInfoList xmlns="{TDC}"/>')


def get_door_state(token: str) -> str:
    return _envelope(
        f'<GetDoorState xmlns="{TDC}"><Token>{escape(token)}</Token></GetDoorState>'
    )


def door_command(operation: str, token: str) -> str:
    """Build a single-token door command (LockDoor/UnlockDoor/AccessDoor/...)."""
    return _envelope(
        f'<{operation} xmlns="{TDC}"><Token>{escape(token)}</Token></{operation}>'
    )


# --------------------------------------------------------------------------- #
# Event (WS-Eventing PullPoint) requests
# --------------------------------------------------------------------------- #
def create_pull_point(to: str, termination: str) -> str:
    body = (
        f'<CreatePullPointSubscription xmlns="{TEV}">'
        f"<InitialTerminationTime>{termination}</InitialTerminationTime>"
        f"</CreatePullPointSubscription>"
    )
    return _envelope(body, _addr_header(ACTION_CREATE_PULLPOINT, to))


def pull_messages(to: str, subscription_id: str, timeout: str, limit: int) -> str:
    body = (
        f'<PullMessages xmlns="{TEV}">'
        f"<Timeout>{timeout}</Timeout><MessageLimit>{limit}</MessageLimit>"
        f"</PullMessages>"
    )
    return _envelope(body, _addr_header(ACTION_PULL, to, _sub_id(subscription_id)))


def renew(to: str, subscription_id: str, termination: str) -> str:
    body = f'<Renew xmlns="{WSNT}"><TerminationTime>{termination}</TerminationTime></Renew>'
    return _envelope(body, _addr_header(ACTION_RENEW, to, _sub_id(subscription_id)))


def unsubscribe(to: str, subscription_id: str) -> str:
    body = f'<Unsubscribe xmlns="{WSNT}"/>'
    return _envelope(
        body, _addr_header(ACTION_UNSUBSCRIBE, to, _sub_id(subscription_id))
    )


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def parse(xml: bytes | str) -> ET.Element:
    """Parse a SOAP response, raising :class:`SoapFault` on a fault body."""
    root = ET.fromstring(xml)
    fault = root.find(f".//{{{S}}}Fault")
    if fault is not None:
        reason = fault.find(f"{{{S}}}Reason/{{{S}}}Text")
        subcode = fault.find(f"{{{S}}}Code/{{{S}}}Subcode/{{{S}}}Value")
        message = (
            (reason.text if reason is not None else None)
            or (subcode.text if subcode is not None else None)
            or "SOAP Fault"
        )
        raise SoapFault(message)
    return root


def parse_door_info_list(root: ET.Element) -> list[Door]:
    doors: list[Door] = []
    for info in root.iter(f"{{{TDC}}}DoorInfo"):
        token = info.get("token", "")
        name_el = info.find(f"{{{TDC}}}Name")
        desc_el = info.find(f"{{{TDC}}}Description")
        caps_el = info.find(f"{{{TDC}}}Capabilities")
        name = (name_el.text or "").strip() if name_el is not None else ""
        doors.append(
            Door(
                token=token,
                name=name or token,
                description=(desc_el.text or "").strip() if desc_el is not None else "",
                capabilities=DoorCapabilities.from_attrib(
                    caps_el.attrib if caps_el is not None else {}
                ),
            )
        )
    return doors


def parse_door_state(root: ET.Element) -> DoorState:
    mode_el = root.find(f".//{{{TDC}}}DoorMode")
    return DoorState(mode=DoorMode.parse(mode_el.text if mode_el is not None else None))


def parse_create_pull_point(root: ET.Element) -> tuple[str | None, str | None]:
    """Return ``(subscription_id, subscription_address)`` from the response."""
    addr_el = root.find(f".//{{{TEV}}}SubscriptionReference/{{{A}}}Address")
    sub_el = root.find(f".//{{{AXEVENT}}}SubscriptionId")
    return (
        sub_el.text if sub_el is not None else None,
        addr_el.text if addr_el is not None else None,
    )


def parse_notifications(root: ET.Element) -> list[Notification]:
    out: list[Notification] = []
    for nm in root.iter(f"{{{WSNT}}}NotificationMessage"):
        topic_el = nm.find(f"{{{WSNT}}}Topic")
        topic = (topic_el.text or "").strip() if topic_el is not None else ""
        msg = nm.find(f"{{{WSNT}}}Message/{{{TT}}}Message")
        source: dict[str, str] = {}
        data: dict[str, str] = {}
        utc: str | None = None
        if msg is not None:
            utc = msg.get("UtcTime")
            for item in msg.findall(f"{{{TT}}}Source/{{{TT}}}SimpleItem"):
                name = item.get("Name")
                if name is not None:
                    source[name] = item.get("Value", "")
            for item in msg.findall(f"{{{TT}}}Data/{{{TT}}}SimpleItem"):
                name = item.get("Name")
                if name is not None:
                    data[name] = item.get("Value", "")
        out.append(Notification(topic=topic, source=source, data=data, utc_time=utc))
    return out


def parse_identity(param_text: str) -> DeviceIdentity:
    """Parse ``param.cgi`` ``key=value`` output into a :class:`DeviceIdentity`."""
    params: dict[str, str] = {}
    for line in param_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        params[key.strip()] = value.strip()
    return DeviceIdentity(
        serial=params.get("root.Properties.System.SerialNumber", ""),
        model=params.get("root.Brand.ProdNbr", ""),
        product_full_name=params.get("root.Brand.ProdFullName", ""),
        product_short_name=params.get("root.Brand.ProdShortName", ""),
        firmware=params.get("root.Properties.Firmware.Version", ""),
        product_type=params.get("root.Brand.ProdType", ""),
    )

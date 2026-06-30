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
    AccessPoint,
    AccessPolicy,
    AccessProfile,
    Credential,
    DeviceIdentity,
    Door,
    DoorCapabilities,
    DoorMode,
    DoorState,
    Notification,
    Schedule,
    TcrCredential,
    User,
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

# --- Access-code / credential management (Axis-native + ONVIF Profile-C) ---
# Axis controllers (AXIS Entry Manager) manage cardholders + PINs through these
# Axis-native services; access *rules* (which door + schedule) are ONVIF.
PX = "http://www.axis.com/vapix/ws/pacs"  # Axis pacs: credentials (PIN/card)
UDB = "http://www.axis.com/vapix/ws/user"  # Axis user DB: cardholders
TAR = "http://www.onvif.org/ver10/accessrules/wsdl"  # access profiles
TSC = "http://www.onvif.org/ver10/schedule/wsdl"  # schedules
TAC = "http://www.onvif.org/ver10/accesscontrol/wsdl"  # access points
# ONVIF Credential service — same credential DB as ``PX`` (identical tokens) but
# the only view carrying the validity window (ValidFrom/ValidTo). PT supplies the
# identifier-type QNames (pt:PIN / pt:Card) used inside CredentialIdentifier.
TCR = "http://www.onvif.org/ver10/credential/wsdl"
PT = "http://www.onvif.org/ver10/pacs"

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


# --------------------------------------------------------------------------- #
# Access-code / credential management
#
# Cardholders live in the Axis user DB (``UDB``); PIN/card credentials live in
# Axis pacs (``PX``); the access *rules* they grant (door + schedule) are the
# ONVIF AccessRules (``TAR``) / Schedule (``TSC``) / AccessControl (``TAC``)
# services. PINs are stored/transmitted as raw ASCII (NOT hex or base64 — that
# is only the vanilla-ONVIF ``tcr`` encoding, which this path avoids).
# --------------------------------------------------------------------------- #
def _list_body(limit: int, start: str | None) -> str:
    body = f"<Limit>{limit}</Limit>"
    if start:
        body += f"<StartReference>{escape(start)}</StartReference>"
    return body


# --- read requests --- #
def get_user_list(limit: int = 100, start: str | None = None) -> str:
    return _envelope(f'<GetUserList xmlns="{UDB}">{_list_body(limit, start)}</GetUserList>')


def get_credential_list(limit: int = 100, start: str | None = None) -> str:
    return _envelope(
        f'<GetCredentialList xmlns="{PX}">{_list_body(limit, start)}</GetCredentialList>'
    )


def get_credential(token: str) -> str:
    return _envelope(
        f'<GetCredential xmlns="{PX}"><Token>{escape(token)}</Token></GetCredential>'
    )


def get_access_profile_list(limit: int = 100, start: str | None = None) -> str:
    return _envelope(
        f'<GetAccessProfileList xmlns="{TAR}">{_list_body(limit, start)}</GetAccessProfileList>'
    )


def get_schedule_info_list(limit: int = 100, start: str | None = None) -> str:
    return _envelope(
        f'<GetScheduleInfoList xmlns="{TSC}">{_list_body(limit, start)}</GetScheduleInfoList>'
    )


def get_access_point_info_list(limit: int = 100, start: str | None = None) -> str:
    return _envelope(
        f'<GetAccessPointInfoList xmlns="{TAC}">{_list_body(limit, start)}</GetAccessPointInfoList>'
    )


# --- write requests (a token of "" means create; otherwise modify) --- #
def set_user(
    token: str,
    name: str,
    first_name: str | None = None,
    last_name: str | None = None,
    description: str = "",
) -> str:
    attrs = ""
    if first_name is not None:
        attrs += f'<Attribute Name="FirstName" type="string" Value="{escape(first_name)}"/>'
    if last_name is not None:
        attrs += f'<Attribute Name="LastName" type="string" Value="{escape(last_name)}"/>'
    desc = f"<Description>{escape(description)}</Description>" if description else "<Description/>"
    user = f'<User token="{escape(token)}"><Name>{escape(name)}</Name>{desc}{attrs}</User>'
    return _envelope(f'<SetUser xmlns="{UDB}">{user}</SetUser>')


def remove_user(token: str) -> str:
    return _envelope(f'<RemoveUser xmlns="{UDB}"><Token>{escape(token)}</Token></RemoveUser>')


def set_credential(
    token: str,
    user_token: str,
    id_data: dict[str, str],
    access_profile_tokens: list[str],
    *,
    enabled: bool = True,
    description: str = "",
    status: str = "Enabled",
) -> str:
    # Element order must match the controller's schema sequence exactly (the same
    # order it returns on GetCredential): UserToken, Description, Enabled, Status,
    # IdData*, CredentialAccessProfile*. ``Status`` is required — omitting it is
    # rejected with "occurrence violation in element Credential".
    parts = []
    if user_token:
        parts.append(f"<UserToken>{escape(user_token)}</UserToken>")
    if description:
        parts.append(f"<Description>{escape(description)}</Description>")
    parts.append(f"<Enabled>{'true' if enabled else 'false'}</Enabled>")
    parts.append(f"<Status>{escape(status)}</Status>")
    for name, value in id_data.items():
        parts.append(f'<IdData Name="{escape(name)}" Value="{escape(value)}"/>')
    for profile in access_profile_tokens:
        parts.append(
            f"<CredentialAccessProfile><AccessProfile>{escape(profile)}"
            f"</AccessProfile></CredentialAccessProfile>"
        )
    cred = f'<Credential token="{escape(token)}">{"".join(parts)}</Credential>'
    return _envelope(f'<SetCredential xmlns="{PX}">{cred}</SetCredential>')


def remove_credential(token: str) -> str:
    return _envelope(
        f'<RemoveCredential xmlns="{PX}"><Token>{escape(token)}</Token></RemoveCredential>'
    )


def create_access_profile(
    name: str, policies: list[tuple[str, str]], description: str = ""
) -> str:
    """Create an access profile (group). ``policies`` = list of (schedule, entity).

    Element order matters (Name, Description, AccessPolicy*) and the ``token``
    attribute is required even when empty, or the device rejects with
    ``ter:InvalidArgs`` / ``ter:MissingAttr``. AccessPolicy carries no EntityType.
    """
    pol = "".join(
        f"<AccessPolicy><ScheduleToken>{escape(s)}</ScheduleToken>"
        f"<Entity>{escape(e)}</Entity></AccessPolicy>"
        for s, e in policies
    )
    body = (
        f'<CreateAccessProfile xmlns="{TAR}"><AccessProfile token="">'
        f"<Name>{escape(name)}</Name><Description>{escape(description)}</Description>"
        f"{pol}</AccessProfile></CreateAccessProfile>"
    )
    return _envelope(body)


def delete_access_profile(token: str) -> str:
    return _envelope(
        f'<DeleteAccessProfile xmlns="{TAR}"><Token>{escape(token)}</Token></DeleteAccessProfile>'
    )


def set_credential_enabled(token: str, enabled: bool) -> str:
    op = "EnableCredential" if enabled else "DisableCredential"
    return _envelope(f'<{op} xmlns="{PX}"><Token>{escape(token)}</Token></{op}>')


# --- ONVIF tcr: credential validity window (ValidFrom/ValidTo) --- #
def get_tcr_credentials(token: str) -> str:
    """``tcr:GetCredentials`` for one token (full record, incl. validity)."""
    return _envelope(
        f'<GetCredentials xmlns="{TCR}"><Token>{escape(token)}</Token></GetCredentials>'
    )


def get_tcr_credential_list(limit: int = 100, start: str | None = None) -> str:
    """``tcr:GetCredentialList`` — read validity windows in bulk."""
    s = f"<StartReference>{escape(start)}</StartReference>" if start else ""
    return _envelope(
        f'<GetCredentialList xmlns="{TCR}"><Limit>{int(limit)}</Limit>{s}'
        "</GetCredentialList>"
    )


def modify_tcr_credential(
    cred: TcrCredential, valid_from: str | None, valid_to: str | None
) -> str:
    """``tcr:ModifyCredential`` preserving the record, setting the validity window.

    Element order per the tcr schema: Description, CredentialHolderReference,
    ValidFrom, ValidTo, CredentialIdentifier*, CredentialAccessProfile*. Explicit
    ``tcr:``/``pt:`` prefixes so the ``pt:*`` identifier-type QNames resolve.
    Empty ``valid_from``/``valid_to`` clears that bound.
    """
    ids = "".join(
        "<tcr:CredentialIdentifier>"
        f"<tcr:Type><tcr:Name>{escape(i.get('type', ''))}</tcr:Name>"
        f"<tcr:FormatType>{escape(i.get('format', ''))}</tcr:FormatType></tcr:Type>"
        f"<tcr:ExemptedFromAuthentication>{escape(i.get('exempted', 'false'))}"
        "</tcr:ExemptedFromAuthentication>"
        f"<tcr:Value>{escape(i.get('value', ''))}</tcr:Value>"
        "</tcr:CredentialIdentifier>"
        for i in cred.identifiers
    )
    profiles = "".join(
        f"<tcr:CredentialAccessProfile><tcr:AccessProfileToken>{escape(t)}"
        "</tcr:AccessProfileToken></tcr:CredentialAccessProfile>"
        for t in cred.access_profile_tokens
    )
    vf = f"<tcr:ValidFrom>{escape(valid_from)}</tcr:ValidFrom>" if valid_from else ""
    vt = f"<tcr:ValidTo>{escape(valid_to)}</tcr:ValidTo>" if valid_to else ""
    body = (
        f'<tcr:ModifyCredential xmlns:tcr="{TCR}" xmlns:pt="{PT}">'
        f'<tcr:Credential token="{escape(cred.token)}">'
        f"<tcr:Description>{escape(cred.description)}</tcr:Description>"
        f"<tcr:CredentialHolderReference>{escape(cred.holder)}"
        "</tcr:CredentialHolderReference>"
        f"{vf}{vt}{ids}{profiles}"
        "</tcr:Credential></tcr:ModifyCredential>"
    )
    return _envelope(body)


# --- parsing --- #
def _text(parent: ET.Element, tag: str, ns: str) -> str:
    el = parent.find(f"{{{ns}}}{tag}")
    return (el.text or "").strip() if el is not None and el.text else ""


def parse_user_list(root: ET.Element) -> tuple[list[User], str | None]:
    """Return ``(users, next_start_reference)`` from a ``GetUserList`` response."""
    users: list[User] = []
    for u in root.iter(f"{{{UDB}}}User"):
        attrs = {
            a.get("Name"): a.get("Value", "") for a in u.findall(f"{{{UDB}}}Attribute")
        }
        users.append(
            User(
                token=u.get("token", ""),
                name=_text(u, "Name", UDB),
                first_name=attrs.get("FirstName", ""),
                last_name=attrs.get("LastName", ""),
                description=_text(u, "Description", UDB),
            )
        )
    nxt = root.find(f".//{{{UDB}}}NextStartReference")
    return users, (nxt.text.strip() if nxt is not None and nxt.text else None)


def parse_credentials(root: ET.Element) -> tuple[list[Credential], str | None]:
    """Parse ``GetCredential(List)`` into credentials + a next-page reference."""
    creds: list[Credential] = []
    for c in root.iter(f"{{{PX}}}Credential"):
        id_data = {
            d.get("Name"): d.get("Value", "")
            for d in c.findall(f"{{{PX}}}IdData")
            if d.get("Name")
        }
        profiles = [
            ap.text.strip()
            for cap in c.findall(f"{{{PX}}}CredentialAccessProfile")
            if (ap := cap.find(f"{{{PX}}}AccessProfile")) is not None and ap.text
        ]
        creds.append(
            Credential(
                token=c.get("token", ""),
                user_token=_text(c, "UserToken", PX),
                description=_text(c, "Description", PX),
                enabled=_text(c, "Enabled", PX).lower() == "true",
                status=_text(c, "Status", PX),
                id_data=id_data,
                access_profile_tokens=profiles,
            )
        )
    nxt = root.find(f".//{{{PX}}}NextStartReference")
    return creds, (nxt.text.strip() if nxt is not None and nxt.text else None)


def parse_tcr_credentials(root: ET.Element) -> tuple[list[TcrCredential], str | None]:
    """Parse ``tcr:GetCredential(List)`` into TcrCredentials + a next-page ref.

    The ONVIF ``tcr`` view is the only one carrying ValidFrom/ValidTo; the
    identifiers are echoed verbatim so a ModifyCredential preserves PIN/card.
    """
    out: list[TcrCredential] = []
    for c in root.iter(f"{{{TCR}}}Credential"):
        ids: list[dict[str, str]] = []
        for ci in c.findall(f"{{{TCR}}}CredentialIdentifier"):
            typ = ci.find(f"{{{TCR}}}Type")
            ids.append(
                {
                    "type": _text(typ, "Name", TCR) if typ is not None else "",
                    "format": _text(typ, "FormatType", TCR) if typ is not None else "",
                    "exempted": _text(ci, "ExemptedFromAuthentication", TCR) or "false",
                    "value": _text(ci, "Value", TCR),
                }
            )
        profiles = [
            t.text.strip()
            for cap in c.findall(f"{{{TCR}}}CredentialAccessProfile")
            if (t := cap.find(f"{{{TCR}}}AccessProfileToken")) is not None and t.text
        ]
        out.append(
            TcrCredential(
                token=c.get("token", ""),
                description=_text(c, "Description", TCR),
                holder=_text(c, "CredentialHolderReference", TCR),
                valid_from=_text(c, "ValidFrom", TCR) or None,
                valid_to=_text(c, "ValidTo", TCR) or None,
                identifiers=ids,
                access_profile_tokens=profiles,
            )
        )
    nxt = root.find(f".//{{{TCR}}}NextStartReference")
    return out, (nxt.text.strip() if nxt is not None and nxt.text else None)


def parse_access_profiles(root: ET.Element) -> list[AccessProfile]:
    profiles: list[AccessProfile] = []
    for p in root.iter(f"{{{TAR}}}AccessProfile"):
        policies = [
            AccessPolicy(
                schedule_token=_text(ap, "ScheduleToken", TAR),
                entity_token=_text(ap, "Entity", TAR),
            )
            for ap in p.findall(f"{{{TAR}}}AccessPolicy")
        ]
        profiles.append(
            AccessProfile(
                token=p.get("token", ""),
                name=_text(p, "Name", TAR),
                description=_text(p, "Description", TAR),
                policies=policies,
            )
        )
    return profiles


def parse_schedule_info_list(root: ET.Element) -> list[Schedule]:
    return [
        Schedule(
            token=s.get("token", ""),
            name=_text(s, "Name", TSC),
            description=_text(s, "Description", TSC),
        )
        for s in root.iter(f"{{{TSC}}}ScheduleInfo")
    ]


def parse_access_point_info_list(root: ET.Element) -> list[AccessPoint]:
    return [
        AccessPoint(
            token=ap.get("token", ""),
            name=_text(ap, "Name", TAC),
            description=_text(ap, "Description", TAC),
            entity_type=_text(ap, "EntityType", TAC),
            entity_token=_text(ap, "Entity", TAC),
        )
        for ap in root.iter(f"{{{TAC}}}AccessPointInfo")
    ]


def parse_token(root: ET.Element, ns: str) -> str | None:
    """Pull a ``<Token>`` from a Set* response (the created/modified token)."""
    el = root.find(f".//{{{ns}}}Token")
    return el.text.strip() if el is not None and el.text else None

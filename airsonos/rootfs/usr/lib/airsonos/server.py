#!/usr/bin/env python3
"""Small Ingress web UI to toggle the visibility of AirSonos devices.

It reads the AirConnect configuration file (``/config/airsonos.xml``) to
know every device AirConnect has discovered, and it queries the Sonos
system live (via SSDP discovery + the ZoneGroupTopology UPnP service) to
find out which players are currently grouped.

The page then lists:

* **Sonos groups** - each current multi-player group, shown as a single
  row. AirConnect only ever exposes a group through its *coordinator*
  player, so enabling/disabling a group maps to that coordinator's entry.
* **Speakers** - every standalone player (and any device that could not
  be matched to the live Sonos topology).

Disabled entries are no longer exposed as AirPlay receivers.

The server intentionally only uses the Python standard library so no
extra dependencies need to be installed. Talking to Sonos is best-effort:
if discovery fails, the page falls back to a flat list of every device in
the configuration file.
"""

from __future__ import annotations

import html
import os
import socket
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

CONFIG_FILE = os.environ.get("AIRSONOS_CONFIG", "/config/airsonos.xml")
LISTEN_PORT = int(os.environ.get("AIRSONOS_WEB_PORT", "8099"))
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")

# Optional comma-separated list of Sonos IPs to skip SSDP discovery.
SONOS_HOSTS = [h.strip() for h in os.environ.get("SONOS_HOST", "").split(",") if h.strip()]

SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900
SSDP_ST = "urn:schemas-upnp-org:device:ZonePlayer:1"
SONOS_PORT = 1400


# ---------------------------------------------------------------------------
# Configuration file (the list of every device AirConnect knows about)
# ---------------------------------------------------------------------------

def read_devices():
    """Return a list of device dicts parsed from the config file.

    Each dict has ``udn``, ``name``, ``mac`` and ``enabled`` keys.
    """
    if not os.path.exists(CONFIG_FILE):
        return []

    try:
        tree = ET.parse(CONFIG_FILE)
    except ET.ParseError:
        return []

    devices = []
    for device in tree.getroot().findall("device"):
        udn = (device.findtext("udn") or "").strip()
        if not udn:
            continue
        name = (device.findtext("name") or "").strip() or udn
        mac = (device.findtext("mac") or "").strip()
        enabled_text = (device.findtext("enabled") or "1").strip()
        enabled = enabled_text not in ("0", "false", "no", "")
        devices.append({"udn": udn, "name": name, "mac": mac, "enabled": enabled})

    return devices


def write_devices(enabled_udns, known_udns):
    """Persist the enabled state for the devices shown on the page.

    ``enabled_udns`` is the set of device UDNs whose checkbox was ticked.
    ``known_udns`` is the set of every UDN that was rendered on the page;
    only those are updated, so devices that were not shown (e.g. grouped
    slaves that AirConnect currently hides) keep their existing state.
    """
    tree = ET.parse(CONFIG_FILE)
    root = tree.getroot()

    for device in root.findall("device"):
        udn = (device.findtext("udn") or "").strip()
        if not udn or udn not in known_udns:
            continue

        enabled_el = device.find("enabled")
        if enabled_el is None:
            enabled_el = ET.SubElement(device, "enabled")
        enabled_el.text = "1" if udn in enabled_udns else "0"

    tree.write(CONFIG_FILE, encoding="utf-8", xml_declaration=True)


# ---------------------------------------------------------------------------
# Live Sonos topology (SSDP discovery + ZoneGroupTopology SOAP)
# ---------------------------------------------------------------------------

def discover_sonos_hosts(timeout=2.0):
    """Return a list of Sonos player IPs found via SSDP.

    A single reachable player is enough to fetch the whole topology, but we
    collect every responder for resilience.
    """
    if SONOS_HOSTS:
        return list(SONOS_HOSTS)

    message = "\r\n".join(
        [
            "M-SEARCH * HTTP/1.1",
            "HOST: {}:{}".format(SSDP_ADDR, SSDP_PORT),
            'MAN: "ssdp:discover"',
            "MX: 1",
            "ST: {}".format(SSDP_ST),
            "",
            "",
        ]
    ).encode("utf-8")

    hosts = []
    seen = set()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.settimeout(timeout)
        sock.sendto(message, (SSDP_ADDR, SSDP_PORT))

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data, addr = sock.recvfrom(2048)
            except socket.timeout:
                break
            text = data.decode("utf-8", "ignore")
            host = addr[0]
            for line in text.splitlines():
                if line.lower().startswith("location:"):
                    parsed = urlparse(line.split(":", 1)[1].strip())
                    if parsed.hostname:
                        host = parsed.hostname
                    break
            if host not in seen:
                seen.add(host)
                hosts.append(host)
    except OSError:
        pass
    finally:
        sock.close()

    return hosts


def _fetch_zone_group_state(host, timeout=3.0):
    """Call GetZoneGroupState on a Sonos player and return the inner XML."""
    body = (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        "<s:Body>"
        '<u:GetZoneGroupState '
        'xmlns:u="urn:schemas-upnp-org:service:ZoneGroupTopology:1">'
        "</u:GetZoneGroupState>"
        "</s:Body></s:Envelope>"
    ).encode("utf-8")

    request = urllib.request.Request(
        "http://{}:{}/ZoneGroupTopology/Control".format(host, SONOS_PORT),
        data=body,
        method="POST",
        headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPACTION": '"urn:schemas-upnp-org:service:ZoneGroupTopology:1'
            '#GetZoneGroupState"',
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8", "ignore")

    envelope = ET.fromstring(raw)
    for element in envelope.iter():
        if element.tag.endswith("ZoneGroupState") and element.text:
            return element.text
    return None


def _parse_zone_groups(state_xml):
    """Parse the inner ZoneGroupState XML into a list of group dicts.

    Each group dict has ``coordinator`` (UUID) and ``members`` (a list of
    ``{"uuid", "name"}``). Invisible members (hidden satellites, bridges,
    paired sub/surrounds) are skipped.
    """
    root = ET.fromstring(state_xml)
    groups = []
    for group in root.iter("ZoneGroup"):
        coordinator = group.get("Coordinator", "")
        members = []
        for member in group.findall("ZoneGroupMember"):
            if member.get("Invisible") == "1":
                continue
            if member.get("IsZoneBridge") == "1":
                continue
            uuid = member.get("UUID", "")
            name = member.get("ZoneName", "") or uuid
            if uuid:
                members.append({"uuid": uuid, "name": name})
        if members:
            groups.append({"coordinator": coordinator, "members": members})
    return groups


def fetch_sonos_groups():
    """Best-effort: return the current Sonos groups, or ``None`` on failure."""
    for host in discover_sonos_hosts():
        try:
            state = _fetch_zone_group_state(host)
        except (urllib.error.URLError, ET.ParseError, OSError, socket.timeout):
            continue
        if not state:
            continue
        try:
            return _parse_zone_groups(state)
        except ET.ParseError:
            continue
    return None


# ---------------------------------------------------------------------------
# Combine config devices with the live topology
# ---------------------------------------------------------------------------

def _normalise_uuid(value):
    return value.replace("uuid:", "").strip().upper()


def _match_device(uuid, devices_by_key):
    """Find the config device that corresponds to a Sonos UUID."""
    key = _normalise_uuid(uuid)
    device = devices_by_key.get(key)
    if device:
        return device
    # Fall back to matching on the MAC embedded in the Sonos UUID.
    for dev in devices_by_key.values():
        mac = dev["mac"].replace(":", "").replace("-", "").upper()
        if mac and mac in key:
            return dev
    return None


def build_view():
    """Return ``(groups, speakers, matched_udns)`` for rendering.

    ``groups`` is a list of ``{"label", "udn", "enabled", "members"}`` for
    every current multi-player Sonos group. ``speakers`` is a list of
    ``{"label", "udn", "enabled"}`` for standalone players and any config
    device that could not be matched to the live topology. When Sonos can
    not be reached both fall back to the flat device list.
    """
    devices = read_devices()
    devices_by_key = {_normalise_uuid(dev["udn"]): dev for dev in devices}

    topology = fetch_sonos_groups()

    if topology is None:
        # No live topology - fall back to a flat list of everything.
        speakers = [
            {"label": dev["name"], "udn": dev["udn"], "enabled": dev["enabled"]}
            for dev in devices
        ]
        speakers.sort(key=lambda item: item["label"].lower())
        return [], speakers, {dev["udn"] for dev in devices}, True

    groups = []
    speakers = []
    matched = set()

    for group in topology:
        coordinator_dev = _match_device(group["coordinator"], devices_by_key)
        member_names = [m["name"] for m in group["members"]]

        if len(group["members"]) > 1:
            if not coordinator_dev:
                continue
            matched.add(coordinator_dev["udn"])
            label = " + ".join(member_names)
            groups.append(
                {
                    "label": label,
                    "udn": coordinator_dev["udn"],
                    "enabled": coordinator_dev["enabled"],
                    "members": member_names,
                }
            )
        else:
            member = group["members"][0]
            dev = _match_device(member["uuid"], devices_by_key)
            if not dev:
                continue
            matched.add(dev["udn"])
            speakers.append(
                {"label": member["name"], "udn": dev["udn"], "enabled": dev["enabled"]}
            )

    # Any config device we could not place (offline, non-Sonos UPnP, ...).
    for dev in devices:
        if dev["udn"] not in matched:
            speakers.append(
                {"label": dev["name"], "udn": dev["udn"], "enabled": dev["enabled"]}
            )
            matched.add(dev["udn"])

    groups.sort(key=lambda item: item["label"].lower())
    speakers.sort(key=lambda item: item["label"].lower())
    return groups, speakers, {dev["udn"] for dev in devices}, False


def restart_addon():
    """Restart this add-on through the Supervisor so changes take effect."""
    if not SUPERVISOR_TOKEN:
        return

    # Give the browser time to receive the response before we go down.
    time.sleep(1)

    request = urllib.request.Request(
        "http://supervisor/addons/self/restart",
        method="POST",
        headers={"Authorization": "Bearer " + SUPERVISOR_TOKEN},
    )
    try:
        urllib.request.urlopen(request, timeout=30)
    except urllib.error.URLError:
        pass


PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AirSonos devices</title>
<style>
  body {{ font-family: sans-serif; margin: 0; padding: 16px;
         background: #fafafa; color: #212121; }}
  h1 {{ font-size: 1.4rem; }}
  h2 {{ font-size: 1.05rem; margin: 24px 0 8px; color: #424242; }}
  p.hint {{ color: #616161; max-width: 640px; }}
  ul {{ list-style: none; padding: 0; max-width: 640px; }}
  li {{ background: #fff; border: 1px solid #e0e0e0; border-radius: 6px;
        padding: 12px 16px; margin-bottom: 8px; display: flex;
        align-items: center; }}
  li label {{ display: flex; align-items: center; width: 100%;
              cursor: pointer; }}
  li input {{ width: 20px; height: 20px; margin-right: 12px; flex: none; }}
  .name {{ font-weight: 600; }}
  .sub {{ display: block; color: #9e9e9e; font-size: 0.78rem; }}
  .warn {{ color: #e65100; }}
  button {{ background: #03a9f4; color: #fff; border: none; border-radius: 6px;
            padding: 12px 20px; font-size: 1rem; cursor: pointer;
            margin-top: 8px; }}
  button:hover {{ background: #0288d1; }}
  .empty {{ color: #616161; max-width: 640px; }}
</style>
</head>
<body>
<h1>AirSonos devices</h1>
{body}
</body>
</html>
"""


def _render_group_item(item):
    checked = " checked" if item["enabled"] else ""
    members = ", ".join(item["members"])
    warn = ""
    if not item["enabled"]:
        warn = ' <span class="warn">(disabled - this group is hidden)</span>'
    return (
        "<li><label>"
        '<input type="checkbox" name="device" value="{udn}"{checked}>'
        '<span><span class="name">{label}</span>{warn}'
        '<span class="sub">Group of: {members}</span></span>'
        "</label></li>".format(
            udn=html.escape(item["udn"]),
            label=html.escape(item["label"]),
            members=html.escape(members),
            checked=checked,
            warn=warn,
        )
    )


def _render_speaker_item(item):
    checked = " checked" if item["enabled"] else ""
    return (
        "<li><label>"
        '<input type="checkbox" name="device" value="{udn}"{checked}>'
        '<span><span class="name">{label}</span>'
        '<span class="sub">{udn}</span></span>'
        "</label></li>".format(
            udn=html.escape(item["udn"]),
            label=html.escape(item["label"]),
            checked=checked,
        )
    )


def render_index(saved=False):
    groups, speakers, known_udns, degraded = build_view()

    notice = ""
    if saved:
        notice = (
            '<p class="hint">Changes saved. The add-on is restarting to apply '
            "them; this page will be unavailable for a moment.</p>"
        )

    if not groups and not speakers:
        body = notice + (
            '<p class="empty">No devices have been detected yet. Make sure the '
            "add-on has been running for a bit so it can discover your Sonos / "
            "UPnP players, then refresh this page.</p>"
        )
        return PAGE.format(body=body)

    known_input = "".join(
        '<input type="hidden" name="known" value="{}">'.format(html.escape(udn))
        for udn in sorted(known_udns)
    )

    sections = [notice]

    if degraded:
        sections.append(
            '<p class="hint">Could not reach your Sonos system to read live '
            "groups, so every known device is listed individually. Uncheck a "
            "device to hide it from AirPlay.</p>"
        )
    else:
        sections.append(
            '<p class="hint">Uncheck an entry to hide it from AirPlay, then '
            "click Save. A <strong>group</strong> is exposed to AirPlay through "
            "its coordinator speaker, so hiding a group hides that one AirPlay "
            "entry. Standalone speakers that already appear natively in AirPlay "
            "can be safely hidden here to avoid duplicates.</p>"
        )

    sections.append('<form method="post" action="">')
    sections.append(known_input)

    if groups:
        sections.append("<h2>Sonos groups</h2>")
        sections.append(
            "<ul>" + "".join(_render_group_item(g) for g in groups) + "</ul>"
        )

    if speakers:
        sections.append("<h2>Speakers</h2>")
        sections.append(
            "<ul>" + "".join(_render_speaker_item(s) for s in speakers) + "</ul>"
        )

    sections.append('<button type="submit">Save</button>')
    sections.append("</form>")
    sections.append(
        '<p class="hint" style="margin-top:24px">'
        '<a href="?debug=1">Open diagnostics</a> if a group is missing.</p>'
    )

    return PAGE.format(body="".join(sections))


def _raw_zone_groups(state_xml):
    """Parse ZoneGroupState keeping every member and its flags (for debug)."""
    root = ET.fromstring(state_xml)
    groups = []
    for group in root.iter("ZoneGroup"):
        members = []
        for member in group.findall("ZoneGroupMember"):
            members.append(
                {
                    "uuid": member.get("UUID", ""),
                    "name": member.get("ZoneName", ""),
                    "invisible": member.get("Invisible", "0"),
                    "bridge": member.get("IsZoneBridge", "0"),
                }
            )
        groups.append({"coordinator": group.get("Coordinator", ""), "members": members})
    return groups


def render_debug():
    """Render a diagnostics page showing raw discovery + topology + matching."""
    devices = read_devices()
    devices_by_key = {_normalise_uuid(dev["udn"]): dev for dev in devices}

    hosts = discover_sonos_hosts()

    parts = ["<h1>AirSonos diagnostics</h1>", '<p><a href=".">&larr; Back</a></p>']

    parts.append("<h2>Sonos hosts found via SSDP</h2>")
    if hosts:
        parts.append("<ul>" + "".join("<li>%s</li>" % html.escape(h) for h in hosts) + "</ul>")
    else:
        parts.append(
            '<p class="warn">None. Multicast/SSDP is likely blocked. Set the '
            "<code>SONOS_HOST</code> option to a Sonos IP to bypass discovery.</p>"
        )

    state = None
    used_host = None
    for host in hosts:
        try:
            state = _fetch_zone_group_state(host)
        except Exception as exc:  # noqa: BLE001 - surfaced for debugging
            parts.append(
                '<p class="warn">Query to %s failed: %s</p>'
                % (html.escape(host), html.escape(str(exc)))
            )
            continue
        if state:
            used_host = host
            break

    parts.append("<h2>Zone groups reported by Sonos</h2>")
    if not state:
        parts.append('<p class="warn">No topology returned.</p>')
    else:
        parts.append("<p>Queried host: %s</p>" % html.escape(str(used_host)))
        try:
            raw_groups = _raw_zone_groups(state)
        except ET.ParseError as exc:
            raw_groups = []
            parts.append('<p class="warn">Could not parse topology: %s</p>' % html.escape(str(exc)))
        for group in raw_groups:
            coord = _match_device(group["coordinator"], devices_by_key)
            coord_txt = coord["name"] if coord else "NO MATCH in config"
            parts.append(
                "<p><strong>Group</strong> coordinator=%s &rarr; %s</p>"
                % (html.escape(group["coordinator"]), html.escape(coord_txt))
            )
            rows = []
            for m in group["members"]:
                dev = _match_device(m["uuid"], devices_by_key)
                match_txt = dev["name"] if dev else "NO MATCH"
                flags = []
                if m["invisible"] == "1":
                    flags.append("invisible")
                if m["bridge"] == "1":
                    flags.append("bridge")
                rows.append(
                    "<li>%s (%s) %s &rarr; config: %s</li>"
                    % (
                        html.escape(m["name"] or "?"),
                        html.escape(m["uuid"]),
                        html.escape("[" + ",".join(flags) + "]") if flags else "",
                        html.escape(match_txt),
                    )
                )
            parts.append("<ul>" + "".join(rows) + "</ul>")

    parts.append("<h2>Devices in the config file</h2>")
    if devices:
        rows = [
            "<li>%s &mdash; udn=%s &mdash; mac=%s &mdash; %s</li>"
            % (
                html.escape(d["name"]),
                html.escape(d["udn"]),
                html.escape(d["mac"] or "(none)"),
                "enabled" if d["enabled"] else "disabled",
            )
            for d in devices
        ]
        parts.append("<ul>" + "".join(rows) + "</ul>")
    else:
        parts.append('<p class="warn">The config file has no &lt;device&gt; entries yet.</p>')

    return PAGE.format(body="".join(parts))


class Handler(BaseHTTPRequestHandler):
    def _send_html(self, content, status=200):
        payload = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if urlparse(self.path).query and "debug" in parse_qs(urlparse(self.path).query):
            self._send_html(render_debug())
            return
        self._send_html(render_index())

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        params = parse_qs(raw)
        enabled_udns = set(params.get("device", []))
        known_udns = set(params.get("known", []))

        try:
            write_devices(enabled_udns, known_udns)
        except (ET.ParseError, OSError):
            self._send_html(
                PAGE.format(
                    body='<p class="empty">Could not update the configuration '
                    "file.</p>"
                ),
                status=500,
            )
            return

        self._send_html(render_index(saved=True))
        threading.Thread(target=restart_addon, daemon=True).start()

    def log_message(self, *args):  # noqa: D401 - silence default logging
        return


def main():
    server = ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()

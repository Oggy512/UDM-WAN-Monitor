"""
UDM WAN Monitor — Collector  v4
Polls /proxy/network/api/s/{site}/stat/device and extracts
wan1, wan2, last_wan_interfaces, last_wan_status from the UDM device entry.

State tracked per interface:
  - up         (port link state from wan1/wan2.up)
  - alive      (from last_wan_interfaces.WAN/WAN2.alive)
  - online     (from last_wan_status.WAN/WAN2 == "online")

Events fired on any state change (critical for degradation, info for recovery).
"""

import logging
import threading

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from app.collectors.base import Collector, CollectorResult
from app.tz import utc_now

logger = logging.getLogger("docsight.udm_wan_monitor")

# ── In-process state ────────────────────────────────────────────────────────
# Each entry: {"up": None, "alive": None, "online": None}
_state_lock = threading.Lock()
_state = {
    "wan1": {"up": None, "alive": None, "online": None},
    "wan2": {"up": None, "alive": None, "online": None},
}


class UdmWanCollector(Collector):
    name = "udm_wan_monitor"

    def __init__(self, config_mgr, storage, web, **kwargs):
        interval = int(config_mgr.get("udm_wan_interval", 60) or 60)
        super().__init__(poll_interval_seconds=interval)
        self._cfg     = config_mgr
        self._storage = storage
        self._web     = web
        self._session: requests.Session | None = None
        self._session_lock = threading.Lock()
        self._last_result: dict | None = None

    def is_enabled(self) -> bool:
        return bool(self._cfg.get("udm_wan_enabled", False))

    def collect(self) -> CollectorResult:
        cfg = _build_cfg_from(self._cfg)
        if not cfg["host"]:
            return CollectorResult.failure(self.name, "UDM host not configured")
        try:
            session = self._get_session(cfg)
            device  = _fetch_udm_device(session, cfg)
        except PermissionError as exc:
            self._invalidate_session()
            return CollectorResult.failure(self.name, str(exc))
        except requests.exceptions.ConnectionError as exc:
            self._invalidate_session()
            return CollectorResult.failure(self.name, f"Connection error: {exc}")
        except requests.exceptions.Timeout:
            self._invalidate_session()
            return CollectorResult.failure(self.name, "Timeout")
        except Exception as exc:  # noqa: BLE001
            self._invalidate_session()
            logger.exception("UDM WAN collect failed")
            return CollectorResult.failure(self.name, str(exc))

        parsed = parse_device(device)
        events = self._detect_changes(parsed)
        if events:
            self._write_events(events)

        ts = utc_now()
        self._last_result = {"parsed": parsed, "timestamp": ts}
        return CollectorResult.ok(self.name, {"parsed": parsed, "events": events, "timestamp": ts})

    # ── Session ──────────────────────────────────────────────────────────────
    def _get_session(self, cfg):
        with self._session_lock:
            if self._session is None:
                self._session = _login(cfg)
            return self._session

    def _invalidate_session(self):
        with self._session_lock:
            self._session = None

    # ── State change detection ────────────────────────────────────────────────
    def _detect_changes(self, parsed: dict) -> list[dict]:
        events = []
        now = utc_now()
        with _state_lock:
            for key in ("wan1", "wan2"):
                w = parsed.get(key, {})
                iface_label = "WAN 1" if key == "wan1" else "WAN 2"
                prev = _state[key]

                for field, cur_val in [
                    ("up",     w.get("up")),
                    ("alive",  w.get("alive")),
                    ("online", w.get("online")),
                ]:
                    old_val = prev[field]
                    if old_val is None:
                        # First poll — establish baseline silently
                        prev[field] = cur_val
                        logger.info("UDM WAN: baseline %s.%s = %s", key, field, cur_val)
                        continue
                    if cur_val == old_val:
                        continue

                    prev[field] = cur_val
                    # Determine severity: degradation = critical/warning, recovery = info
                    degraded = (cur_val is False) or (cur_val == "offline") or (cur_val is False)
                    if degraded:
                        sev = "critical" if key == "wan1" else "warning"
                    else:
                        sev = "info"

                    msg = _event_msg(iface_label, field, cur_val, w.get("ip"))
                    events.append({
                        "timestamp":  now,
                        "severity":   sev,
                        "event_type": "udm_wan",
                        "message":    msg,
                        "details": {
                            "interface": iface_label,
                            "field":     field,
                            "value":     str(cur_val),
                            "ip":        w.get("ip"),
                            "source":    "community.udm_wan_monitor",
                        },
                    })
                    logger.warning("UDM WAN EVENT: %s", msg)
        return events

    def _write_events(self, events: list[dict]):
        try:
            if self._storage and hasattr(self._storage, "save_events"):
                self._storage.save_events(events)
                logger.info("UDM WAN: wrote %d event(s) to global log", len(events))
            elif self._storage and hasattr(self._storage, "save_event"):
                for ev in events:
                    self._storage.save_event(
                        timestamp  = ev["timestamp"],
                        severity   = ev["severity"],
                        event_type = ev["event_type"],
                        message    = ev["message"],
                        details    = ev.get("details"),
                    )
        except Exception:  # noqa: BLE001
            logger.warning("UDM WAN: could not write events to global log", exc_info=True)


# ── Module-level helpers ──────────────────────────────────────────────────────

def _build_cfg_from(cfg) -> dict:
    host = (cfg.get("udm_wan_host") or "").strip().rstrip("/")
    port = int(cfg.get("udm_wan_port", 443) or 443)
    base = f"https://{host}:{port}" if host and not host.startswith("http") else f"{host}:{port}"
    return {
        "host":       host,
        "base":       base,
        "username":   cfg.get("udm_wan_username", ""),
        "password":   cfg.get("udm_wan_password", ""),
        "site":       (cfg.get("udm_wan_site") or "default").strip(),
        "verify_ssl": bool(cfg.get("udm_wan_verify_ssl", False)),
    }


def _login(cfg: dict) -> requests.Session:
    session = requests.Session()
    session.verify = cfg["verify_ssl"]
    payload = {"username": cfg["username"], "password": cfg["password"], "remember": True}
    headers = {"Content-Type": "application/json"}
    r = session.post(f"{cfg['base']}/api/auth/login", json=payload, headers=headers, timeout=15)
    if r.status_code != 200:
        r = session.post(f"{cfg['base']}/api/login", json=payload, headers=headers, timeout=15)
    r.raise_for_status()
    token = r.headers.get("X-Updated-Csrf-Token") or r.headers.get("csrf-token")
    if token:
        session.headers["X-Csrf-Token"] = token
    logger.info("UDM WAN: login OK")
    return session


def _fetch_udm_device(session: requests.Session, cfg: dict) -> dict:
    """Fetch /stat/device and return the UDM/gateway device entry."""
    base = cfg["base"]
    site = cfg["site"]
    url  = f"{base}/proxy/network/api/s/{site}/stat/device"
    r = session.get(url, timeout=15)
    if r.status_code == 401:
        raise PermissionError("Session expired (401)")
    r.raise_for_status()
    devices = r.json().get("data", [])
    # Find the gateway device (udm / ugw / usg type)
    udm = next((d for d in devices if d.get("type") in ("udm", "ugw", "usg")),
               devices[0] if devices else {})
    return udm


def parse_device(d: dict) -> dict:
    """
    Extract all WAN info from a single UDM stat/device entry.

    Sources:
      wan1 / wan2           → ip, up, latency, speed, rx/tx, availability, type, ipv6, dns
      last_wan_interfaces   → alive (bool) per interface
      last_wan_status       → "online"/"offline" per interface
      uplink                → nameservers_dynamic (for WAN1 if wan1.dns is absent), uptime
    """
    wan1_raw = d.get("wan1", {})
    wan2_raw = d.get("wan2", {})
    lwi      = d.get("last_wan_interfaces", {})   # {"WAN": {"ip":..,"alive":..}, "WAN2":{..}}
    lws      = d.get("last_wan_status", {})        # {"WAN": "online", "WAN2": "offline"}
    uplink   = d.get("uplink", {})                 # active uplink extra info

    def _wan(raw, lwi_key, lws_key, uplink_data):
        dns_list = raw.get("dns") or []
        # For WAN1 dns may be absent — fall back to uplink.nameservers_dynamic
        if not dns_list and uplink_data:
            dns_list = uplink_data.get("nameservers_dynamic") or []
        # alive / online come from the dedicated fields
        lwi_entry = lwi.get(lwi_key, {})
        return {
            "ip":           raw.get("ip"),
            "netmask":      raw.get("netmask"),
            "ipv6":         (raw.get("ipv6") or [None])[0],   # first IPv6 addr
            "up":           raw.get("up"),
            "alive":        lwi_entry.get("alive"),            # from last_wan_interfaces
            "online":       lws.get(lws_key) == "online",     # from last_wan_status
            "latency":      raw.get("latency"),
            "availability": raw.get("availability"),           # % uptime
            "speed":        raw.get("speed"),
            "type":         raw.get("type"),
            "media":        raw.get("media"),
            "full_duplex":  raw.get("full_duplex"),
            "ifname":       raw.get("ifname") or raw.get("name"),
            "is_uplink":    raw.get("is_uplink", False),
            "rx_bytes":     raw.get("rx_bytes"),
            "tx_bytes":     raw.get("tx_bytes"),
            "rx_bytes_r":   raw.get("rx_bytes-r"),
            "tx_bytes_r":   raw.get("tx_bytes-r"),
            "rx_errors":    raw.get("rx_errors"),
            "tx_errors":    raw.get("tx_errors"),
            "rx_dropped":   raw.get("rx_dropped"),
            "tx_dropped":   raw.get("tx_dropped"),
            "dns":          ", ".join(str(x) for x in dns_list) if dns_list else None,
        }

    # uplink is only for the active WAN — pass it to WAN1 (typically eth9)
    wan1_is_uplink = wan1_raw.get("is_uplink", False)
    result = {
        "wan1": _wan(wan1_raw, "WAN",  "WAN",  uplink if wan1_is_uplink else None),
        "wan2": _wan(wan2_raw, "WAN2", "WAN2", uplink if not wan1_is_uplink else None),
        # Device-level info
        "device": {
            "model":       d.get("model"),
            "name":        d.get("name"),
            "version":     d.get("version"),
            "ip":          d.get("ip"),
            "mac":         d.get("mac"),
            "uptime":      uplink.get("uptime") or d.get("uptime"),
            "cpu_pct":     d.get("system-stats", {}).get("cpu"),
            "mem_pct":     d.get("system-stats", {}).get("mem"),
            "temperature": (d.get("temperatures") or [{}])[0].get("value"),
            "load_avg":    d.get("loadavg_5"),
            "lan_clients":  next((s.get("num_sta") for s in [d] if d.get("num_sta")), None)
                            or d.get("user-num_sta"),
            "wlan_clients": d.get("wlangroup_num_sta") or d.get("num_sta"),
            "active_wan":  uplink.get("comment"),   # "WAN" or "WAN2"
        },
        # Raw extra ports from config (populated in routes.py)
        "wan_ports": [],
    }
    return result


def _event_msg(iface: str, field: str, value, ip: str | None) -> str:
    ip_str = f" ({ip})" if ip else ""
    if field == "alive":
        state = "nicht erreichbar (alive=false)" if not value else "wieder erreichbar (alive=true)"
        return f"{iface}{ip_str}: {state}"
    if field == "online":
        state = "offline" if not value else "wieder online"
        return f"{iface}{ip_str}: {state}"
    if field == "up":
        state = "Link DOWN" if not value else "Link UP"
        return f"{iface}{ip_str}: {state}"
    return f"{iface}: {field} geändert zu {value}"

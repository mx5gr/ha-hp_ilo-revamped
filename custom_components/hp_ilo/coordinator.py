"""DataUpdateCoordinator for HP iLO integration."""
from __future__ import annotations

import re
import warnings
from dataclasses import dataclass
from datetime import timedelta
import logging
from typing import Any
from xml.etree import ElementTree

import hpilo

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .const import ILO_GEN_UNKNOWN, ILO_GEN_3, ILO_GEN_4, ILO_GEN_5, ILO_GEN_6

_LOGGER = logging.getLogger(__name__)

# Update interval - all entities will share this single refresh cycle
UPDATE_INTERVAL = timedelta(seconds=60)


# ---------------------------------------------------------------------------
# Debug logging patch
# ---------------------------------------------------------------------------

def _xml_to_str(xml_element) -> str:
    """Safely serialise an ElementTree element (or list of them) to a string."""
    if xml_element is None:
        return "(none)"
    try:
        if isinstance(xml_element, (list, tuple)):
            return "\n".join(
                ElementTree.tostring(e, encoding="unicode") for e in xml_element
            )
        if hasattr(xml_element, "tag"):
            return ElementTree.tostring(xml_element, encoding="unicode")
    except Exception:  # noqa: BLE001
        pass
    return str(xml_element)


def _patch_ilo_debug_logging(ilo_obj: hpilo.Ilo) -> None:
    """Monkey-patch hpilo.Ilo to log every RIBCL request/response pair.

    Wraps hpilo.Ilo._request — the single chokepoint for all RIBCL XML
    traffic.  Applied at the *instance* level so other Ilo objects are
    unaffected.  Only called when the logger is at DEBUG level, so there
    is zero overhead in normal operation.
    """
    original_request = ilo_obj._request

    def _logged_request(xml, progress=None):
        try:
            if hasattr(ElementTree, "tostringlist"):
                raw = b"\r\n".join(ElementTree.tostringlist(xml)) + b"\r\n"
            else:
                raw = ElementTree.tostring(xml)
            _LOGGER.debug(
                "[HP iLO REQUEST → %s:%s]\n%s",
                ilo_obj.hostname,
                ilo_obj.port,
                raw.decode("utf-8", errors="replace"),
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug(
                "[HP iLO REQUEST → %s:%s] (could not serialise: %s)",
                ilo_obj.hostname, ilo_obj.port, exc,
            )

        header, response = original_request(xml, progress=progress)

        _LOGGER.debug(
            "[HP iLO RESPONSE ← %s:%s]\nheader=%s\n%s",
            ilo_obj.hostname,
            ilo_obj.port,
            header,
            _xml_to_str(response),
        )
        return header, response

    ilo_obj._request = _logged_request


# ---------------------------------------------------------------------------
# iLO generation detection
# ---------------------------------------------------------------------------

def _detect_ilo_generation(fw_version: dict | None) -> int:
    """Return numeric iLO generation from get_fw_version() result.

    get_fw_version() returns a dict whose 'management_processor' field is:
      - "iLO3"  on Gen7 and earlier  (ProLiant BL/DL/ML G7 and earlier)
      - "iLO4"  on Gen8 / Gen9       (ProLiant DL/ML/SL Gen8, Gen9)
      - "iLO5"  on Gen10             (ProLiant DL/ML/SL/Apollo Gen10, Gen10 Plus)
      - "iLO6"  on Gen11             (ProLiant DL/ML/SL Gen11)

    Returns ILO_GEN_UNKNOWN (0) if the field is absent or unrecognised.
    """
    if not fw_version:
        return ILO_GEN_UNKNOWN
    mp = str(fw_version.get("management_processor", "")).lower()
    for gen in (6, 5, 4, 3):
        if f"ilo{gen}" in mp:
            return gen
    return ILO_GEN_UNKNOWN


# ---------------------------------------------------------------------------
# Cross-generation data normalisation helpers
# ---------------------------------------------------------------------------

def _normalise_nic_information(health: dict) -> None:
    """Ensure health['nic_information'] is always a label-keyed dict.

    python-hpilo parses NIC data as a *list* of dicts.  The
    get_embedded_health() post-processor then converts any list whose entries
    have a 'label' or 'location' key into a dict keyed by that field.

    iLO 4 NIC dicts include 'location' (e.g. "Embedded", "iLO Dedicated
    Network Port") so the post-processor produces a location-keyed dict.

    iLO 5 NIC dicts drop the 'location' key, so the post-processor leaves a
    raw list.  This helper converts that list to a 'port_description'-keyed
    dict (falling back through mac_address → index) so all downstream sensor
    code stays uniform.
    """
    nic_raw = health.get("nic_information")
    if nic_raw is None:
        return

    if isinstance(nic_raw, list):
        nic_dict: dict[str, dict] = {}
        for idx, nic in enumerate(nic_raw):
            if not isinstance(nic, dict):
                continue
            key = (
                nic.get("port_description")
                or nic.get("location")
                or nic.get("network_port")
                or nic.get("mac_address")
                or f"NIC_{idx}"
            )
            nic_dict[str(key)] = nic
        health["nic_information"] = nic_dict
    # If already a dict (iLO 4 post-processed path) — nothing to do.


def _normalise_storage(health: dict) -> None:
    """Ensure health['storage'] is always a label-keyed dict.

    python-hpilo's storage parser returns:
        {'storage': [<ctrl_dict>, ...], 'storage_discovery_status': '...'}

    The get_embedded_health() post-processor converts the list to a dict when
    entries carry a 'label' key (iLO 4 behaviour).  On iLO 5 the controller
    dict uses 'model' rather than 'label', so the post-processor leaves a
    raw list.  This helper normalises that to a model/index-keyed dict.
    """
    storage_raw = health.get("storage")
    if storage_raw is None:
        return

    if isinstance(storage_raw, list):
        ctrl_dict: dict[str, dict] = {}
        for idx, ctrl in enumerate(storage_raw):
            if not isinstance(ctrl, dict):
                continue
            key = (
                ctrl.get("label")
                or ctrl.get("model")
                or ctrl.get("controller_type")
                or f"Controller_{idx}"
            )
            ctrl_dict[str(key)] = ctrl
        health["storage"] = ctrl_dict
    # Already a dict — nothing to do.


def _normalise_memory_from_host_data(host_data: list | None, health: dict) -> None:
    """Build memory_components from SMBIOS type-17 records in host_data.

    This is the universal fallback used when neither memory_components nor a
    recognised memory_details format is present (e.g. iLO 4 Gen8/Gen9 servers
    that return MEMORY_DETAILS with 'size'/'frequency' keys instead of the
    expected 'memory_size'/'memory_speed' keys).

    SMBIOS type-17 (Memory Device) records are returned by every iLO generation
    via get_host_data() and always contain:
      'Label'  – slot name, e.g. 'PROC  1 DIMM  2 '  (normalise whitespace)
      'Size'   – e.g. 'not installed' or '8192 MB'
      'Speed'  – e.g. '1600 MHz' (absent when not installed)

    This source is reliable across iLO 3, 4, 5, and 6 and is preferred over
    parsing the raw MEMORY_DETAILS XML field names which differ between
    generations.
    """
    if not isinstance(host_data, list):
        return
    memory = health.get("memory")
    if not isinstance(memory, dict):
        memory = {}
        health["memory"] = memory

    components: list[list[tuple]] = []
    for record in host_data:
        if not isinstance(record, dict) or record.get("type") != 17:
            continue
        label = record.get("Label", "").strip()
        # Collapse multiple spaces inside the label (e.g. "PROC  1 DIMM  2 " → "PROC 1 DIMM 2")
        label = re.sub(r"  +", " ", label).strip()
        size  = (record.get("Size") or "").strip()
        speed = (record.get("Speed") or "").strip()

        if not label:
            continue
        # Skip empty/uninstalled slots — Size is "not installed" or absent
        size_lower = size.lower()
        if not size or size_lower in ("not installed", "n/a", ""):
            continue

        component: list[tuple] = [
            ("memory_location", {"value": label}),
            ("memory_size",     {"value": size}),
        ]
        if speed and speed not in ("N/A", "0 MHz", ""):
            component.append(("memory_speed", {"value": speed}))

        components.append(component)

    if components:
        memory["memory_components"] = components


def _normalise_memory(health: dict, host_data: list | None = None) -> None:
    """Normalise health['memory'] so memory_components is always present.

    Three memory data shapes exist across iLO generations:

    iLO 3 (G7 and earlier) — native memory_components
    ---------------------------------------------------
    health['memory']['memory_components'] is already a list of component-lists
    of (field_name, {'value': ...}) tuples.  No action needed.

    iLO 5/6 Gen10+ — MEMORY_DETAILS with memory_* field names
    -----------------------------------------------------------
    health['memory']['memory_details'] is a dict keyed by DIMM slot XML tag
    name, then by socket index.  Slot dicts use prefixed field names:
        {'memory_location': 'PROC 1 DIMM 1A', 'memory_size': '8192 MB',
         'memory_speed': '2400 MHz', 'memory_type': 'RDIMM', ...}

    iLO 4 Gen8/Gen9 — MEMORY_DETAILS with raw XML field names
    ----------------------------------------------------------
    health['memory']['memory_details'] uses raw XML tag names (size/frequency
    instead of memory_size/memory_speed) and has no location string per slot.
    For this generation the SMBIOS fallback (Path 3) gives better results.

    Universal fallback (Path 3) — SMBIOS type-17 from host_data
    ------------------------------------------------------------
    get_host_data() SMBIOS type-17 records always contain Label + Size + Speed
    with proper HP slot names (e.g. 'PROC 1 DIMM 2').  This is the most
    reliable source across all generations and is used whenever memory_components
    cannot be built from health data alone.
    """
    memory = health.get("memory")
    if not isinstance(memory, dict):
        memory = {}
        health["memory"] = memory

    # --- Path 1: already in the native iLO 3 / iLO 4 old-style memory_components shape ---
    if "memory_components" in memory:
        return

    # --- Path 2: iLO 5/6 memory_details with memory_* prefixed keys ---
    memory_details = memory.get("memory_details")
    if isinstance(memory_details, dict):
        # Peek at first populated slot to confirm iLO 5 key schema
        first_slot: dict | None = None
        for _tag, sockets in memory_details.items():
            if isinstance(sockets, dict):
                for _sk, slot in sockets.items():
                    if isinstance(slot, dict) and "memory_size" in slot:
                        first_slot = slot
                        break
            if first_slot:
                break

        if first_slot is not None:
            components: list[list[tuple]] = []
            for _slot_tag, sockets in memory_details.items():
                if not isinstance(sockets, dict):
                    continue
                for _sock_key, slot_data in sockets.items():
                    if not isinstance(slot_data, dict):
                        continue
                    size = slot_data.get("memory_size", "Not Installed")
                    if not size or size in ("Not Installed", ""):
                        continue
                    component: list[tuple] = [
                        (field_name, {"value": slot_data[field_name]})
                        for field_name in (
                            "memory_location",
                            "memory_size",
                            "memory_speed",
                            "memory_type",
                            "memory_rank",
                        )
                        if field_name in slot_data
                    ]
                    if component:
                        components.append(component)
            if components:
                memory["memory_components"] = components
                return

    # --- Path 3: Universal fallback — SMBIOS type-17 records from host_data ---
    # This covers iLO 4 Gen8/Gen9 (MEMORY_DETAILS with raw field names) and any
    # other generation where health memory data is absent or uses an unrecognised
    # schema.  SMBIOS type-17 always provides Label + Size + Speed with proper
    # HP slot names (e.g. 'PROC 1 DIMM 2').
    if host_data:
        _normalise_memory_from_host_data(host_data, health)


def _normalise_network_settings(ns: dict | None) -> dict | None:
    """Ensure both ip_address and ipv4_address keys are always present.

    iLO 4 returns 'ip_address'; iLO 5 returns 'ipv4_address'.
    Mirroring the value to the missing key means sensor code that reads either
    key works on both generations without branching.
    """
    if not isinstance(ns, dict):
        return ns
    if "ip_address" not in ns and "ipv4_address" in ns:
        ns["ip_address"] = ns["ipv4_address"]
    if "ipv4_address" not in ns and "ip_address" in ns:
        ns["ipv4_address"] = ns["ip_address"]
    return ns


def _normalise_health(health: dict | None, ilo_gen: int, host_data: list | None = None) -> dict | None:
    """Apply all normalisation passes to the raw health dict in-place."""
    if not isinstance(health, dict):
        return health
    _normalise_nic_information(health)
    _normalise_storage(health)
    _normalise_memory(health, host_data=host_data)
    return health


# ---------------------------------------------------------------------------
# Data container
# ---------------------------------------------------------------------------

@dataclass
class HpIloData:
    """Class to hold all HP iLO data fetched in a single update cycle."""

    # Detected iLO generation — ILO_GEN_3, ILO_GEN_4, ILO_GEN_5, ILO_GEN_6,
    # or ILO_GEN_UNKNOWN (0) if detection failed.
    ilo_gen: int = ILO_GEN_UNKNOWN

    # Server health data (temperatures, fans, processors, memory, NIC, storage)
    health: dict[str, Any] | None = None

    # Power status ("ON" or "OFF")
    power_status: str | None = None

    # Power on time in minutes
    power_on_time: int | None = None

    # Server name
    server_name: str | None = None

    # Host data (SMBIOS entries: model, BIOS version, serial, memory DIMMs, etc.)
    host_data: list[dict] | None = None

    # Network settings (iLO management NIC: IP, MAC, DNS, gateway, etc.)
    network_settings: dict | None = None

    # iLO firmware version, type and license
    fw_version: dict | None = None

    # Present, min, max and average power readings in Watts
    power_readings: dict | None = None

    # UID indicator light status ("ON" / "OFF")
    uid_status: str | None = None

    # Server asset tag string (or None if not set)
    asset_tag: str | None = None

    # Power regulator / saver mode dict  e.g. {'host_power_saver': 'AUTO'}
    # Not available via RIBCL on iLO 5+ / Gen10+ — will be None on those servers.
    # Available on iLO 3 and iLO 4.
    power_saver: dict | None = None

    # Power cap, alert thresholds and efficiency mode
    # Not available via RIBCL on iLO 5+ / Gen10+ — will be None on those servers.
    # Available on iLO 3 and iLO 4.
    pwreg: dict | None = None

    # Whether server stays powered off after a critical temperature shutdown
    # Not available via RIBCL on iLO 5+ / Gen10+ — will be None on those servers.
    # Available on iLO 3 and iLO 4.
    critical_temp_remain_off: bool | None = None

    # iLO event log entries (list of dicts, most-recent first)
    ilo_event_log: list[dict] | None = None

    # Integrated Management Log / server event log entries (most-recent first)
    server_event_log: list[dict] | None = None

    # Raw iLO connection for commands (buttons, switch actions)
    ilo: hpilo.Ilo | None = None


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------

class HpIloDataUpdateCoordinator(DataUpdateCoordinator[HpIloData]):
    """Coordinator to manage fetching HP iLO data from a single endpoint."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the coordinator."""
        self.config_entry = entry
        self.host = entry.data["host"]
        self.port = int(entry.data["port"])
        self.username = entry.data["username"]
        self.password = entry.data["password"]

        super().__init__(
            hass,
            _LOGGER,
            name=f"HP iLO ({self.host})",
            update_interval=UPDATE_INTERVAL,
        )

    async def _async_update_data(self) -> HpIloData:
        """Fetch data from HP iLO.

        Called by the coordinator at the configured interval.
        All entities receive the same data from this single fetch.
        """
        try:
            return await self.hass.async_add_executor_job(self._fetch_data)
        except hpilo.IloLoginFailed as err:
            raise UpdateFailed(f"Authentication failed: {err}") from err
        except hpilo.IloCommunicationError as err:
            raise UpdateFailed(f"Communication error: {err}") from err
        except hpilo.IloError as err:
            raise UpdateFailed(f"iLO error: {err}") from err

    def _create_ilo(self) -> hpilo.Ilo:
        """Instantiate an hpilo.Ilo, optionally attaching the debug logger.

        When the integration logger is at DEBUG level, every RIBCL request and
        response is written to the Home Assistant log.  There is zero overhead
        when debug logging is disabled.
        """
        ilo = hpilo.Ilo(
            hostname=self.host,
            login=self.username,
            password=self.password,
            port=self.port,
        )
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "Debug logging enabled for HP iLO at %s:%s — "
                "all RIBCL requests and responses will be logged.",
                self.host,
                self.port,
            )
            _patch_ilo_debug_logging(ilo)
        return ilo

    def _fetch_data(self) -> HpIloData:
        """Fetch all data from HP iLO (runs in executor thread).

        Execution order matters:
          1. get_fw_version() first — needed for generation detection, which
             controls which normalisation passes run later.
          2. All remaining data calls.
          3. Normalisation — applied after all raw data is collected.
        """
        _LOGGER.debug("Fetching data from HP iLO at %s:%s", self.host, self.port)

        ilo = self._create_ilo()
        data = HpIloData(ilo=ilo)

        def _try(label: str, fn, *args, **kwargs):
            """Call fn() suppressing all iLO errors.

            IloError / IloFeatureNotSupported indicate the feature is absent
            on this server/firmware combination — these are expected on older
            or newer iLO generations and are not logged as errors.
            """
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    return fn(*args, **kwargs)
            except (hpilo.IloError, hpilo.IloFeatureNotSupported) as err:
                _LOGGER.debug("Could not get %s: %s", label, err)
            return None

        # ── Step 1: firmware version + generation detection ────────────────
        data.fw_version = _try("firmware version", ilo.get_fw_version)
        data.ilo_gen = _detect_ilo_generation(data.fw_version)
        _LOGGER.debug(
            "Detected iLO generation: %s (management_processor=%s)",
            data.ilo_gen,
            data.fw_version.get("management_processor") if data.fw_version else "unknown",
        )

        # ── Step 2: fetch all data ─────────────────────────────────────────
        raw_health            = _try("embedded health",       ilo.get_embedded_health)
        data.power_status     = _try("power status",          ilo.get_host_power_status)
        data.power_on_time    = _try("power on time",         ilo.get_server_power_on_time)
        data.server_name      = _try("server name",           ilo.get_server_name)
        raw_network_settings  = _try("network settings",      ilo.get_network_settings)
        data.host_data        = _try("host data",             ilo.get_host_data)
        data.power_readings   = _try("power readings",        ilo.get_power_readings)
        data.uid_status       = _try("UID status",            ilo.get_uid_status)
        data.ilo_event_log    = _try("iLO event log",         ilo.get_ilo_event_log)
        data.server_event_log = _try("server event log",      ilo.get_server_event_log)

        # The following three calls work on iLO 3 and iLO 4 via RIBCL.
        # On iLO 5+ / Gen10+ they raise IloError and _try() returns None,
        # so the corresponding sensors simply do not appear in the UI.
        data.power_saver = _try("power saver status", ilo.get_host_power_saver_status)
        data.pwreg       = _try("power regulation",   ilo.get_pwreg)

        raw_ctro = _try("critical temp remain off", ilo.get_critical_temp_remain_off)
        if raw_ctro is not None:
            data.critical_temp_remain_off = (
                raw_ctro.get("critical_temp_remain_off", "No").upper() == "YES"
            )

        raw_tag = _try("asset tag", ilo.get_asset_tag)
        if raw_tag is not None:
            data.asset_tag = raw_tag.get("asset_tag")

        # ── Step 3: normalise for cross-generation compatibility ───────────
        data.health           = _normalise_health(raw_health, data.ilo_gen, host_data=data.host_data)
        data.network_settings = _normalise_network_settings(raw_network_settings)

        _LOGGER.debug("Successfully fetched data from HP iLO")
        return data

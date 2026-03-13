"""Constants for the HP iLO integration."""

DOMAIN = "hp_ilo"

# Config entry keys
CONF_HOST = "host"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_PORT = "port"
CONF_SCAN_INTERVAL = "scan_interval"

# Default values
DEFAULT_PORT = 443
DEFAULT_SCAN_INTERVAL = 60  # seconds

# iLO generation identifiers.
# Stored in coordinator data (HpIloData.ilo_gen) and optionally in config
# entry data ("ilo_gen") so the UI can show which generation is connected.
ILO_GEN_UNKNOWN = 0   # could not detect generation
ILO_GEN_3 = 3         # Gen7 and earlier (HP ProLiant G7 and earlier, iLO 3)
ILO_GEN_4 = 4         # Gen8 / Gen9  (HP ProLiant Gen8, Gen9)
ILO_GEN_5 = 5         # Gen10 / Gen10 Plus (HPE ProLiant Gen10, Gen10 Plus)
ILO_GEN_6 = 6         # Gen11 (HPE ProLiant Gen11)

# Sensor types supported by the coordinator-based sensor platform.
# These map directly to fields on HpIloData (coordinator.data).
SENSOR_TYPES = {
    # Simple scalar fields
    "server_name":           {"key": "server_name",         "unit": None},
    "server_fqdn":           {"key": "server_fqdn",         "unit": None},
    "server_power_status":   {"key": "server_power_status", "unit": None},
    "server_power_on_time":  {"key": "server_power_on_time","unit": "min"},
    "server_uid_status":     {"key": "server_uid_status",   "unit": None},
    # Dict / template-based fields (value_template required for useful output)
    "server_health":         {"key": "server_health",       "unit": None},
    "server_host_data":      {"key": "server_host_data",    "unit": None},
    "network_settings":      {"key": "network_settings",    "unit": None},
}

# Legacy sensor_type → coordinator data key mapping
LEGACY_SENSOR_TYPE_MAP = {
    "server_name":         "server_name",
    "server_fqdn":         "server_fqdn",
    "server_host_data":    "server_host_data",
    "server_power_status": "server_power_status",
    "server_power_on_time":"server_power_on_time",
    "server_uid_status":   "server_uid_status",
    "server_health":       "server_health",
    "network_settings":    "network_settings",
}

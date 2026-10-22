"""Constants for the E-REDES integration."""

from typing import Final

DOMAIN: Final = "eredes"

# Configuration keys
CONF_CPE: Final = "cpe"
CONF_ACCESS_TOKEN: Final = "access_token"
CONF_WEBHOOK_ID: Final = "webhook_id"
CONF_PRICE_ENTITY: Final = "price_entity"
CONF_PRICE_SOURCE: Final = "price_source"
CONF_TARIFF_CYCLE: Final = "tariff_cycle"
CONF_VAT_PERCENT: Final = "vat_percent"  # legacy (1.3.0), see CONF_VAT_MODE
CONF_VAT_MODE: Final = "vat_mode"
CONF_LARGE_FAMILY: Final = "large_family"

VAT_NONE: Final = "none"
VAT_FULL: Final = "full"
VAT_AUTO: Final = "auto"

PRICE_SOURCE_NONE: Final = "none"
PRICE_SOURCE_COOPERNICO: Final = "coopernico_tf"
PRICE_SOURCE_SENSOR: Final = "sensor"

# Legacy config keys migrated to CONF_ACCESS_TOKEN (see async_migrate_entry)
LEGACY_TOKEN_KEYS: Final = ("session_cookie", "aat_token")

# API endpoints
BASE_URL: Final = "https://balcaodigital.e-redes.pt"
API_URL: Final = f"{BASE_URL}/ms/reading/data-usage/edm/get"

# Timing
DEFAULT_SCAN_INTERVAL: Final = 3600  # 1 hour in seconds

# API request types
REQUEST_TYPE_15MIN: Final = "3"  # 15-minute interval readings

# Sensor keys
SENSOR_ENERGY: Final = "energy"
SENSOR_POWER: Final = "power"
SENSOR_TOKEN_EXPIRY: Final = "token_expiry"

# Treat the token as expired this long before its real `exp`, so a poll never
# starts with a token that dies mid-request.
TOKEN_EXPIRY_MARGIN_SECONDS: Final = 120

"""Constants for the SmartSchool (Webtop) integration."""

DOMAIN = "smartschool"
EVENT_NEW_HOMEWORK = f"{DOMAIN}_new_homework"
EVENT_NEW_MESSAGE = f"{DOMAIN}_new_message"

# Config-entry data keys (the values captured from the browser's loginByBio
# request; see the integration docs).
CONF_BIO_LOGIN = "bio_login"
CONF_UNIQUE_ID = "unique_id"
CONF_SELECTED_USER = "selected_user"
CONF_DEVICE_ID = "device_id"
CONF_IS_MOBILE = "is_mobile"

# Options (Settings -> Devices & services -> SmartSchool -> Configure).
CONF_SCAN_INTERVAL = "scan_interval"  # minutes
CONF_MESSAGES_ENABLED = "messages_enabled"
DEFAULT_SCAN_INTERVAL = 30
# Each poll is a handful of requests on a single-session account; keep it
# polite. The upper bound keeps "new homework" events reasonably timely.
MIN_SCAN_INTERVAL = 15
MAX_SCAN_INTERVAL = 360

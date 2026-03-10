"""Constants for LeChange Door Lock integration."""

DOMAIN = "lechange_door_lock"
CONF_APP_ID = "app_id"
CONF_APP_SECRET = "app_secret"
CONF_ACCESS_TOKEN = "access_token"
CONF_TOKEN_EXPIRE_TIME = "token_expire_time"  # 存储token过期时间戳
CONF_DEVICE_ID = "device_id"
CONF_DEVICE_NAME = "device_name"

DEFAULT_SCAN_INTERVAL = 30  # seconds

API_BASE = "https://openapi.lechange.cn:443/openapi"

# Platforms
PLATFORMS = ["sensor", "binary_sensor", "button"]

# Services
SERVICE_GENERATE_SNAPKEY = "generate_snapkey"
SERVICE_GET_SNAPKEY_LIST = "get_snapkey_list"
SERVICE_OPEN_DOOR_REMOTE = "open_door_remote"
SERVICE_WAKE_UP_DEVICE = "wake_up_device"
SERVICE_GET_OPEN_DOOR_RECORD = "get_open_door_record"
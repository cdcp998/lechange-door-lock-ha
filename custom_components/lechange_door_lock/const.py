"""Constants for the LeChange (Imou) Door Lock integration.

Based on the reverse-engineered client API (see API/report):
  - login:  POST /pcs/v1/user.account.GetToken  (x-pcs signing)
  - devices: device.list.BasicList / device.list.DeviceBasicInfoQueryV2
  - model:   iot.manager.QueryModelInfo (identifier <-> ref mapping)
  - state:   iot.control.GetProperties
  - control: iot.control.SetService / iot.control.SetProperties
"""

import os

DOMAIN = "lechange_door_lock"

# --- config entry fields -------------------------------------------------
CONF_USERNAME = "username"            # 乐橙 App 账号(手机号/邮箱)
CONF_PASSWORD = "password"            # 乐橙 App 密码
CONF_SESSION_ID = "session_id"        # GetToken 返回的 sessionId
CONF_TOKEN = "token"                  # GetToken 返回的 token(派生签名密钥)
CONF_INTERNAL_USERNAME = "internal_username"  # 云端内部账号名 (lc1n...)
CONF_USER_ID = "user_id"
CONF_KEY1 = "key1"                    # 登录后: md5(token) 小写
CONF_KEY2 = "key2"                    # 登录后: sha256(token) 小写
CONF_API_HOST = "api_host"            # 区域网关 (entryUrlV2)
CONF_DEVICE_ID = "device_id"
CONF_DEVICE_NAME = "device_name"
CONF_PRODUCT_ID = "product_id"
CONF_MODEL_NAME = "model_name"
CONF_FIRMWARE_VERSION = "firmware_version"
CONF_CHANNEL_JSON = "channels"        # JSON: channelList 快照
CONF_LOCK_STATE = "lock_state"        # 设备列表中的 lockState (beClosed/...)
CONF_STREAM_ENTRY = "stream_entry"    # 云端流媒体网关 (streamEntryAddrV3 / mediaConfig.streamUrl)
CONF_SNAPKEY_CONFIG = "snapkey_config"        # 临时密码配置(实体),存 entry.options
CONF_LAST_SNAPKEY_RESULT = "last_snapkey_result"  # 最近一次生成的临时密码(脱敏后)

# --- camera / video options ----------------------------------------------
CONF_RTSP_HOST = "rtsp_host"          # 门锁局域网 IP(开启视频 RTSP 用)
CONF_RTSP_PORT = "rtsp_port"          # 默认 554
CONF_RTSP_USERNAME = "rtsp_username"  # 默认 admin
CONF_RTSP_PASSWORD = "rtsp_password"
CONF_RTSP_URL = "rtsp_url"            # 完整 URL 覆盖(如 go2rtc 中转地址)
CONF_RTSP_SUBTYPE = "rtsp_subtype"    # 0 主码流 / 1 子码流

# --- cloud api -----------------------------------------------------------
API_ENTRY_HOST = "https://app-v2.imou.com"
API_PREFIX = "/pcs/v1/"
APIVER = "191204"
APP_ID = "lcbaseapp"
PROJECT = "Base"
PROTO_VER = "V9.7.6"
CONNECT_TIMEOUT = 15

# 乐橙私有 Dahua Root CA(登录入口网关 app-v2.imou.com 使用私有 CA)
CA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "certs", "dahua-root.pem")

DEFAULT_SCAN_INTERVAL = 30  # seconds

# Platforms
PLATFORMS = ["sensor", "binary_sensor", "lock", "button", "switch", "camera",
             "number", "select", "text", "time"]

# --- services -------------------------------------------------------------
SERVICE_GENERATE_SNAPKEY = "create_snapkey"
SERVICE_GET_SNAPKEY_LIST = "get_snapkey_list"
SERVICE_OPEN_DOOR_REMOTE = "open_door_remote"
SERVICE_WAKE_UP_DEVICE = "wake_up_device"
SERVICE_GET_OPEN_DOOR_RECORD = "get_open_door_record"
SERVICE_CALL_ANSWER = "call_answer"
SERVICE_CALL_REFUSE = "call_refuse"
SERVICE_CALL_HANGUP = "call_hangup"
SERVICE_GET_VOICE_REPLY = "get_voice_reply"
SERVICE_SET_VOICE_REPLY = "set_voice_reply"
SERVICE_SET_PROPERTIES = "set_properties"
SERVICE_CALL_SERVICE = "call_service"
SERVICE_DELETE_SNAPKEY = "delete_snapkey"
SERVICE_SEND_SMS_CODE = "send_sms_code"
SERVICE_AUTHORIZE_TERMINAL = "authorize_terminal"

# --- events ---------------------------------------------------------------
EVENT_PREFIX = f"{DOMAIN}_event"

# --- error codes (App 内映射) ----------------------------------------------
SUCCESS_CODES = {0, 200, 1000, 10000}
AUTH_FAIL_CODES = {3, 13, 2027, 11010}     # 认证失败/登录态失效 → 重新登录
ACCOUNT_LOCKED_CODES = {2008, 100001, 22005}
DEVICE_OFFLINE_CODES = {10003}             # 设备休眠/离线/不可达
RATE_LIMIT_CODES = {2029, 2030}

# --- model property identifiers (SKG8J5R0 / R10-M0X) ------------------------
PROP_LOCK_STATUS = "doorLockStatus"      # 0 门已锁 / 1 门未锁 / 2 未知
PROP_LOCK_STATE = "doorLockState"        # 0 关 / 1 开 (门物理状态)
PROP_POWER_STATE = "powerState"          # 0 正常 / 1 省电 / 2 超级省电
PROP_TAMPER = "tamper"
PROP_CHILD_LOCK = "child_lock"
PROP_OPEN_DOOR_BY_TOUCH = "openDoorByTouch"
PROP_OPEN_DOOR_MSG = "openDoorMsg"
PROP_WIFI_DOOR_LOCK = "wifiDoorLock"     # struct {SSID, status, intensity...}
PROP_SLEEP_STATUS = "sleepStatus"
PROP_WAKEUP_STATUS = "wakeupStatus"
PROP_DORMANT = "Dormant"
PROP_DEVICE_POWER_LOCK = "devicePowerLock"  # array: 电池列表
PROP_LOCK_NOTE_REPORT = "lockNoteReport"    # array: 开门记录
PROP_CHANNEL_NAMES = "ipc_devChnName"       # array: 摄像头通道名
PROP_CALL_TRANSFER = "sdl_callTransferSwitch"  # 0 关 / 1 开
PROP_NIGHT_MODE = "NightMode"

# --- device list status ----------------------------------------------------
STATUS_ONLINE = "online"
STATUS_SLEEP = "sleep"

# lockState 取值(设备列表)
LOCK_STATE_CLOSED = "beClosed"
LOCK_STATE_OPENED = "beOpened"
LOCK_STATE_AJAR = "beAjar"
LOCK_STATE_OPENED_KEYS = {LOCK_STATE_OPENED, LOCK_STATE_AJAR}

# 开门记录 keyType 枚举 → 中文
KEY_TYPE_NAMES = {
    "0": "密码", "1": "卡片", "2": "指纹", "3": "临时密钥", "4": "人脸",
    "5": "密码+卡片", "6": "密码+指纹", "7": "密码+人脸", "8": "卡片+指纹",
    "9": "卡片+人脸", "10": "人脸+指纹", "11": "一次性密码", "12": "周期性密码",
    "13": "动态密码", "14": "机械钥匙", "15": "远程用户", "16": "门内开门",
    "17": "室内机开门", "18": "室外机开门", "19": "二维码", "20": "手机",
    "21": "管理员密码", "22": "管理员指纹", "23": "管理员密码+指纹",
}

# 星期(select 选项) ↔ 设备模型 period 枚举(0=周日 .. 6=周六)
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
WEEKDAY_TO_PERIOD = {
    "Sunday": 0, "Monday": 1, "Tuesday": 2, "Wednesday": 3,
    "Thursday": 4, "Friday": 5, "Saturday": 6,
}
SNAPKEY_WEEKDAY_OPTIONS = ("Every day", "Weekdays", "Weekend") + WEEKDAYS

DEFAULT_SNAPKEY_CONFIG = {
    "name": "Home Assistant",
    "effective_num": -1,   # 使用次数(-1 不限)
    "effective_day": 1,    # 有效天数
    "begin_time": "00:00:00",
    "end_time": "23:59:59",
    "weekday_mode": "Every day",
}

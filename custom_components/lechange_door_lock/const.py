"""Constants for the LeChange (Imou) Door Lock integration.

Client-side cloud API endpoints:
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

# --- 设备密码体系(两套密码: 同一 KDF 的两个代次) ----------------------------
# 安全码 = 设备标签出厂值(设备密码的出厂初始值) → 历史告警图解密(出厂代)
CONF_SECURITY_CODE = "security_code"
# 设备密码 = App"修改设备密码"后的当前值 → 实时流帧解密(当前代);
# 未修改过设备密码时与安全码相同,留空则回退用安全码。
CONF_DEVICE_PASSWORD = "device_password"

# --- 本地通道地址接口(每通道可选局域网直连; 设备不在 LAN 时接口仍可用) --------
# 值格式: 单通道可用 "host[:port]" 或完整 "rtsp://user:pass@host:port/...";
# 组合通道用 URL 查询参数 ?channelId=N 选择, 或多行 "通道号=host[:port]"。
# 为空 → 走云端 RTSV1 链路(节流+唤醒)。设备回网后此处即生效, 无需改配置。
CONF_CHANNEL_HOSTS = "channel_hosts"          # JSON dict: {"0": "192.168.1.10:554", "1": "..."}

# --- 云端媒体(RTSV1 / 快照节流) --------------------------------------------
CONF_CAMERA_AUTO_IMAGE = "camera_auto_image"   # 摄像头自动取图总开关
DEFAULT_CAMERA_AUTO_IMAGE = False    # 默认关(取流/CGI 都耗电); 手动服务/按钮不受限
CONF_SNAPSHOT_MIN_INTERVAL = "snapshot_min_interval"  # 门外截图最小间隔(秒)
DEFAULT_SNAPSHOT_MIN_INTERVAL = 60   # 电池设备: 默认 60s 节流(取流自带唤醒)
CONF_SNAPSHOT_STREAM_ID = "snapshot_stream_id"        # 取流码流: '1'主(默认)/'2'子
DEFAULT_SNAPSHOT_STREAM_ID = "1"     # ★ 仅主码流在中继有数据, 子码流零包
CONF_SNAPSHOT_OSD = "snapshot_osd"   # 门外截图 OSD(时间戳+通道名; 可关)
DEFAULT_SNAPSHOT_OSD = True          # ★ 默认开; 用户可关
CONF_SNAPSHOT_OSD_ALPHA = "snapshot_osd_alpha"        # OSD 底色不透明度 0-255
DEFAULT_SNAPSHOT_OSD_ALPHA = 160     # ≈63% 黑, 半透明
# --- 实时预览 OSD(录制/预览视频流烧录, 独立开关, 可设置不添加) --------------
CONF_STREAM_PREVIEW_OSD = "stream_preview_osd"        # 实时预览 OSD 开关
DEFAULT_STREAM_PREVIEW_OSD = True    # 默认开(时间戳+通道名烧录); 可关
CONF_STREAM_PREVIEW_SECONDS = "stream_preview_seconds"  # 预览录制时长(秒)
DEFAULT_STREAM_PREVIEW_SECONDS = 10
CONF_SNAPSHOT_LAYOUT = "snapshot_layout"              # 多通道布局
LAYOUT_HSTACK = "hstack"             # 左右(默认)
LAYOUT_VSTACK = "vstack"             # 上下
LAYOUT_SINGLE = "single"             # 单摄单图(配 CONF_SNAPSHOT_CHANNELS)
DEFAULT_SNAPSHOT_LAYOUT = LAYOUT_HSTACK
CONF_SNAPSHOT_CHANNELS = "snapshot_channels"          # 截取通道: '0+1'双摄 / '0'猫眼 / '1'辅摄
CHANNELS_DUAL = "0+1"                # 双摄组合(默认)
DEFAULT_SNAPSHOT_CHANNELS = CHANNELS_DUAL
SNAPSHOT_CHANNEL_OPTIONS = (CHANNELS_DUAL, "0", "1")
SNAPSHOT_LAYOUT_OPTIONS = (LAYOUT_HSTACK, LAYOUT_VSTACK, LAYOUT_SINGLE)
MEDIA_APIVER = "191204"              # things.media.* 域(实测)
MEDIA_STREAM_PAYLOAD_TYPES = {96, 98}  # RTP PT: 96=视频(DHAV)
STREAM_CONNECT_TIMEOUT = 15
STREAM_KEEPALIVE_INTERVAL = 8        # 中继 OPTIONS keepalive(实测 8-10s)
STREAM_MAX_FRAME = 8 * 1024 * 1024

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
PLATFORMS = ["sensor", "binary_sensor", "button", "switch", "camera",
             "number", "select", "text", "time"]

# --- services -------------------------------------------------------------
SERVICE_GENERATE_SNAPKEY = "create_snapkey"
SERVICE_GET_SNAPKEY_LIST = "get_snapkey_list"
SERVICE_OPEN_DOOR_REMOTE = "open_door_remote"
SERVICE_WAKE_UP_DEVICE = "wake_up_device"
SERVICE_GET_OPEN_DOOR_RECORD = "get_open_door_record"
SERVICE_DOORFRONT_SNAPSHOT = "doorfront_snapshot"   # 门外截图(云端取流抽帧)
SERVICE_RELOAD_DATA = "reload_data"                 # 强制重载设备全部数据(持久会话)
SERVICE_DUMP_DIAGNOSTICS = "dump_diagnostics"       # 诊断转储: 原始响应+解码键清单
SERVICE_ALARM_IMAGE = "alarm_image"                 # 告警抓拍图下载+解码
SERVICE_RECORD_PREVIEW = "record_preview"           # 实时预览录制(可选 OSD)
SERVICE_CALL_ANSWER = "call_answer"
SERVICE_CALL_REFUSE = "call_refuse"
SERVICE_CALL_HANGUP = "call_hangup"
SERVICE_GET_VOICE_REPLY = "get_voice_reply"
SERVICE_SET_VOICE_REPLY = "set_voice_reply"
SERVICE_SET_PROPERTIES = "set_properties"
SERVICE_SET_CREDENTIALS = "set_credentials"         # 显式设置/清空安全码/设备密码
SERVICE_CALL_SERVICE = "call_service"
SERVICE_DELETE_SNAPKEY = "delete_snapkey"
SERVICE_SEND_SMS_CODE = "send_sms_code"
SERVICE_AUTHORIZE_TERMINAL = "authorize_terminal"

# --- GT4 网页滑块助手 --------------------------------------------------------
# 滑块页由 gt4_helper 生成并挂在 HA 自身 HTTP 端口(config_flow 使用时缓存),
# 用户手动完成滑块后页面 POST 四元组回 HA;gt4_helper 负责校验+重试登录。
GT4_HTML_FILENAME = "gt4.html"
GT4_LISTEN_PORT = 8765
GT4_LISTEN_PATH = "/gt4"

# --- events ---------------------------------------------------------------
EVENT_PREFIX = f"{DOMAIN}_event"

# --- error codes (客户端行为映射) --------------------------------------------
SUCCESS_CODES = {0, 200, 1000, 10000}
# 认证失败/登录态失效 → 重新登录。12002 = 签名密钥/token 被作废(实测:同账号
# 任意端 GetToken 新登录会作废旧 token,单 token 策略;判别实验:
# 错误密钥→12002,错误/缺失 sessionId→10000,故 12002 是"重登"的可靠信号)
# 12001 = token 未激活(来源 sid 无登录史);12002 = 曾激活已失效。
AUTH_FAIL_CODES = {3, 13, 2027, 11010, 12001, 12002}
ACCOUNT_LOCKED_CODES = {2008, 100001, 22005}
DEVICE_OFFLINE_CODES = {10003}             # 设备休眠/离线/不可达
RATE_LIMIT_CODES = {2029, 2030}

# --- GT4 / 终端信任 -----------------------------------------------------------
# GT4 captchaId 为乐橙客户端通用公开值;OEM AK/SK 为设备厂商接入凭据,
# 不随源码分发 — 使用者通过环境变量或 options 提供(留空则 GT4 校验不可用)。
GT4_CAPTCHA_ID = os.environ.get("LECHANGE_GT4_CAPTCHA_ID", "27e3ebaf32906618c3eb8bc69035903a")
# OEM AK/SK: 乐橙客户端内置厂商接入凭据(oem_config_server.lc 解析), 与 GT4_CAPTCHA_ID
# 同属客户端通用公开值, 此前已随历史发布出库 → 按发布形态内置缺省值, 开箱即用;
# 环境变量仍可覆盖(多账号/自 research 场景)。
OEM_AK = os.environ.get("LECHANGE_OEM_AK", "2QnTkhG3^t!rKXNP")
OEM_SK = os.environ.get("LECHANGE_OEM_SK", "%^k#1DI2gI#hdNK%eb#JPk@nJIxGXV1U")
# 12114 = 风险态 GT4 拦截(captchaData.verifyToken);
# 12112 = 终端绑定校验(新终端首登, 走授信链 GrantingCredit)。
NEED_GT4_CODES = {12114}
NEED_CREDIT_CODES = {12112}

# --- model property identifiers ---------------------------------------------
PROP_LOCK_STATE = "doorLockState"        # 0 关 / 1 开 (门物理状态)
PROP_POWER_STATE = "powerState"          # 0 正常 / 1 省电 / 2 超级省电
PROP_POWER_MODE = "powerMode"            # 工作模式: 0 自动 / 1 正常 / 2 省电 / 3 超级省电
PROP_TAMPER = "tamper"
PROP_CHILD_LOCK = "child_lock"
PROP_WIFI_DOOR_LOCK = "wifiDoorLock"     # struct {SSID, status, intensity...}
PROP_SLEEP_STATUS = "sleepStatus"
PROP_WAKEUP_STATUS = "wakeupStatus"
PROP_DORMANT = "Dormant"
PROP_DEVICE_POWER_LOCK = "devicePowerLock"  # array: 电池列表
PROP_LOCK_NOTE_REPORT = "lockNoteReport"    # array: 开门记录
PROP_CHANNEL_NAMES = "ipc_devChnName"       # array: 摄像头通道名
PROP_CALL_TRANSFER = "sdl_callTransferSwitch"  # 0 关 / 1 开

# 工作模式(powerMode)枚举 → select/sensor 选项键
# ★ 枚举值按 powerState(0 正常/1 省电/2 超级省电)的既有约定顺延:
#   自动=0 插入首位; 真机抓包如有出入, 只需调整本表
WORK_MODE_OPTIONS = ("auto", "normal", "power_saving", "super_power_saving")
WORK_MODE_TO_VALUE = {
    "auto": 0, "normal": 1, "power_saving": 2, "super_power_saving": 3,
}
VALUE_TO_WORK_MODE = {v: k for k, v in WORK_MODE_TO_VALUE.items()}

# --- device list status ----------------------------------------------------
STATUS_ONLINE = "online"
STATUS_SLEEP = "sleep"

# lockState 取值(设备列表)
LOCK_STATE_CLOSED = "beClosed"
LOCK_STATE_OPENED = "beOpened"
LOCK_STATE_AJAR = "beAjar"
LOCK_STATE_OPENED_KEYS = {LOCK_STATE_OPENED, LOCK_STATE_AJAR}

# 云侧告警 labelType 行为映射(抓包 API/capture 20260905: R10-Max 的
# 开门/出门事件统一为 accessAlarm, 见 UnlockRecordWithDate 页);
# 其余类型码捕获后继续补充, 未收录的原样透传(数字码仍被展示层抑制)
ALARM_LABEL_TYPE_NAMES = {
    "accessAlarm": "开门/出门事件",
}

# 开门记录 keyType 枚举 → 中文
KEY_TYPE_NAMES = {
    "0": "密码", "1": "卡片", "2": "指纹", "3": "临时密钥", "4": "人脸",
    "5": "密码+卡片", "6": "密码+指纹", "7": "密码+人脸", "8": "卡片+指纹",
    "9": "卡片+人脸", "10": "人脸+指纹", "11": "一次性密码", "12": "周期性密码",
    "13": "动态密码", "14": "机械钥匙", "15": "远程用户", "16": "门内开门",
    "17": "室内机开门", "18": "室外机开门", "19": "二维码", "20": "手机",
    "21": "管理员密码", "22": "管理员指纹", "23": "管理员密码+指纹",
}

DEFAULT_SNAPKEY_CONFIG = {
    "name": "Home Assistant",
    "effective_num": -1,   # 使用次数(-1 不限)
    "effective_day": 1,    # 有效天数
    "begin_time": "00:00:00",
    "end_time": "23:59:59",
    # weekday_mode 已移除: 系统自动按每天(掩码 127)分配, 不再提供配置项
}

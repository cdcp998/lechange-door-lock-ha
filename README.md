## LeChange Door Lock 乐橙门锁 Home Assistant 集成

### 📌 项目简介
本项目是一个 Home Assistant 自定义集成，用于连接和控制乐橙（LeChange/Imou）智能门锁。
**v1.1.0 起改用从客户端 App 抓取的私有云协议**（账号+密码 → x-pcs 签名 → 区域网关），
不再依赖更新缓慢的乐橙开放平台接口（openapi.lechange.cn / 开放平台 AppID）。
**v1.6.0 完成会话信任模型落地**:集成侧持有持久 sessionId(首次激活后即具备续期
资格),会话失效自动走密码续期链恢复;风控人机验证(GT4)在 HA 本地完成,日常运行
零人工介入。
### 很多功能没有实际测试.属于开发阶段,遇到问题请在[Issues](https://github.com/cdcp998/lechange-door-lock-ha/issues) 页面反馈
### ⚠️ 免责声明 (Disclaimer)
> [!WARNING]
> **本集成是非官方、社区开发的第三方集成,与乐橙(LeChange/Imou)官方无任何关联,亦未经其认可或背书。**

1. **非官方**:本集成使用的客户端协议系基于对**网络流量与乐橙移动应用(App)**的
   独立分析而来,非乐橙官方提供。
2. **风险自担,无任何保证**:使用本集成即表示您自行承担全部风险,项目不提供任何
   明示或默示的保证(包括可用性、准确性或特定用途适用性)。
3. **数据说明**:本集成需要登录您的乐橙账号并通过乐橙官方云服务完成设备访问,
   数据仅在本机 Home Assistant 与乐橙云之间传输。本集成**不收集、不存储、
   不向任何第三方服务器转发**您的数据;密码保存在 Home Assistant 配置条目中,
   请自行评估存储风险。
4. **随时可能失效**:乐橙 App/服务端/固件更新随时可能改变协议或加签规则,
   可能导致本集成在任意时间失效;由此导致的无法使用、账号异常或其他损失,
   本项目不承担责任。
5. **高危操作**:远程开锁、唤醒、呼叫应答/拒绝、语音回复、摄像头推流等均为
   高影响操作,请自行确保自动化与权限配置安全,后果由使用者承担。

> 使用本项目即表示您理解并接受上述全部条款,自行承担全部风险。

### ✨ 功能特性
- **门锁实体**：`lock.*` 显示 已锁/未锁，支持远程开锁（remoteOpenDoor）。
- **状态监控**：门锁电池/摄像机电池电量、门状态（关/开）、电量模式、WiFi 信号、最近开门记录。
- **开门信息**：最近开门时间（格式化）与最近开门方式（密码/卡片/指纹/远程…），来自 `lockNoteReport`。
- **安全传感器**：在线状态、通道在线状态、休眠状态、防拆报警、童锁。
- **控制开关**：童锁、呼叫转接（`iot.control.SetProperties`）。
- **远程控制**：远程开门按钮;**「获取门外截图」按钮**(替代原“唤醒设备”——无效操作;拉取摄像头/门铃即唤醒锁体,快照保存到 `www/lechange/` 并经 `/local/...` URL 事件返回)。
- **📹 视频**：每个摄像头通道生成 camera 实体，**默认云端 RTSV1 实时取流抽帧**（节流 60s，双摄自动组合 + 半透明 OSD；支持局域网 CGI/RTSP/go2rtc 覆盖）。
- **🗣️ 对话**：应答/拒绝/挂断门铃呼叫（CallAnswer/CallRefuse/CallHangup）、获取/设置语音回复（GetVoiceReply/SetVoiceReply）。
- **📡 MQTT 实时通道**：设备在线状态/属性推送实时更新；控制指令（如远程开门）**MQTT 优先 → 云 API 自动兜底**；断线自动重试，不影响轮询。
- **🔑 临时密码**：**实体化配置**（名称/使用次数/有效天数/生效星期/时间段，number·select·text·time 四类实体）+「生成临时密码」「刷新列表」「删除」按钮服务 + 数量传感器；基于消息域云 API（`iot.message.SmartLockSecretAdd/ListV2/Delete`），**设备休眠可用、客户端自产密码、不触发短信验证码**。
- **云侧告警**：轮询 `cloud.message.GetDeviceAlarmMixMessage`（设备休眠照常返回），新告警触发 `alarm` 事件，`最新告警` 传感器展示标签/时间/消息。
- **开门记录**：轮询 `lockNoteReport` 属性，新记录会触发事件。
- **会话自动续期**：会话失效时自动使用存储的账号密码重新登录。v1.6.0 起,续期链
  携带集成侧自有持久 sessionId(该 sid 首次激活后即具备续期资格),会话失效自动恢复,
  日常运行无需人工。
- **GT4 本地滑块验证**：账号触发风控(12114)时,配置流程自动在 HA 自身 HTTP 端口
  生成人机验证滑块页(`/api/lechange/gt4/slides`),浏览器完成滑块后自动接力
  (校验 → 发送短信 → 回到验证码输入),不需要打开乐橙 App;容器部署无需额外端口映射。
- **国际化**：内置中文、英文界面翻译。

### 📦 安装方法
#### 方式一：HACS 安装（推荐）
1. 在 HACS 中点击“自定义存储库”，添加此仓库 URL，类别选择“集成”。
2. 搜索并安装 `LeChange Door Lock`，重启 Home Assistant。

#### 方式二：手动安装
1. 将文件夹 `lechange_door_lock` 复制到 Home Assistant 的 `custom_components` 目录下。
2. 重启 Home Assistant。

> **⚠️ ffmpeg 依赖**: 门外截图/实时预览 OSD 需要 ffmpeg。HA OS/Supervised 自带
> (集成已声明 `dependencies: ["ffmpeg"]`); 若使用容器/自定义安装, 请确保 ffmpeg
> 在 PATH 中(可用 `ffmpeg -version` 验证), 缺失时快照/预览功能不可用并记录日志提示。

### 🔧 配置说明
1. 进入 **设置 → 设备与服务 → 添加集成 → LeChange Door Lock**。
2. 输入您的**乐橙 App 账号**,并选择登录方式:
   - **账号密码**:输入 App 密码。若账号触发风控(需人机验证),流程会自动进入
     **本地 GT4 滑块页**(见下节),完成后回到密码登录继续。
   - **短信验证码**:集成直接发送验证码;若触发风控,同样先完成本地 GT4 滑块,
     通过后短信自动重新发送,输入收到的 6 位验证码登录。
3. 自动登录并列出账号下设备,选择门锁完成配置。

#### GT4 本地滑块(v1.6.0)
账号连续输错密码、新终端首次登录或账号处于风控状态时,乐橙服务端要求通过极验 GT4
人机验证(错误码 12114)。v1.6.0 起该验证在 HA 内完成,流程如下:

1. 配置流程检测到 12114 后,自动在 HA 自身 HTTP 端口生成滑块页面。
2. 在浏览器打开 `http://<HA地址>:8123/api/lechange/gt4/slides`,完成滑块。
3. 页面自动将验证结果回传 HA(`POST /api/lechange/gt4/tuple`),集成完成服务端校验
   并重发短信或继续登录,回到配置流程继续操作。

滑块页挂在 HA 原生 HTTP 端口上:HA OS/Supervised/容器/venv 均无需额外端口映射;
反向代理与 HTTPS 场景下回传地址使用相对路径,自动跟随。

> ⚠️ 密码仅用于登录与失效后自动重登，存储在 HA 配置条目中，请自行评估安全风险。
> 短信验证码登录不保存密码。v1.6.0 起短信登录同样持久化集成侧 sessionId,
> 会话失效后自动走密码续期;若配置时未填写密码,会话失效后需重新添加配置。

### 📹 视频(摄像头)配置
添加设备时集成会保存账号下的云端信息,并为每个摄像头通道生成 camera 实体。
**v1.5.0 起默认走云端 RTSV1 私有协议实时取流抽帧**
(无需 LAN/开放平台),电池设备自动节流。进入集成的 **选项** 可细调:
- `security_code`：**安全码**（机身标签 8 位,出厂代）→ 告警抓拍图解密（必须）。
- `device_password`：**设备密码**（App 修改设备密码后的当前值）→ 云端预览帧解密；
  未改过则与安全码相同,留空即可。
- `snapshot_min_interval`：门外截图最小间隔（默认 60s,`≥15`;取流请求会唤醒电池设备）。
- `snapshot_stream_id`：码流偏好,默认主码流 `1`;子码流实测中继无数据,自动回退主码流。
- `snapshot_layout`：双摄组合布局 — `hstack` 左右(默认) / `vstack` 上下 / `single` 单摄单图。
- `snapshot_channels`：组合通道 — `0+1` 双摄(默认) / `0` 仅猫眼 / `1` 仅辅摄。
- `snapshot_osd` / `snapshot_osd_alpha`：OSD 叠加层(时间戳+通道标签)开关与底色不透明度。
- `channel_hosts`：**本地通道地址接口**（每行 `通道号=局域网IP[:端口]`,如
  `0=192.168.1.10:80`）;设备在 LAN 时优先走 `cgi-bin/snapshot.cgi` 直连快照
  （无唤醒、零云端流量）,设备不在本网时自动回退云端链路 —— 接口常驻,回网即生效。
- `rtsp_url` / `rtsp_host`：传统 RTSP/go2rtc 中转覆盖（可选,优先级高于云端快照）。

> 云端取流走私有 TLS/WSSE 握手(`rtpxav` 中继 + DHAV 帧加密),HA camera 实体以
> **轮询快照**方式呈现(未配置 RTSP 覆盖时不声明 STREAM 特性);配置了 `rtsp_url`
> 或局域网 RTSP 则仍可由 HA stream 组件全帧播放。

### 🖥️ 使用说明
#### 实体列表（每台设备）
| 实体类型 | 功能 | 翻译键 |
|----------|------|--------|
| 门锁 | 已锁/未锁 + 远程开锁 | `lock.<name>_lock` |
| 传感器 | 门锁电池电量 / 摄像头电池电量 | `sensor.<name>_battery_lock` 等 |
| 传感器 | 门状态 / 电量模式 / WiFi 信号 / 最近开门记录 | `sensor.<name>_door_state` 等 |
| 传感器 | 最近开门时间 / 最近开门方式 | `sensor.<name>_latest_open_door_time` 等 |
| 传感器 | 临时密码数量（属性含列表与最近生成） | `sensor.<name>_snapkey_count` |
| 二进制传感器 | 在线 / 休眠 / 防拆 / 童锁 | `binary_sensor.<name>_online` 等 |
| 二进制传感器 | 通道 0 / 1 在线状态 | `binary_sensor.<name>_channel_online` 等 |
| 按钮 | 远程开门 / **获取门外截图** | `button.<name>_open_door` / `button.<name>_snapshot_door` |
| 按钮 | 生成临时密码 / 刷新临时密码列表 | `button.<name>_generate_snapkey` 等 |
| 开关 | 童锁 / 呼叫转接 | `switch.<name>_child_lock` 等 |
| 数字/选择/文本/时间 | 临时密码配置（次数/天数/星期/名称/时间段） | `number.<name>_snapkey_*` 等（配置类） |
| 摄像头 | 通道 0 / 1 摄像头（视频） | `camera.<name>_camera_0` 等 |

#### 临时密码使用（实体化配置）
1. 在 **数字**（使用次数/有效天数）、**选择**（生效星期：每天/工作日/周末/单日）、
   **文本**（名称）、**时间**（开始/结束）中设置参数（持久化到集成选项）。
2. 点击 **`button.generate_snapkey`** 生成；最近生成的密码在按钮属性
   `last_generated_password` / `last_generated_name` 与 `sensor.snapkey_count` 属性中查看，
   同时触发 `lechange_door_lock_event`（`type: snapkey_created`）。
3. 点击 **`button.refresh_snapkey_list`** 拉取当前列表；`sensor.snapkey_count` 显示数量。
4. 也可直接调用 `create_snapkey` / `get_snapkey_list` 服务（参数化调用）。
5. 设备休眠时（status=sleep）生成会返回 `10003`，需先唤醒后重试。

#### 支持的服务
| 服务 | 描述 |
|------|------|
| `lechange_door_lock.open_door_remote` | 远程开门（remoteOpenDoor） |
| `lechange_door_lock.wake_up_device` | 唤醒休眠设备(已由「获取门外截图」按钮替代,仅保留兼容) |
| `lechange_door_lock.call_answer` | 应答门铃呼叫 |
| `lechange_door_lock.call_refuse` | 拒绝门铃呼叫 |
| `lechange_door_lock.call_hangup` | 挂断门铃呼叫 |
| `lechange_door_lock.get_voice_reply` | 获取语音回复列表 |
| `lechange_door_lock.set_voice_reply` | 设置语音回复 |
| `lechange_door_lock.create_snapkey` | 生成临时密码（客户端生成,不触发验证码） |
| `lechange_door_lock.get_snapkey_list` | 临时密码分组列表（云,设备休眠可用） |
| `lechange_door_lock.delete_snapkey` | 删除临时密码（需 keyId,extra 可传全字段） |
| `lechange_door_lock.get_open_door_record` | 开门记录 |
| `lechange_door_lock.doorfront_snapshot` | 门外截图（云端取流抽帧;支持 `channels`/`layout`/`osd` 按次覆盖,结果经事件+可选保存） |
| `lechange_door_lock.alarm_image` | 告警抓拍图（picUrl 下载+DHAV 解码,`alarm_id` 可选,结果经事件） |
| `lechange_door_lock.set_properties` | 通用属性写入 |
| `lechange_door_lock.call_service` | 通用服务调用（高级/调试） |

#### 事件（自动化用）
服务与轮询通过 `lechange_door_lock_event` 事件返回结果，`type` 取值：
- `open_door` / `wake_up` / `call_answer` / `call_refuse` / `call_hangup`
- `snapshot`（门外截图已保存，含 `url`/`bytes` 字段）
- `doorfront_snapshot`（云端门外截图,含 `size`/`url`/`channels`/`layout`）
- `alarm_image`（告警抓拍图解码,含 `alarm_id`/`time`/`title`/`url`）
- `voice_reply_list` / `voice_reply_set` / `snapkey_created` / `snapkey_list` / `snapkey_deleted`
- `open_door_records` / `set_properties` / `service_result`
- `open_record`（新开门记录，含 `record` 字段，如用户/方式）
- `alarm`（新云侧告警，含 `alarm` 字段：labelType/refId/time/message，设备休眠可用）

#### 自动化示例：门铃呼叫后应答
```yaml
# 开门记录事件 → 自动应答
automation:
  - alias: "Answer doorbell call"
    trigger:
      platform: event
      event_type: lechange_door_lock_event
      event_data:
        type: open_record   # 或按需监听 type: call
    action:
      service: lechange_door_lock.call_answer
      data:
        device_id: "600EBBDRSF00000"
        user_info: "{{ trigger.event.data.record.userInfo | default('') }}"
```

#### 视频接入示例（go2rtc 中转）
```yaml
# configuration.yaml
go2rtc:
  streams:
    lechange_lock:
      - rtsp://admin:pass@192.168.1.10:554/cam/realmonitor?channel=1&subtype=0
```
然后在集成选项中把 `rtsp_url` 设置为 `rtsp://<ha>:8554/lechange_lock`（或直接填设备局域网 RTSP）。

### 🧪 测试
单元测试（不依赖 HA 运行时、不访问网络、全部使用匿名化夹具）:
```powershell
pip install -r requirements-test.txt
pytest tests/ -q
```
覆盖:x-pcs 签名头(黄金用例)、登录后密钥派生、会话失效自动重登、HTTP/网络/TLS
错误映射、型号定义 ref→identifier 递归解码(struct/array/枚举/布尔)、锁状态推导、
双电池拆分、星期→period 映射与临时密码时间段构建、开门时间格式化/方式映射、
RTSP 地址拼装、云端/局域网主机判定、AK 身份签名(sid 透传/10000 无 token 保护/
12114 处理)、GT4 滑块页生成与四元组分发等(共 153 例)。
CI(`.github/workflows/hacs.yml`)同时运行 [HACS Action](https://github.com/hacs/action)
仓库校验与上述测试。

### ⚠️ 注意事项
- **设备休眠**：门锁为省电进入休眠（status=sleep），此时 IoT 调用会返回 `10003` 服务器内部错误，状态保留上次值；唤醒后可恢复。
- **开锁为高危操作**：服务/按钮会直接远程开锁，请谨慎配置自动化，并注意是否已开启 App 内的远程开门确认。
- **节流**：`cloud_polling` 默认 30 秒轮询；若账号同时被多个集成使用，请调大 `update_interval`。
- **人机验证**：连续输错密码或触发风控时,乐橙服务端要求 GT4 人机验证。v1.6.0 起
  在 HA 内完成(见配置说明中的「GT4 本地滑块」),不再需要打开乐橙 App。
- **多端登录**:乐橙账号同一时间只保留一个活跃 token。集成与手机 App 登录会互相
  顶替,被顶一端自动重登恢复;这是服务端行为,表现为短暂互踢后各自恢复,属正常现象。
- **终端管理账号**：集成使用线上安卓客户端特征终端(clientType=android + 常见机型特征 +
  安装时固定生成的 terminalId,持久化于集成选项,不与手机 App 同终端)。若账号开启
  终端管理,首次密码登录会被拦截(错误码 12112,与密码正确性无关):配置流程会自动
  进入「终端授权验证」步骤——发送授权短信验证码 → 输入 6 位验证码 → 终端入白名单
  并自动完成登录,全程无需打开乐橙 App。

### 📜 更新日志
统一更新日志见 [CHANGELOG.md](CHANGELOG.md)(发布时 CI 会自动提取并作为 Release 说明)。
- **v1.6.0 (2026-09-04)**: 会话信任模型落地(sid 持久/token 信任继承来源 sid 登录史/
  10000 无 token 保持会话/多端互踢自愈);**自主续期登录链**(首次绑定后日常运行零人工);
  **GT4 本地滑块验证**(12114 时在 HA 自身端口完成人机验证,容器零端口映射,不再要求
  打开乐橙 App);AK 身份接口(CheckGeeTest4/GetValidCode/GetTokenBySMS);
  GrantingCredit 按线上协议校准(原码直传/无 CheckValidCode 中间步);测试 153 例。
- **v1.5.0 (2026-09-04)**: 云端实时预览/门外截图/告警图(DAV 解密)/本地通道地址;
  两套设备密码体系;OSD 字体内置;MQTT 实时通道(控制 MQTT 优先 → 云 API 兜底);
  UA 设备特征池。
- **v1.4.0 (2026-09-03)**: 临时密码改用实测验证的云消息 API(设备休眠也可生成/列表);云侧告警轮询与 `alarm` 事件、最新告警传感器;单设备详情 API;通道在线 unique_id 等修复。支持**短信验证码登录**(账号密码/短信二选一);密码登录失败定向提示(需短信验证码/人机验证)。
- **v1.3.0 (2026-09-03)**: 临时密码实体化配置(number/select/text/time)+ 生成/刷新按钮 + 数量传感器;最近开门时间/方式传感器;通道在线状态。(功能移植自社区分支持续改进)
- **v1.2.0 (2026-09-03)**: 客户端私有云协议迁移;门锁/开关/摄像头(默认云端流媒体网关)/对话实体与服务;单测 + HACS/发布 CI;MIT LICENSE 与免责声明;修复登录会话死锁。(v1.1.0 为开发中间版本,已并入)
- **v1.0.1 (2026-03-11)**: 添加开门记录(旧接口版本)。
- **v1.0.0 (2026-03-10)**: 首次发布(旧开放平台接口版本)。

### 🐛 问题反馈
请在此仓库的 [Issues](https://github.com/cdcp998/lechange-door-lock-ha/issues) 页面反馈。

### 📄 项目性质与版权说明
本项目为第三方客户端/接口学习项目,仅供个人技术研究、学习交流使用,
不得用于任何商业用途。

本项目与"乐橙(LeChange/Imou)"无关,未获得官方授权。请勿将本项目用于任何违反法律法规或
侵犯他人合法权益的行为。

本项目代码均为独立编写的互操作性实现,不包含原客户端代码、资源文件、
密钥、证书及商标。使用者下载后应在 24 小时内删除。

使用本项目产生的一切后果由使用者自行承担。

若相关权利人认为本项目存在侵权,请通过仓库
[Issues](https://github.com/cdcp998/lechange-door-lock-ha/issues) 页面联系,
本人将在收到有效通知后尽快删除相关代码及内容。

### AI使用声明
*本项目代码由AI辅助生成。
开发者已对AI生成的代码进行审查、测试与修改，确保其符合项目需求与安全标准。*

---
**感谢使用！**

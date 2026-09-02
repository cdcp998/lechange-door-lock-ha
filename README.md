## LeChange Door Lock 乐橙门锁 Home Assistant 集成

### 📌 项目简介
本项目是一个 Home Assistant 自定义集成，用于连接和控制乐橙（LeChange/Imou）智能门锁。
**v1.1.0 起改用从客户端 App 抓取的私有云协议**（账号+密码 → x-pcs 签名 → 区域网关），
不再依赖更新缓慢的乐橙开放平台接口（openapi.lechange.cn / 开放平台 AppID）。

### ⚠️ 免责声明 (Disclaimer)
> [!WARNING]
> **本集成是非官方、社区开发的第三方集成,与乐橙(LeChange/Imou)官方无任何关联,亦未经其认可或背书。**

1. **非官方/逆向**:本集成使用的客户端协议系从**网络流量与乐橙移动应用(App)**中
   逆向分析而来,非乐橙官方提供。
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
- **安全传感器**：在线状态、休眠状态、防拆报警、童锁、触屏开门。
- **控制开关**：童锁、呼叫转接、触屏开门（`iot.control.SetProperties`）。
- **远程控制**：远程开门、唤醒设备按钮。
- **📹 视频**：每个摄像头通道生成 camera 实体，支持局域网 RTSP / go2rtc 中转（在集成选项里配置地址）。
- **🗣️ 对话**：应答/拒绝/挂断门铃呼叫（CallAnswer/CallRefuse/CallHangup）、获取/设置语音回复（GetVoiceReply/SetVoiceReply）。
- **临时密码**：生成/查询临时密码（CreateDeviceSnapkey/GetDeviceSnapkeys），结果通过事件返回。
- **开门记录**：轮询 `lockNoteReport` 属性，新记录会触发事件。
- **会话自动续期**：会话失效时自动使用存储的账号密码重新登录。
- **国际化**：内置中文、英文界面翻译。

### 📦 安装方法
#### 方式一：HACS 安装（推荐）
1. 在 HACS 中点击“自定义存储库”，添加此仓库 URL，类别选择“集成”。
2. 搜索并安装 `LeChange Door Lock`，重启 Home Assistant。

#### 方式二：手动安装
1. 将文件夹 `lechange_door_lock` 复制到 Home Assistant 的 `custom_components` 目录下。
2. 重启 Home Assistant。

### 🔧 配置说明
1. 进入 **设置 → 设备与服务 → 添加集成 → LeChange Door Lock**。
2. 输入您的**乐橙 App 账号与密码**（与手机 App 相同，无需开放平台凭证）。
3. 自动登录并列出账号下设备，选择门锁完成配置。

> ⚠️ 密码仅用于登录与失效后自动重登，存储在 HA 配置条目中，请自行评估安全风险。

### 📹 视频(摄像头)配置
添加设备时,集成会自动保存账号下的**云端流媒体网关**地址(`streamEntryAddrV3` / `mediaConfig.streamUrl`,
如 `nginxdeviceproxy-online-hz.imou.com:443`),camera 实体默认使用该云端地址拼装 RTSP 源。
如需调整,进入集成的 **选项(选项 → 摄像头)** 配置:
- `rtsp_url`：完整 RTSP/中转地址（最高优先级），也支持 go2rtc 地址。
- `rtsp_host`：局域网 IP（留空则默认使用云端流媒体网关,端口 443）。
- `rtsp_port` / `rtsp_username` / `rtsp_password`：RTSP 端口与凭证（默认 554/admin）。
- `rtsp_subtype`：码流（0 主码流 / 1 子码流）。
- 局域网快照(`cgi-bin/snapshot.cgi`)仅在 `rtsp_host` 为局域网 IPv4 时生效。
- 设备休眠/离线时无法取流,与 App 行为一致。

> 云端流媒体网关走私有握手/加密(streamEncryModel),若无法直接推流,推荐配置
> `rtsp_url` 指向 go2rtc 等中转服务,或改用局域网直连 RTSP。

### 🖥️ 使用说明
#### 实体列表（每台设备）
| 实体类型 | 功能 | 翻译键 |
|----------|------|--------|
| 门锁 | 已锁/未锁 + 远程开锁 | `lock.<name>_lock` |
| 传感器 | 门锁电池电量 / 摄像头电池电量 | `sensor.<name>_battery_lock` 等 |
| 传感器 | 门状态 / 电量模式 / WiFi 信号 / 最近开门记录 | `sensor.<name>_door_state` 等 |
| 二进制传感器 | 在线 / 休眠 / 防拆 / 童锁 / 触屏开门 | `binary_sensor.<name>_online` 等 |
| 按钮 | 远程开门 / 唤醒设备 | `button.<name>_open_door` 等 |
| 开关 | 童锁 / 呼叫转接 / 触屏开门 | `switch.<name>_child_lock` 等 |
| 摄像头 | 通道 0 / 1 摄像头（视频） | `camera.<name>_camera_0` 等 |

#### 支持的服务
| 服务 | 描述 |
|------|------|
| `lechange_door_lock.open_door_remote` | 远程开门（remoteOpenDoor） |
| `lechange_door_lock.wake_up_device` | 唤醒休眠设备 |
| `lechange_door_lock.call_answer` | 应答门铃呼叫 |
| `lechange_door_lock.call_refuse` | 拒绝门铃呼叫 |
| `lechange_door_lock.call_hangup` | 挂断门铃呼叫 |
| `lechange_door_lock.get_voice_reply` | 获取语音回复列表 |
| `lechange_door_lock.set_voice_reply` | 设置语音回复 |
| `lechange_door_lock.create_snapkey` | 生成临时密码 |
| `lechange_door_lock.get_snapkey_list` | 临时密码列表 |
| `lechange_door_lock.get_open_door_record` | 开门记录 |
| `lechange_door_lock.set_properties` | 通用属性写入 |
| `lechange_door_lock.call_service` | 通用服务调用（高级/调试） |

#### 事件（自动化用）
服务与轮询通过 `lechange_door_lock_event` 事件返回结果，`type` 取值：
- `open_door` / `wake_up` / `call_answer` / `call_refuse` / `call_hangup`
- `voice_reply_list` / `voice_reply_set` / `snapkey_created` / `snapkey_list`
- `open_door_records` / `set_properties` / `service_result`
- `open_record`（新开门记录，含 `record` 字段，如用户/方式）

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
双电池拆分、RTSP 地址拼装、云端/局域网主机判定等。
CI(`.github/workflows/hacs.yml`)同时运行 [HACS Action](https://github.com/hacs/action)
仓库校验与上述测试。

### ⚠️ 注意事项
- **设备休眠**：门锁为省电进入休眠（status=sleep），此时 IoT 调用会返回 `10003` 服务器内部错误，状态保留上次值；唤醒后可恢复。
- **开锁为高危操作**：服务/按钮会直接远程开锁，请谨慎配置自动化，并注意是否已开启 App 内的远程开门确认。
- **节流**：`cloud_polling` 默认 30 秒轮询；若账号同时被多个集成使用，请调大 `update_interval`。
- **人机验证**：若连续输错密码或触发了风控，登录会失败（需在 App 中完成验证后再试）。

### 📜 更新日志
统一更新日志见 [CHANGELOG.md](CHANGELOG.md)(发布时 CI 会自动提取并作为 Release 说明)。
- **v1.2.0 (2026-09-03)**: 单元测试套件 50 例 + HACS/发布 CI;摄像头默认云端流媒体网关;修复登录会话死锁;免责声明与脱敏(详版见 CHANGELOG)。
- **v1.1.0 (2026-09-03)**: 切换为客户端私有云协议(账号+密码 → x-pcs 签名);新增门锁/开关/摄像头实体、对话服务(呼叫应答/拒绝/挂断、语音回复)、临时密码与开门记录事件;会话自动续期。
- **v1.0.1 (2026-03-11)**: 添加开门记录(旧接口版本)。
- **v1.0.0 (2026-03-10)**: 首次发布(旧开放平台接口版本)。

### 🐛 问题反馈
请在此仓库的 [Issues](https://github.com/cdcp998/lechange-door-lock-ha/issues) 页面反馈。

---

**感谢使用！**

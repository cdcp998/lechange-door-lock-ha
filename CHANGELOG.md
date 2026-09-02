# 更新日志 / Changelog

本文件为统一更新日志(Keep a Changelog 风格)。发布版本时,CI 会自动从本文件提取
对应版本的章节作为 GitHub Release 说明,并附带组件 ZIP 安装包。

版本号与 `custom_components/lechange_door_lock/manifest.json` 保持一致。

---

## [1.2.0] - 2026-09-03

### 新增
- 单元测试套件(50 例:x-pcs 签名/型号解码/状态推导/RTSP 拼装,匿名化夹具)。
- CI:`.github/workflows/hacs.yml`(HACS Action 仓库校验 + pytest)。
- 发布 CI:`.github/workflows/release.yml`(tag v* → ZIP 安装包 + 变更说明)。
- 摄像头默认使用**云端流媒体网关**地址(添加设备时自动保存,无需手动配置)。
- 免责声明、统一更新日志(CHANGELOG.md)、`.gitignore` 敏感目录隔离。

### 修复
- 登录后请求的 `token/内部用户名` 未解析导致的会话失效与重登死锁(单元测试发现)。
- `lc_api.py` 登录后密钥派生(md5/sha256(token)),修正 12002 误报。

## [1.1.0] - 2026-09-03

### 变更
- **切换为客户端私有云协议**——账号+密码 → x-pcs 签名 → 区域网关,替换旧开放平台接口。

### 新增
- 门锁实体(lock):已锁/未锁 + 远程开锁(remoteOpenDoor)。
- 开关:童锁、呼叫转接、触屏开门(iot.control.SetProperties)。
- 摄像头(视频):每通道 camera 实体,局域网 RTSP / go2rtc 中转。
- 对话服务:应答/拒绝/挂断门铃呼叫(CallAnswer/CallRefuse/CallHangup)、
  获取/设置语音回复(GetVoiceReply/SetVoiceReply)。
- 传感器:门锁/摄像机双电池、门状态、电量模式、WiFi 信号、最近开门记录。
- 二进制传感器:在线/休眠/防拆/童锁/触屏开门。
- 临时密码 CreateDeviceSnapkey/GetDeviceSnapkeys,结果经事件返回。
- 开门记录事件(lockNoteReport 轮询对比)。
- 会话自动续期、会话持久化。

## [1.0.1] - 2026-03-11

- 添加开门记录(旧开放平台接口版本)。

## [1.0.0] - 2026-03-10

- 首次发布(旧开放平台接口版本:设备状态、电量、远程开门、唤醒、临时密码)。

# 更新日志 / Changelog

本文件为统一更新日志(Keep a Changelog 风格)。发布版本时,CI 会自动从本文件提取
对应版本的章节作为 GitHub Release 说明,并附带组件 ZIP 安装包。

版本号与 `custom_components/lechange_door_lock/manifest.json` 保持一致。

> 规则:**功能/行为变化才升版本**;CI、文档、测试、许可证等配套改动统一归入
> 当前版本条目,不单独刷版本号。

---

## [1.3.0] - 2026-09-03

### 新增(移植自社区分支持续改进,基于客户端私有云 API)
- **临时密码实体化配置**:number(使用次数/有效天数)、select(生效星期:每天/工作日/周末/单日)、
  text(名称)、time(开始/结束时间)4 类配置实体,配置持久化到集成选项。
- 按钮:按配置**生成临时密码**(CreateDeviceSnapkey,最近结果经按钮属性/事件返回)、
  **刷新临时密码列表**(GetDeviceSnapkeys)。
- 传感器:临时密码数量(含列表与最近生成结果),**最近开门时间**(格式化为 yyyy-MM-dd HH:mm:ss)、
  **最近开门方式**(密码/卡片/指纹/远程…,来自 lockNoteReport.keyType)。
- 二进制传感器:主/辅摄像头**通道在线状态**(channelList[].status)。
- 共享实体基类 entity.py,单元测试扩展(星期映射/时间段构建/时间格式化/方式映射)。

## [1.2.0] - 2026-09-03

### 协议与核心
- **切换为客户端私有云协议**——账号+密码 → x-pcs 双签名 → 区域网关,替换旧开放平台接口。
- 会话自动续期、持久化;型号定义 identifier↔ref 递归解码(99 属性/31 服务)。

### 新增功能
- 门锁实体(lock):已锁/未锁 + 远程开锁(remoteOpenDoor)。
- 开关:童锁、呼叫转接、触屏开门(iot.control.SetProperties)。
- **摄像头(视频)**:每通道 camera 实体,**默认使用云端流媒体网关**地址(添加设备时自动保存),
  支持局域网 RTSP / go2rtc 中转与快照。
- **对话服务**:应答/拒绝/挂断门铃呼叫(CallAnswer/CallRefuse/CallHangup)、
  获取/设置语音回复(GetVoiceReply/SetVoiceReply)。
- 传感器:门锁/摄像机双电池、门状态、电量模式、WiFi 信号、最近开门记录。
- 二进制传感器:在线/休眠/防拆/童锁/触屏开门。
- 临时密码 CreateDeviceSnapkey/GetDeviceSnapkeys,结果经事件返回。
- 开门记录事件(lockNoteReport 轮询对比)。

### 修复
- 登录后请求的 `token/内部用户名` 未解析导致的会话失效与重登死锁(单元测试发现)。
- `lc_api.py` 登录后密钥派生(md5/sha256(token)),修正 12002 误报。

### 质量与合规
- 单元测试套件 50 例(签名/型号解码/状态推导/RTSP 拼装,匿名化夹具,不依赖网络与私有数据)。
- CI:`.github/workflows/hacs.yml`(HACS Action 仓库校验 9 项 + pytest)。
- 发布 CI:`.github/workflows/release.yml`(tag v* → ZIP 安装包,说明自动取自本文件)。
- 新增 MIT LICENSE、README 免责声明、配置向导风险提示。
- 统一更新日志(CHANGELOG.md);`.gitignore` 隔离敏感目录(逆向依据/实测记录仅存本地);公开文件脱敏。

> v1.1.0 为开发中间版本(未发布),其内容已并入本版本,不再单独记录。

## [1.0.1] - 2026-03-11

- 添加开门记录(旧开放平台接口版本)。

## [1.0.0] - 2026-03-10

- 首次发布(旧开放平台接口版本:设备状态、电量、远程开门、唤醒、临时密码)。

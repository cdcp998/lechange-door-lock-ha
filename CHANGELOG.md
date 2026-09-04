# 更新日志 / Changelog

本文件为统一更新日志(Keep a Changelog 风格)。发布版本时,CI 会自动从本文件提取
对应版本的章节作为 GitHub Release 说明,并附带组件 ZIP 安装包。

版本号与 `custom_components/lechange_door_lock/manifest.json` 保持一致。

> 规则:**版本号只在发布时确定**。日常新功能/修复统一合并进当前未发布版本条目,
> 发布(tag v*)时再固化 — 不刷版本号。

---

## [未发布]

### 修复
- **添加设备最后一步 "Unknown error occurred"(经常出现)**:自 v1.2.0(迁移
  至客户端私有云协议)起,`async_login` 返回键 `"username"`(云端内部账号),
  而配置流程最后一步(设备选择+安全码)读取不存在的键 `"internal_username"`
  → `KeyError` 未捕获 → HA 前端报 "未知错误",设备无法通过配置流程添加。
  现改为:键名双兼容回退(`internal_username` → `username`),最后一步全部
  字段缺键回退兜底,登录数据整体缺失时也能创建 entry(由 EVERGREEN 自动续期链
  补全会话),不再出现未捕获异常。新增回归测试 6 例
  (`tests/test_config_flow_device_step.py`,自带 homeassistant 最小桩)。

---

## [v1.6.1] - 2026-09-04

### UI 增强
- **GT4 滑块步骤显示完整可点击链接**:配置流程的人机验证步骤现在通过 `get_url()`
  解析出完整 URL(外网 → 内网 → HA Cloud 逐级回退),前端渲染为可直接点击的超链接,
  无需再手动拼接 `host:8123`;相对路径保留为兜底提示。

### 修复
- **hacs.json 恢复最低 HA 版本约束**(`homeassistant: 2024.1.0`):集成使用了
  `asyncio.timeout`(Python 3.11+ / HA 2023.10+),低于此版本安装时 HACS 会正确阻止。

---

## [v1.6.0] - 2026-09-04

### 会话信任模型
- **sid 持久**:sessionId 由终端生成后持久保存,每次登录复用同一 sid;token 每次登录签发。
- **token 信任度继承来源 sid 的登录历史**:有登录史(至少一次 Login 10000)的 sid
  签发的 token 直接激活;无登录史的 sid 签发的 token 保持未激活,登录与业务调用均
  返回 12001。首次短信登录完成激活后,该 sid 即永久具备续期资格。
- **GetToken 签发新 token 的条件**:账号当前无活跃 token,或 sid 携带 12002 续期标记;
  否则返回 10000 且响应不带 token(failNum 字段为当日累计错误密码次数,每日 0 点清空,
  与 token 签发无关)。集成侧将"10000 无 token"识别为保持现有会话,不再误判为登录失败。
- **单账号单活跃 token**:各端登录互相顶替,被顶一端由客户端静默重登。集成侧在业务
  返回 12001/12002 时自动走密码续期链,与手机 App 共存语义明确为"后登者顶前者,被顶者自愈"。

### 新增(登录链同步,零人工续期)
- **自主续期登录链**(`async_login_evergreen`):密码 GetToken(携带自有 sid)→ Login 激活
  → 业务恢复,全程两个请求,无需验证码。首次绑定(短信 + GT4 滑块)完成后日常运行不再
  需要任何人工介入。
- **GT4 本地滑块验证**(gt4_helper.py):账号风险态/新终端首登遇 12114 时,配置流程自动
  生成滑块页面,挂在 **HA 自身 HTTP 端口**(容器部署无需额外端口映射):
  - 页面:`GET /api/lechange/gt4/slides`
  - 回传:`POST /api/lechange/gt4/tuple`
  滑块通过后自动完成 CheckGeeTest4 → 重发短信 → 回到验证码输入步骤;不再要求
  "去乐橙 App 完成验证"。
- **AK 身份接口**:CheckGeeTest4 / GetValidCode / GetTokenBySMS 支持 default 前缀 AK
  身份(签名密钥为 SK 单哈希,与密码路径的双哈希区分)。OEM AK/SK 为厂商接入凭据,
  改由环境变量提供(`LECHANGE_OEM_AK` / `LECHANGE_OEM_SK`),不再随源码分发。
- 错误码扩展:`12001`(token 未激活)纳入认证失效码表;新增 `12114`(需 GT4)、
  `12112`(需终端授信)定向处理。

### 修复(终端授权链线上协议校准)
- **GrantingCredit 按线上协议校准**:完整绑定链实测
  (GetToken 12112 → GetValidCode(GrantingCredit) → **GrantingCredit** → GetTokenBySMS),
  确认 `validCode` 直接传**短信验证码原码**、`type` 固定 `"phone"`、`areaCode` 空串,
  **无 CheckValidCode→accessToken 中间步**(旧链实测 15000 的真因)。
- 单测同步:授权链断言改为原码直传 + type=phone + 单次调用。

### 测试
- 单元测试扩展至 **153 例**(新增 17 例:AK 身份与 SK 签名重算、GetToken sid 头透传、
  "10000 无 token"会话保护、12114 上抛、滑块页面占位符/UA 注入、监听器四元组分发)。

## [v1.5.0] - 2026-09-04

### ✨ 媒体接入 v1.5.0 (2026-09-04)
- **云端实时预览**(`record_preview`): RTSV1 取流录制, 默认主码流, H265 优先;
  支持 OSD(时间戳左上 + 通道名右下, 逐秒真实时间, 可关)。
- **门外截图**: camera 快照 + `doorfront_snapshot` 服务; 双摄组合(左右/上下/单摄),
  支持 OSD(默认开), 60s 节流。
- **告警图**: `alarm_image` 服务; DAV 容器解密(安全码) → 原厂 JPEG。
- **本地通道地址**: `channel_hosts`; LAN `snapshot.cgi` 优先, 设备不在网自动回退云端。
- **两套密码**: 配置流采集安全码(必填)+设备密码(可选); 安全码=告警图, 设备密码=流帧。
- **OSD 字体**: 插件内置思源黑体 SC(子集 3.4MB), 截图/预览同字体, 不依赖系统字库。
- **UA 设备特征池**: 19 款混合品牌机型, 按 terminal_id 确定性派生。
- **MQTT 实时通道**: 凭据 `client_v2/auth/get`(apiver 6550) + TLS 8883
  (内置 CA) + `iot_request/iot_response` 往返; **控制 MQTT 优先 → 云 API 兜底**,
  属性推送实时更新实体; 断线自动重试, 不影响轮询。
- **ffmpeg**: 系统二进制依赖, 无 imageio 回退。
- 单测 135 例。

### 修复(会话持续性,2026-09-03 决定性实验)
- **12002 纳入自动重登码表**(`AUTH_FAIL_CODES`):实测服务端为**单 token 策略**——同账号
  任意端 `GetToken` 新登录即作废旧 token(手机 App 登录会踢掉集成会话,反之亦然)。
  此前 12002 不在失效码表内,会话被踢后集成持续空轮询不恢复;现收到 12002 自动重登,
  与手机 App 共存改为"谁登录谁在线,后登者踢前者"的明确语义。
- 单元测试更新:业务错误样例改用 13924(12002 现为认证失效语义);
  重登行为断言 12002→GetToken→重试(2 次重登路径),83 例全通过。

### 修复(终端特征迁移)
- **client-ua 迁移为线上安卓特征**:`clientType=android` 保持,
  `terminalModel/terminalBrand` 由 PC 占位值(HA-Integration-Box/Generic)改为线上客户端值
  (**SM-S921B/samsung**),`clientOV` 对齐 Android 14(34)——phone 型有服务端校验(12112)
  不可模拟,PC 特征终端会话待遇差、易频繁失效,线上安卓特征为可持续方案。
- **terminalId 迁移为 App 同款标准 UUID**(大写带连字符):coordinator 启动时检测旧
  `lechange-hass-*` 格式一次性升级并持久化,保持"一台终端一份 ID"语义(不膨胀终端管理列表)。
- 单元测试新增 client-ua 终端特征组(83 例,含签名串参与性断言)。
- 实测:线上 UA 登录 + 30s 只读心跳,
  user.account.Login / device.list.DeviceBasicInfoQueryV2 / cloud.message.GetDeviceAlarmMixMessage
  三路全 10000;循环模式统计会话存活时长,会话失效自动重登。

## [1.4.0] - 2026-09-03

### 依据(本地分析,不随仓库分发)
- 客户端接口清单与运行时行为分析;流量样本 103 条记录/32 API。

### 新增(短信验证码登录)
- 配置向导支持**两种登录方式**:**账号密码** / **短信验证码**
  (`user.account.GetTokenBySMS` {account, areaCode, validCode},响应与 GetToken 同构,
  登录后同一套密钥切换 md5(token)/sha256(token))。
- 短信流程:输入账号 → 提示在乐橙 App 获取验证码(集成 best-effort 尝试自动发送
  `common.validcode.GetValidCode` {type:"phone", usage:"SMSLogin"},未登录发送实测被
  服务端风控拦截(11010)→ 引导 App 获取)→ 输入 6 位验证码登录。
- **密码登录失败定向提示**:错误码映射 `sms_needed`(2026/2032/2016 需短信/两步验证,
  引导改用短信登录)与 `captcha_needed`(2033/2036/11006/11007/11012/12000 风控)。
- **人机验证(极验 GT4)**:若连续输错密码或触发了风控，登录会失败（需在 乐橙 App  中完成验证后再试）。

### 修复(按实测验证)
- **临时密码改走真实 App 云消息 API**:`iot.message.SmartLockSecretAdd`(生成)/
  `SmartLockSecretListV2`(分组列表)/新增 **`delete_snapkey`**(`SmartLockSecretDelete`)。
  - **不使用 `iot.control.SetService`(CreateDeviceSnapkey)生成**——老接口及前置身份验证
    (GetValidCode/CheckValidCode)会触发短信验证码;改为客户端自产 keyId/tempKey
    (8 位数字,keyId 随机,createTime/expiredTime 真实 epoch)直接登记,
    **设备休眠可用、不触发验证码**(实测 10000)。
  - `usagePeriod` 按 `星期位掩码-起T0000Z-止T2359Z` 格式构建。
- **消息域 apiver 分域(R13)**:`iot.message.*`/临时密码 = **`V10.2.2` + `charset=UTF-8`**,
  已实测可用;用户/设备域保持 `191204` + `utf-8`。
- **云侧告警接入**:`cloud.message.GetDeviceAlarmMixMessage`(实测 payload,设备休眠
  照常返回)→ 每轮询检测新告警触发 `lechange_door_lock_event`(`type: alarm`),
  新增「最新告警」传感器;首轮只建基线不刷历史。
- 单设备详情改用 `device.info.BasicInfoGet`(实测),失败回退列表接口。

### 其他
- **「唤醒设备」改为「获取门外截图」**:原 SetProperties 唤醒对休眠设备无效;
  改为拉取摄像头(门铃常在线)唤醒锁体 + Dahua CGI 抓拍,快照保存到 `www/lechange/`
  并经 `lechange_door_lock_event`(`type: snapshot`,含 `/local/...` URL)返回。
- 通道在线传感器 unique_id 修复;snapkey 操作后实体刷新;SetProperties 枚举值对齐。
- 测试扩展至 **69 例**(usagePeriod/位掩码、SMS 登录、消息域 apiver/Content-Type 断言、
  删除密钥 payload)。

## [1.3.0] - 2026-09-03

### 新增(移植自社区分支持续改进,基于客户端私有云 API)
- **临时密码实体化配置**:number(使用次数/有效天数)、select(生效星期:每天/工作日/周末/单日)、
  text(名称)、time(开始/结束时间)4 类配置实体,配置持久化到集成选项。
- 按钮:按配置**生成临时密码**、**刷新临时密码列表**;传感器:临时密码数量、
  **最近开门时间/方式**(lockNoteReport.keyType)。
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
- 临时密码与开门记录事件(lockNoteReport 轮询对比)。

### 修复
- 登录后请求的 `token/内部用户名` 未解析导致的会话失效与重登死锁(单元测试发现)。
- `lc_api.py` 登录后密钥派生(md5/sha256(token)),修正 12002 误报。

### 质量与合规
- 单元测试套件 50 例(签名/型号解码/状态推导/RTSP 拼装,匿名化夹具,不依赖网络与私有数据)。
- CI:`.github/workflows/hacs.yml`(HACS Action 仓库校验 9 项 + pytest)。
- 发布 CI:`.github/workflows/release.yml`(tag v* → ZIP 安装包,说明自动取自本文件)。
- 新增 MIT LICENSE、README 免责声明、配置向导风险提示。
- 统一更新日志(CHANGELOG.md);`.gitignore` 隔离敏感目录;公开文件脱敏。

> v1.1.0 为开发中间版本(未发布),其内容已并入本版本,不再单独记录。

## [1.0.1] - 2026-03-11

- 添加开门记录(旧开放平台接口版本)。

## [1.0.0] - 2026-03-10

- 首次发布(旧开放平台接口版本:设备状态、电量、远程开门、唤醒、临时密码)。

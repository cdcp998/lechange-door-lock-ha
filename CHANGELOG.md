# 更新日志 / Changelog

本文件为统一更新日志(Keep a Changelog 风格)。发布版本时,CI 会自动从本文件提取
对应版本的章节作为 GitHub Release 说明,并附带组件 ZIP 安装包。

版本号与 `custom_components/lechange_door_lock/manifest.json` 保持一致。

> 规则:**版本号只在发布时确定**。日常新功能/修复统一合并进当前未发布版本条目,
> 发布(tag v*)时再固化 — 不刷版本号。

---

## [1.4.0] - 2026-09-03

### 依据(新分析文件,仅存本地不公开)
- 热更接口分析(运行时 JS bundle):`hotupdate_api_list.json` / `hotupdate_api_usage.txt` /
  `热更接口功能与用法.md` / `接口测试报告.md`(R1-R15 避雷清单与可信度分级 A/B/C)/
  `门锁功能全分析.md`;抓包证据 `capture/flow.jsonl`(103 条记录/32 API)+ 复现 addon。

### 新增(短信验证码登录)
- 配置向导支持**两种登录方式**:**账号密码** / **短信验证码**(App 源码取证:
  `user.account.GetTokenBySMS` {account, areaCode, validCode},响应与 GetToken 同构,
  登录后同一套密钥切换 md5(token)/sha256(token))。
- 短信流程:输入账号 → 提示在乐橙 App 获取验证码(集成 best-effort 尝试自动发送
  `common.validcode.GetValidCode` {type:"phone", usage:"SMSLogin"},未登录发送实测被
  服务端风控拦截(11010)→ 引导 App 获取)→ 输入 6 位验证码登录。
- **密码登录失败定向提示**:错误码映射 `sms_needed`(2026/2032/2016 需短信/两步验证,
  引导改用短信登录)与 `captcha_needed`(2033/2036/11006/11007/11012/12000 风控)。
- **人机验证(极验 GT4)**:若连续输错密码或触发了风控，登录会失败（需在 乐橙 App  中完成验证后再试）。

### 修复(按抓包/测试报告验证)
- **临时密码改走真实 App 云消息 API**:`iot.message.SmartLockSecretAdd`(生成)/
  `SmartLockSecretListV2`(分组列表)/新增 **`delete_snapkey`**(`SmartLockSecretDelete`)。
  - **不使用 `iot.control.SetService`(CreateDeviceSnapkey)生成**——老接口及前置身份验证
    (GetValidCode/CheckValidCode)会触发短信验证码;改为客户端自产 keyId/tempKey
    (8 位数字,keyId 随机,createTime/expiredTime 真实 epoch)直接登记,
    **设备休眠可用、不触发验证码**(实测 10000)。
  - `usagePeriod` 按抓包格式 `星期位掩码-起T0000Z-止T2359Z` 构建。
- **消息域 apiver 分域(R13)**:`iot.message.*`/临时密码 = **`V10.2.2` + `charset=UTF-8`**,
  已实测可用;用户/设备域保持 `191204` + `utf-8`。
- **云侧告警接入**:`cloud.message.GetDeviceAlarmMixMessage`(抓包验证 payload,设备休眠
  照常返回)→ 每轮询检测新告警触发 `lechange_door_lock_event`(`type: alarm`),
  新增「最新告警」传感器;首轮只建基线不刷历史。
- 单设备详情改用 `device.info.BasicInfoGet`(抓包验证),失败回退列表接口。

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

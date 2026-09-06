# 避雷指南 —— 乐橙门锁临时密码生成/删除踩坑汇总

> 本文档记录 2026-09-06 逆向 + 真机实测的全部坑位与结论，供后续维护/新开发避雷。
> **原则：只信真机证据（抓包 + 实测响应），不信静态 DEX/假设。**

## 0. 一句话核心

**生成走两步式（设备域 `CreateDeviceSnapkey` 签发 → 消息域 `Add` 上报），删除走设备域（`sdl_standardDelKey` 211400）。设备域是权威（服务端"入口=认可"），消息域只是登记层。**

---

## 1. 坑位清单（按严重程度）

### 坑 1：一步式生成 → 服务端不认可 → 被清理 ★★★（本轮的"病根"）

| 现象 | 原因 |
|---|---|
| HA 生成的临时密码 App 里删不掉 / 被 App 生成顶掉 | **一步式（客户端自产 keyId 直接 `SmartLockSecretAdd`）→ 设备数据库不知晓 → state=0（未认可）→ 设备同步时服务端清理不匹配记录** |
| 用户实测：HA 生成→App 生成触发同步→**HA 密码被删** | 服务端对"未认可"记录（设备侧无）先清后显 |

**真相**：App 生成 = **两步式**（bundle `addTempKeyToSass` @11739016）：
```js
setService({ service: CreateDeviceSnapkey, inputData }).then(e => {
  // 出参 key/keyID（服务端签发）→ addKey（消息域上报）
})
```
**① 设备域签发（设备知晓——服务端认可）→ ② 消息域登记。**
HA 若用一步式（客户端自产 keyId 直接 Add）→ **设备不知晓 → 不认可**。

**避雷**：生成**必须两步式**（`SetService CreateDeviceSnapkey` → `Add`），服务端签发 key/ID（`mode="server"`）；`mode="client"` 是回落（未认可，仅应急）。

---

### 坑 2：删除走消息域 → 12100 / 500 ★★★

| 现象 | 原因 |
|---|---|
| `SmartLockSecretDelete` 返回 `12100 no operate permission` | 服务端对 state=0 未确认草稿（设备数据库无此 keyId）拒绝——消息域删除是"请求式"，需设备上下文匹配 |
| App 显示"网络错误" | 服务端对**非数字 keyId（如 "abc"）**返回 **HTTP 500 空 body**（内部异常，无业务码）——App 无法解析 → 统一"网络错误"文案 |
| HA 日志 `Imou API error 500` | 同 500（`_http_post` 对非 200 抛 ImouAPIError） |

**真相**：App 删除链（bundle `z()` @11842460）：
```js
setService({ service: sdl_standardDelKey, inputData: { 211401: [{ type, name, keyID }] } })
  .then(() => deleteKey(条目对象))   // 消息域
```
**① 设备域 `sdl_standardDelKey`（211400）删设备密钥（按设备数据库）→ ② 消息域删登记。**
设备域删除**权限最大**——实测对 `keyId="abc"`（消息域 500/12100 的记录）**返回 10000 + ListV2 立即移除（total=0）**；对 state=0/1 新记录同样 10000。

**避雷**：删除**优先设备域** `SetService sdl_standardDelKey`（211400，ref：delKeys 211401 / type 211441 / name 211442 / keyID 211443 / groupName 211444 / groupID 211445）；消息域 `SmartLockSecretDelete` 仅兜底。**keyID 非数字/超范围 → -1**（设备域对 -1+name 匹配可删）。

---

### 坑 3：服务端对畸形 keyId 处理异常 ★★

| 现象 | 原因 |
|---|---|
| 发 `keyId:"abc"`（非数字）→ 消息域 Delete 返回 **HTTP 500**（无业务码） | 服务端对非数字 keyId 解析失败 → 内部异常 500；用客户端自产 keyId 时**必须纯数字**（秘密字串/校验） |
| `SmartLockSecretAdd` 传 `keyId:"abc"` 竟返回 10000（**服务端宽松**） | Add 不校验 keyId 格式，**但**畸形记录（非 int）从此**任何消息域删除都 500**——**生成时绝不能用非数字 keyId** |

**避雷**：keyId/tempKey 一律**纯数字**（`secrets.randbelow` 而非 `random`——CSPRNG，禁 `random`/`randint` 生成密码类值——可预测）；生成前校验 keyId 纯数字。

---

### 坑 4：`random.randint(10000000,99999999)` 全段 keyId ★

| 现象 | 原因 |
|---|---|
| v1.6.5 用 `random.randint(10000000,99999999)` 生成 keyId → 部分记录（23M/79M 段）删除 12100 | 服务端只认可 **App 段 [67000000,68999999]**（App 抓包 keyId=67157501 实证）；旧全段含服务端不认可的段 |
| 误判"keyId 段 = 删除权限" | **实际不是段**（68M 段记录 68170052 也 12100——因 state=0 未确认/伪记录）；**是"设备是否知晓"（state）与"是否数字"** |

**避雷**：keyId 用 **App 段（`secrets.randbelow(2000000)+67000000`）**；但**更根本**——生成走两步式（服务端签发），客户端 keyId 仅回落用。

---

### 坑 5：`createTime/expiredTime` 传空/非 epoch → 13924 ★

| 现象 | 原因 |
|---|---|
| Add 返回 13924（参数/数据） | `createTime/expiredTime` 必须**真实 epoch 秒字符串**（空/缺省 → 13924，早期所有"失败"真因） |

**避雷**：`createTime=now`、`expiredTime=now+86400*effect_days`（str(int)）。

---

### 坑 6：`usagePeriod` 不完整 → "127-" 落库残留 ★

| 现象 | 原因 |
|---|---|
| Add 后 App 列表/删除按完整 usagePeriod 匹配失败（残留） | `usagePeriod` 缺省落库 `"127-"`（未拼两段日期）——App 用 11 字段全量匹配删除，usagePeriod 不符 → 删不掉 |

**避雷**：`usagePeriod` 必须完整：`"127-<今日>YYYYMMDDT0000Z-<今日+days>YYYYMMDDT2359Z"`（7-bit 周掩码 127=每天；时间 "0HH:MM:SS" 截前 5 位）。

---

### 坑 7：MQTT 通道 —— 私有、不稳定、非官方 ★★

| 坑 | 说明 |
|---|---|
| MQTT 无官方出处 | 官方文档（11+ 页子代理实测）**从未公开 MQTT**；官方=HTTP Webhook + pullMessages |
| MQTT 写不可靠 | 9-03 实测可读（GetProperties 10000）；9-05 连接失败（0 字节断开，可能 token 差异）；**MQTT 只作读/推送通道（默认关），写一律 HTTP** |
| MQTT 无消息域删除 | **枚举实测**：`iot.message.SmartLockSecretDelete` MQTT → `11016 Wrong uri[/pcs/v2/...]`；`iot.smart.SetService` → 11001 参数格式错（MQTT 的 v2 参数体系不同）——**消息域删除 MQTT 不存在** |

**避雷**：MQTT 仅读；写（生成/删除/控制）全走 HTTP 云 API；不要试 MQTT 写（浪费时间）。

---

### 坑 8：设备状态误判 ★

| 坑 | 说明 |
|---|---|
| 认为"删除失败=设备离线" | **错**——设备 `sleep` 时设备域 SetService 删除**仍 10000**（云端处理，不依赖在线）；064821 抓包设备 online 但删除仍 500（畸形 keyId）——**删除失败与设备状态无关** |
| 认为"state=0 草稿删除返回 10000 但列表不移除" | 部分成立（消息域），但**设备域可直接删**（include state=0）——**结论：用设备域** |

**避雷**：设备状态不影响删除成败；但**生成**（CreateDeviceSnapkey 设备面）**需要设备在线**（sleep → 10003 回落 state=0，未认可）——生成前 `ensure_awake`（取流唤醒）。

---

### 坑 9：授信终端/权限 ≠ 删除权限 ★

| 现象 | 结论 |
|---|---|
| 试图"升级授信终端"解决 12100 | **错**——已授信终端（43a08627，R32 证实）删畸形/state=0 记录**仍 12100**——**删除失败与终端授权无关**，是服务端对记录的校验（消息域） |
| GrantingCredit 提交 11001 | 需「待授信会话」（alert=true && state=false，R22）——不齐则 11001（与参数无关） |
| "PC 接口权限低" | 终端凭证影响登录（12114 GT4 风控），但不决定消息域删除（高权限终端仍 12100）——**消息域删除是记录层校验** |

**避雷**：不要走"授信/权限"路线解决消息域删除——**正确姿势=设备域 SetService**。

---

### 坑 10：UI/日志误导 ★

| 现象 | 真相 |
|---|---|
| App "网络错误" | 服务器非 2xx/无业务码（500 空 body）→ App 统一文案（bundle Post() @360417：12100 只 console.log，msg/desc 替换为"操作失败,请检查网络"）——**别信字面，看抓包/日志** |
| HA "not in device list" | `async_delete_snapkey` 旧逻辑 `async_request_refresh` 不更新 snapkey_list（主轮询刷快照/属性）——**已修**（找不到记录时直接 `async_smart_lock_secret_list` 拉一次） |
| HA `expected int at key_id` | schema 强制 int——畸形记录（"abc"）被拒——**已改回 int**（畸形记录走设备域删除，不通过 HA schema） |

**避雷**：排查时看真实响应（抓包/API code），不要被 UI 文案/表层错误误导。

---

## 2. 关键验证结论（本轮可信）

| 项 | 结论 |
|---|---|
| App 生成 | **两步式**：`SetService(CreateDeviceSnapkey)` 签发 key/ID → `addKey` 上报（bundle @11739016） |
| App 删除 | **两步式**：`SetService(sdl_standardDelKey 211400)` 删设备 → `deleteKey`（消息域）删登记（bundle @11842460） |
| 设备域删除 | `sdl_standardDelKey`（211400）+ `{211401:[{211441:type,211442:name,211443:keyID}]}` → **10000**（服务端认可，ListV2 移除）——**对 state=0/1/畸形全适用** |
| 服务端"入口=认可" | 设备域（SetService）是权威——设备知晓=认可；消息域（Add/Delete）只是登记层 |
| `createTime/expiredTime` | 必须真实 epoch 秒（空 → 13924） |
| keyId | 纯数字（App 段 [67000000,68999999] 回落用）；服务端签发（两步式）优先 |

## 3. 官方文档 vs 实际（逆向产物，非官方）

> 本集成是**逆向工程的产物**（App 私有 API + 热更 JS bundle），**非乐橙开放平台官方 API**。
> - 官方开放平台文档**不覆盖**这些接口（MQTT 无官方出处、消息域/设备域是 App 私有）
> - **以真机抓包 + 实测响应为准**；静态 DEX/bundle 只作线索（App 热更 JS 可能与旧 DEX 不同）

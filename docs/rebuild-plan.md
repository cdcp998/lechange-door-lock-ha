# 重建计划 —— 基于真机验证证据（最终版）

> 本文件记录 2026-09-06 乐橙门锁集成临时密码功能的重建结论（真机抓包 + 实测铁证）。
> **原则：只用真机验证过的链路，废弃验证失败的。**
> 踩坑详细清单见 [avoid-pitfalls.md](avoid-pitfalls.md)。

## 1. 已验证结论（证据）

### 1.1 临时密码生成 —— 两步式（App 同款，服务端认可）
| 证据 | 说明 |
|---|---|
| App 热更 bundle `addTempKeyToSass` @11739016 | **App 生成 = 两步式**：`setService({service: CreateDeviceSnapkey, inputData}).then(e => addKey(...))` —— ①设备域签发 key/keyID → ②消息域上报 |
| 用户实测「HA 生成→App 生成」序列 | **HA 一步式（客户端自产 keyId 直接 Add）的密码被服务端"顶掉"**（state=0 未认可 → 设备同步清理）——**"入口=认可"机制** |

**结论**：生成必须**两步式**（`SetService CreateDeviceSnapkey` ref 26300 → `SmartLockSecretAdd` 上报）。服务端签发 key/ID（`mode="server"`）；设备休眠 10003 时回落客户端自产（`mode="client"`，未认可，仅应急）。

### 1.2 临时密码删除 —— 设备域优先（App 同款，最高权限）
| 证据 | 说明 |
|---|---|
| App 热更 bundle `z()` @11842460 | **App 删除 = 两步式**：`setService({service: sdl_standardDelKey, inputData:{211401:[{type,name,keyID}]}}).then(()=>deleteKey(条目对象))` —— ①设备域删设备密钥 → ②消息域删登记 |
| 实测 `SetService sdl_standardDelKey`（211400） | 对消息域 12100/500 的记录（`keyId="abc"` 畸形、state=0 草稿、state=1 正常）**全返回 10000 + ListV2 立即移除（total=0）**——设备域按设备数据库删，**权限最大** |

**结论**：删除优先**设备域** `sdl_standardDelKey`（211400；ref：delKeys 211401 / type 211441 / name 211442 / keyID 211443 / groupName 211444 / groupID 211445；keyID 非数字/超范围 → -1）。消息域 `SmartLockSecretDelete` 仅兜底。

### 1.3 退化的旧结论（已废弃）
| 旧结论 | 为什么废弃 |
|---|---|
| MQTT 写通道（曾经"可行"） | 官方无出处（官方=HTTP）；MQTT 私有、不稳定（9-05 失败）；**写一律 HTTP**（MQTT 仅读/推送，默认关） |
| 一步式生成（客户端 keyId 直接 Add） | 服务端不认可（state=0 未确认）→ 被清理——**必须两步式** |
| 消息域删除（SmartLockSecretDelete） | 对 state=0/畸形 12100/500——**设备域才可靠** |

## 2. 关键 ref 编码（QueryModelInfo 真机提取，型号 SKG8J5RP）

| 标识符 | ref | 用途 |
|---|---|---|
| CreateDeviceSnapkey | 26300 | 两步式生成（服务端签发 key/keyID） |
| sdl_standardDelKey | 211400 | 设备域批量删除密钥（211401 delKeys / 211441 type / 211442 name / 211443 keyID / 211444 groupName / 211445 groupID） |
| remoteOpenDoor | 26600 | 远程开门 |
| child_lock | 120000 | 童锁 |
| Dormant / sleepStatus | 100300 / 108200 | 唤醒/休眠探测 |
| doorLockStatus | 102800 | 门锁状态 |
| powerMode | 105200 | 工作模式 |

## 3. 临时密码关键字段（App 抓包 + 实测）

| 字段 | 要求 | 坑 |
|---|---|---|
| keyId | 纯数字；两步式时服务端签发；回落用 App 段 `secrets.randbelow(2000000)+67000000` | `random` 可预测（禁）；非数字畸形记录 → 消息域删除 500 |
| tempKey | 8 位数字（`secrets` —— CSPRNG） | `random`（MT）可预测 |
| createTime / expiredTime | **真实 epoch 秒字符串**（`str(int(time.time()))`） | 空 → 13924（早期所有失败真因） |
| usagePeriod | 完整：`"127-<今日>YYYYMMDDT0000Z-<今日+days>YYYYMMDDT2359Z"` | 缺省落库 "127-" → 匹配失败残留 |
| type | 3（临时密钥） | — |

## 4. 完成的重建步骤（✅）

- [x] **生成**：两步式（`CreateDeviceSnapkey` → `Add`），`ensure_awake` 保障，`secrets` 纯数字
- [x] **删除**：设备域 `sdl_standardDelKey`（211400）优先 → 消息域兜底
- [x] **MQTT 写废弃**（写全 HTTP；MQTT 仅读推送、默认关）
- [x] **`delete_snapkey` 找不到记录时直接拉列表**（旧 refresh 不更新 snapkey_list）
- [x] **属性读取走信息域**（BasicInfoGetV2/GetDeviceDetailInfo，休眠可读）
- [x] 测试通过（288）

## 5. 后续（可选）

- 跨型号验证：`sdl_standardDelKey` ref 经 `QueryModelInfo`/`service_ref` 动态解析（当前按 SKG8J5RP 实测 ref 硬编码 + 动态兜底）
- 唤醒链路：`ensure_awake` 用取流 URL（async_get_transfer_stream_url）；SetProperties 唤醒已废弃（40999）
- 童锁：与唤醒同方向（信息域/云消息，非 SetProperties 写）

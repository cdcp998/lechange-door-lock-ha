## LeChange Door Lock 乐橙门锁 Home Assistant 集成

### 📌 项目简介
本项目是一个 Home Assistant 自定义集成，用于连接和控制乐橙（LeChange）智能门锁设备。通过乐橙开放平台 API，您可以在 Home Assistant 中实时监控门锁状态、远程开门、唤醒设备、生成临时密码等。（找不现成，用纯AI写的代码，大佬们喷轻点）

### ✨ 功能特性
- **设备状态监控**：自动获取门锁在线状态（含子通道）、电池电量（百分比）。
- **远程控制**：通过按钮实体远程开门、唤醒休眠设备。
- **临时密码管理**：生成一次性或周期性临时密码，获取已创建的密码列表（通过服务调用）。
- **自动令牌刷新**：自动管理 Access Token，在过期前自动刷新，无需手动干预。
- **设备信息同步**：自动同步设备型号和固件版本到设备注册表。
- **国际化支持**：内置中文、英文界面翻译，实体名称自动适配系统语言。

### 📦 安装方法
#### 方式一：HACS 安装（推荐）
1. 确保已安装 [HACS](https://hacs.xyz/)。
2. 在 HACS 中点击“自定义存储库”，添加此仓库 URL，类别选择“集成”。
3. 搜索并安装 `LeChange Door Lock`。
4. 重启 Home Assistant。

#### 方式二：手动安装
1. 下载 [最新发布包](https://github.com/cdcp998/lechange-door-lock-ha/releases/latest) 并解压。
2. 将文件夹 `lechange_door_lock` 复制到 Home Assistant 的 `custom_components` 目录下。
3. 重启 Home Assistant。

### 🔧 配置说明
1. 在 Home Assistant 中进入 **配置 → 设备与服务 → 添加集成**。
2. 搜索 `LeChange Door Lock` 并点击。
3. 输入您在乐橙开放平台获取的 **App ID** 和 **App Secret**。
4. 点击“提交”，系统会自动验证并列出您账号下的所有门锁设备。
5. 选择要添加的设备，完成配置。

> **注意**：Access Token 将自动获取并刷新，无需手动输入。

### 🖥️ 使用说明
#### 实体列表
成功添加后，每个设备会生成以下实体：
| 实体类型 | 功能 | 实体ID示例 |
|----------|------|------------|
| 传感器 | 电池电量 | `sensor.r10_max_4812_battery` |
| 二进制传感器 | 设备在线状态 | `binary_sensor.r10_max_4812_online` |
| 二进制传感器 | 各通道在线状态（如存在） | `binary_sensor.r10_max_4812_ch_1_online` |
| 按钮 | 远程开门 | `button.r10_max_4812_open_door` |
| 按钮 | 唤醒设备 | `button.r10_max_4812_wake_up` |

#### 支持的服务
您可以在自动化或脚本中调用以下服务：
| 服务 | 描述 | 所需参数 |
|------|------|----------|
| `lechange_door_lock.generate_snapkey` | 生成临时密码 | `device_id`, `name`, `effective_num`, `effective_day`, `effect_period`, `begin_time`, `end_time` |
| `lechange_door_lock.get_snapkey_list` | 获取临时密码列表 | `device_id` |
| `lechange_door_lock.open_door_remote` | 远程开门 | `device_id` |
| `lechange_door_lock.wake_up_device` | 唤醒设备 | `device_id` |

#### 自动化示例
```yaml
# 每天8点生成一个有效期为1天的临时密码
automation:
  - alias: "Generate daily temporary password"
    trigger:
      platform: time
      at: "08:00:00"
    action:
      service: lechange_door_lock.generate_snapkey
      data:
        device_id: "600EBBDRSF84812"
        name: "Daily Guest"
        effective_num: 5
        effective_day: 1
        effect_period:
          - period: "Monday"
            beginTime: "08:00:00"
            endTime: "20:00:00"
        begin_time: "08:00:00"
        end_time: "20:00:00"
```

### ⚠️ 注意事项
- **权限要求**：确保您的乐橙开放平台应用已开启 **Config** 权限，否则门锁操作会失败。
- **设备休眠**：门锁为省电会进入休眠状态，此时在线状态可能显示“断开”。唤醒按钮可主动唤醒设备，唤醒后 10 秒会自动刷新状态。
- **API 调用限制**：免费套餐有调用次数限制，本集成已优化为 1 分钟更新一次状态，并每天自动刷新令牌，请合理使用。
- **多设备支持**：每个门锁需单独添加，设备 ID 在乐橙 App 或开放平台中可查。

### 📜 更新日志
#### v1.0.0 (2026-03-10)
- 首次发布。
- 支持设备在线状态、电量监控。
- 支持远程开门、唤醒设备按钮。
- 支持生成临时密码、获取密码列表服务。
- 自动令牌刷新与设备信息同步。
- 完整中英文国际化。

### 🐛 问题反馈
如果您在使用中遇到任何问题，或有功能建议，请在此仓库的 [Issues](https://github.com/cdcp998/lechange-door-lock-ha/issues) 页面反馈。

---

**感谢使用！**

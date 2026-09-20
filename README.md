# FxxkPush v0.1

自建 AI 消息分诊推送服务：PC 上的 AI 判断"哪些通知值得打扰手机"，重要的经 ntfy 推到手机/手表，垃圾静默归档。

## 架构

```
VPS 日志（OOM/磁盘>90%/SSH 登录/服务挂掉）
  ├─ 硬规则 → ntfy(fp-vps) ──────────────┐
  └─ 灰区事件 → ntfy(fp-gray) ───────────┤
                                         ├→ PC: ai_triager (MiMo v2.5)
Windows toast 通知（QQ/钉钉/学习通等）    │     ├─ 重要 → ntfy(fp-phone) → 手机/手表
  └─ notification_listener → ntfy(fp-pc)┘     └─ 忽略 → 静默归档 JSONL
```

- **VPS（1C1G）**：传感器 + 中继。ntfy 常驻（推送通道永远在线），日志硬规则判级（grep 级零内存），不跑 AI
- **PC**：大脑 + 采集器。Windows 通知监听、AI 分诊（OpenAI 兼容 API）、结果推送
- **手机**：ntfy 客户端接收 + Wear OS 手表震动，腾讯系 App 后台全杀

## 组件

| 文件 | 运行位置 | 作用 |
|---|---|---|
| `deploy/vps_monitor.py` | VPS (systemd) | journalctl 采集 + 硬规则判级 + 灰区落盘，60s 巡检，冷却去重 |
| `deploy/ntfy-server.yml` | VPS | ntfy 服务端配置（token 鉴权，deny-all） |
| `pc/notification_listener.py` | PC (自启) | UserNotificationListener 抓 toast → fp-pc + JSONL 归档 |
| `pc/pc_subscriber.py` | PC (自启) | 订阅 fp-vps/fp-gray，弹 Windows toast |
| `pc/ai_triager.py` | PC (自启) | 消费 fp-pc/fp-gray/fp-vps → MiMo 分诊 → fp-phone / 静默 |
| `pc/probe_notifications.py` | PC | 通知权限探测/诊断工具 |
| `vps_exec.py` | PC | SSH 执行助手（密码走 `vps.secret`，不入库） |

## 快速上手

前提：一台有公网 IP 的 Linux VPS + 一台 Windows PC（Win10/11）+ 手机装 ntfy 客户端。

### 1. VPS：装 ntfy + 部署监控

```bash
# 安装 ntfy（apt 源或 GitHub release 二进制均可）
apt install ntfy        # 或下载二进制到 /usr/local/bin

# 应用服务端配置（按需改 listen 端口、base-url）
cp deploy/ntfy-server.yml /etc/ntfy/server.yml

# 创建管理员用户并签发 token（deploy-all 默认拒绝匿名）
NTFY_PASSWORD=你的密码 ntfy user add --role admin omo
ntfy token add omo        # 输出 tk_xxx，PC 侧要用

# 启动并设置开机自启
systemctl enable --now ntfy
curl http://127.0.0.1:2586/v1/health   # 返回 200 即成功

# 部署日志监控
mkdir -p /opt/fuckpush
cp deploy/vps_monitor.py /opt/fuckpush/
# 创建 systemd unit（参考内容如下）
```

`/etc/systemd/system/fuckpush-monitor.service`：

```ini
[Unit]
Description=FuckPush VPS monitor
After=network.target ntfy.service

[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/fuckpush/vps_monitor.py
Environment=FP_NTFY_TOKEN=tk_你的token
Environment=FP_NTFY_URL=http://127.0.0.1:2586/
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload && systemctl enable --now fuckpush-monitor
```

### 2. 手机：安装 ntfy 客户端

1. 安装 ntfy（Play Store / F-Droid / App Store）
2. 添加订阅：服务器填 `http://你的VPS-IP:2586`，话题填 `fp-phone`
3. 在订阅设置里填账号密码（步骤 1 创建的用户）并开启高优先级 + 震动

### 3. PC：装依赖 + 配置

```powershell
# Python 3.12（pywinrt 3.2.1 不支持 3.14）
uv venv .venv --python 3.12
uv pip install --python .venv httpx winotify winrt-runtime `
  winrt-Windows.UI.Notifications.Management winrt-Windows.UI.Notifications `
  winrt-Windows.Foundation winrt-Windows.ApplicationModel winrt-Windows.Foundation.Collections

# 首次授权：允许读取 Windows 通知（弹窗里点允许）
.venv\Scripts\python.exe pc\probe_notifications.py
```

配置三个本地文件（都不入库）：

```
pc/ntfy.secret          # 一行：ntfy token（tk_xxx）
pc/triage_config.json   # AI 配置，示例：
{
  "provider": "mimo",
  "base_url": "https://api.xiaomimimo.com/v1",
  "api_key": "sk-你的key",
  "model": "mimo-v2.5",
  "dedup_window_sec": 1800,
  "max_tokens": 200
}
```

### 4. PC：启动与自启

```powershell
# 手动启动（三个都要）
.venv\Scripts\pythonw.exe pc\pc_subscriber.py
.venv\Scripts\pythonw.exe pc\notification_listener.py
.venv\Scripts\pythonw.exe pc\ai_triager.py

# 或注册登录自启（管理员运行一次）
pc\install_autostart.bat
```

### 5. 验证

- VPS 停一个被监控的服务（或等真实故障）→ PC 弹 toast、手机收到告警
- PC 上让 QQ 好友发条消息 → `pc/notifications.jsonl` 出现记录；AI 判重要则手机响
- 任意渠道发含 `text` 的消息 → 无条件直推手机（测试通道）

## 分诊规则

- **硬规则层**（VPS 本地，零内存零延迟）：进程 OOM、磁盘 >90%、root SSH 登录、监控单元 failed → 直接推送
- **AI 层**（PC）：灰色地带判断（新错误模式、频率异常、消息重要性），输出 `{"label": "重要"|"忽略", "reason": "..."}`；30 分钟滑动窗口去重（同类事件只报一次+计数）；AI 调用失败时默认按重要推送（宁滥勿缺）
- **测试通道**：内容含 `text` 的消息绕过 AI 直推手机

## 配置文件（不入库，自建）

| 文件 | 内容 |
|---|---|
| `vps.secret` | VPS SSH 密码（`host port user password` 四段） |
| `pc/ntfy.secret` | ntfy token（一行） |
| `pc/triage_config.json` | AI API 地址/密钥/模型 |

## 已知边界

- **微信 4.x / 企业微信 PC 端暂不支持** 🔧 正在修复中：两者的通知为应用自绘、不走 Windows 通知中心，UIA 控件树为黑盒。修复方向：手机通知镜像转发 / 右下角浮层 OCR / 桌面窗口持续读取，后续版本加入
- pywinrt 3.2.1 需要 Python ≤3.12（3.14 报 cannot create instances）
- 本机若有 TUN 代理（v2rayN/sing-box），SSH 直连端口可能被截，注意换端口
- ReviOS 精简版需验证通知中心可用（本项目环境已验证）

## Roadmap

- [ ] 每日日报（AI 汇总当天事件/误判，22:00 推手机）
- [ ] 微信 / 企业微信采集支持（见已知边界）
- [ ] 误判样本积累 → 未来 27B 模型微调
- [ ] WebSocket 订阅 + PWA 界面
- [ ] Go 重写（性能瓶颈出现后）

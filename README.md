# FxxkPush v0.2.1

自建 AI 消息分诊推送服务：PC 上的 AI 判断"哪些通知值得打扰手机"，重要的经 ntfy 推到手机/手表，垃圾静默归档。

**核心能力：手机可以放心关闭微信、QQ、企业微信等后台**——所有需要你知道的消息，由 PC 端 AI 分诊后主动推到手机；垃圾消息静默归档，不打扰。同时 VPS 的异常（宕机前兆、入侵、磁盘告急）也会第一时间推到你手上。

## 这解决了什么问题

- 手机关掉微信/QQ 后台 → 省电省内存，再也不用频繁清后台
- 但怕错过重要消息？→ PC 替你值班：家人联系、@所有人、@我、日程提醒、账户安全、服务器异常……AI 判重要的才推手机
- 群聊灌水、营销推送、转账回执、学校新闻 → 全部静默，手机一天安静
- VPS 半夜磁盘爆了 / 被入侵 → 手机立刻收到告警，不用早上起来看服务器才发现

## 架构

```
VPS 日志（OOM/磁盘>90%/SSH 登录/服务挂掉）
  ├─ 硬规则 → ntfy(fp-vps) ──────────────┐
  └─ 灰区事件 → ntfy(fp-gray) ───────────┤
                                         ├→ PC: ai_triager (MiMo v2.5)
Windows toast 通知（QQ/钉钉/学习通等）    │     ├─ 重要 → ntfy(fp-phone) → 手机/手表
  └─ notification_listener → ntfy(fp-pc)┘     └─ 忽略 → 静默归档 JSONL
                                         │
微信 + 企业微信（自绘UI，无系统通知）      │
  └─ wechat_vision_listener ─────────────┘
     （窗口藏屏外 + 定时截图 → MiMo 视觉分诊）

PC 侧所有服务 → http://127.0.0.1:2586 → (pc/ntfy_tunnel.py 走 SSH) → VPS ntfy:2586
```

- **VPS（1C1G）**：传感器 + 中继。ntfy 常驻（推送通道永远在线），日志硬规则判级（grep 级零内存），不跑 AI
- **PC**：大脑 + 采集器。Windows 通知监听、微信/企业微信视觉识别、AI 分诊、结果推送
- **手机**：ntfy 客户端接收 + Wear OS 手表震动，微信/QQ/企业微信后台全杀

### 为什么 PC 侧要绕一条 SSH 隧道

很多网络（校园网、公共 Wi-Fi、移动热点）会封掉非常规端口——本项目实测环境就封了 80 / 2586 / 8080 / 8888，而 SSH 端口放行。于是 `pc/ntfy_tunnel.py` 在本地监听 `127.0.0.1:2586`，把每个连接通过 SSH 的 `direct-tcpip` 通道转到 VPS 的 ntfy 上。好处：

- 不管换到哪个网络，只要 SSH 通，推送通道就通
- ntfy 端口不必对外暴露给 PC，暴露面更小
- PC 侧所有服务只需认 `http://127.0.0.1:2586` 一个地址

如果不需要（你的网络不封端口），把各服务里的 `FP_NTFY_URL` 环境变量设为直连地址即可，隧道进程可以不启动。

## 消息采集方式

| 来源 | 方式 | 实时性 |
|---|---|---|
| QQ / 钉钉 / 学习通等（走 Windows toast 的 App） | UserNotificationListener 抓系统通知 | 秒级实时 |
| 微信 / 企业微信（自绘 UI，不走系统通知） | 窗口藏屏外，定时 PrintWindow 截图 → MiMo 视觉识别 | 30 分钟轮询 |
| VPS 日志 | journalctl 采集 + 硬规则 grep 判级 | 60 秒巡检 |

微信/企业微信的视觉方案说明：这两个 App 的通知是自绘的、不走 Windows 通知中心，UIA 控件树也是黑盒。本项目的解法是把主窗口挪到屏幕外（保持可见不最小化），定时用 PrintWindow 离屏截图发给视觉模型识别——**全程无鼠标劫持、不挡屏幕、不影响操作电脑**。

## 重要消息判定规则（按优先级）

1. **必推**：消息含 `@所有人` / `@我`；内容含 `text`（测试通道）
2. **AI 判定**（MiMo v2.5）：家人/紧急联系人来信、日程提醒、账户安全、服务异常需要处理
3. **黑名单**（无条件忽略）：微信支付、公众号、服务通知、学校新闻、失物招领等系统号
4. AI 调用失败时默认按重要推送（宁滥勿缺）
5. 附带 30 分钟滑动窗口去重（同类事件只报一次+计数）

## 快速上手

前提：一台有公网 IP 的 Linux VPS + 一台 Windows PC（Win10/11）+ 手机装 ntfy 客户端。

### 1. VPS：装 ntfy + 部署监控

```bash
# 安装 ntfy（apt 源或 GitHub release 二进制均可）
apt install ntfy

# 应用服务端配置（记得改 base-url 成你自己的地址）
cp deploy/ntfy-server.yml /etc/ntfy/server.yml

# 创建管理员用户并签发 token（默认拒绝匿名访问）
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
Description=FxxkPush VPS monitor
After=network.target ntfy.service

[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/fuckpush/vps_monitor.py
Environment=FP_NTFY_TOKEN=tk_你的token
Environment=FP_NTFY_URL=http://127.0.0.1:2586/
# 这些 IP 的 SSH 登录不上报（比如 VPS 自身、你自己的固定出口 IP），逗号分隔
Environment=FP_SELF_IPS=你的VPS_IP
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload && systemctl enable --now fuckpush-monitor
systemctl is-active ntfy fuckpush-monitor   # 两个都应是 active
```

### 2. 手机：安装 ntfy 客户端

1. 安装 ntfy（Play Store / F-Droid / App Store）
2. 添加订阅：服务器填 `http://你的VPS-IP:2586`，话题填 `fp-phone`
3. 在订阅设置里填账号密码（步骤 1 创建的用户）并开启高优先级 + 震动

### 3. PC：装依赖 + 配置

```powershell
# Python 3.12（pywinrt 3.2.1 不支持 3.14）
uv venv .venv --python 3.12
uv pip install --python .venv httpx winotify paramiko winrt-runtime `
  winrt-Windows.UI.Notifications.Management winrt-Windows.UI.Notifications `
  winrt-Windows.Foundation winrt-Windows.ApplicationModel winrt-Windows.Foundation.Collections

# 首次授权：允许读取 Windows 通知（弹窗里点允许）
.venv\Scripts\python.exe pc\probe_notifications.py
```

配置本地文件（都不入库）：

```
vps.secret              # 一行：host port user password（隧道与 vps_exec.py 用）
pc/ntfy.secret          # 一行：ntfy token（tk_xxx）
pc/triage_config.json   # AI 配置，示例：
{
  "provider": "mimo",
  "base_url": "https://api.xiaomimimo.com/v1",
  "api_key": "sk-你的key",
  "model": "mimo-v2.5",
  "wechat_poll_min": 30,
  "wechat_my_name": "你的微信昵称",
  "dedup_window_sec": 1800,
  "max_tokens": 200
}
```

### 4. PC：启动与自启

**启动顺序有意义**：隧道先起，其余服务才有 ntfy 可连。

```powershell
.venv\Scripts\pythonw.exe pc\ntfy_tunnel.py            # 1) 隧道（先起）
Start-Sleep 5
.venv\Scripts\pythonw.exe pc\pc_subscriber.py          # 2) 订阅 VPS 告警 → 弹 toast
.venv\Scripts\pythonw.exe pc\notification_listener.py  # 3) 抓 Windows 通知
.venv\Scripts\pythonw.exe pc\ai_triager.py             # 4) AI 分诊
.venv\Scripts\pythonw.exe pc\wechat_vision_listener.py # 5) 微信/企业微信视觉识别

# 或一键重启（已按正确顺序）：双击 pc\restart_services.bat
# 或注册登录自启：pc\install_autostart.bat
```

> 每个服务在任务管理器里会显示成 **2 个进程**（venv 的 `pythonw.exe` 是个跳板，会派生真正的解释器），所以 5 个服务 = 10 个进程，这是正常的。

### 5. 验证

```powershell
# a) 隧道通不通
curl.exe -H "Authorization: Bearer $(Get-Content pc\ntfy.secret)" http://127.0.0.1:2586/v1/health   # 期望 200

# b) 端到端：任意渠道发含 text 的消息 → 手机收到（测试通道，绕过 AI 与去重）
# c) 微信/企业微信：让人发条消息 → 30 分钟内 AI 分诊，重要则手机响
# d) VPS：停一个被监控的服务 → 手机收到告警
# e) 日志：pc\logs\*.log 是各服务的工作日志（pythonw 没有控制台，出问题看这里）
```

## 组件

| 文件 | 运行位置 | 作用 |
|---|---|---|
| `deploy/vps_monitor.py` | VPS (systemd) | journalctl 采集 + 硬规则判级 + 灰区落盘，60s 巡检，冷却去重 |
| `deploy/ntfy-server.yml` | VPS | ntfy 服务端配置（token 鉴权，deny-all） |
| `pc/ntfy_tunnel.py` | PC (自启) | SSH 本地转发：`127.0.0.1:2586` → VPS ntfy，绕开端口封锁，断线自愈 |
| `pc/notification_listener.py` | PC (自启) | UserNotificationListener 抓 toast → fp-pc + JSONL 归档 |
| `pc/wechat_vision_listener.py` | PC (自启) | 微信/企业微信窗口藏屏外 → 定时截图 → MiMo 视觉分诊 |
| `pc/pc_subscriber.py` | PC (自启) | 订阅 fp-vps/fp-gray，弹 Windows toast |
| `pc/ai_triager.py` | PC (自启) | 消费 fp-pc/fp-gray/fp-vps → AI 分诊 → fp-phone / 静默 |
| `pc/park_windows.py` + `拖走聊天窗口.bat` | PC | 一键把微信/企业微信窗口挪到屏幕外（重启后手动归位用） |
| `pc/probe_notifications.py` | PC | 通知权限探测/诊断工具 |
| `pc/restart_services.bat` | PC | 按正确顺序重启全部服务 |
| `vps_exec.py` | PC | SSH 执行助手：跑命令 / `--put` 上传（临时文件+原子替换，断线不会截断目标）/ `--get` |

## 配置文件（不入库，自建）

| 文件 | 内容 |
|---|---|
| `vps.secret` | VPS SSH 凭据（`host port user password` 四段） |
| `pc/ntfy.secret` | ntfy token（一行） |
| `pc/triage_config.json` | AI API 地址/密钥/模型/轮询间隔/微信昵称 |

## 已知边界

- 微信/企业微信消息推送有最长 30 分钟延迟（轮询间隔，用实时性换零打扰，`wechat_poll_min` 可调）
- 微信/企业微信窗口不能最小化到托盘（藏屏幕外可以），否则截图为空；重启电脑后窗口会回到屏内，双击「拖走聊天窗口.bat」归位（vision listener 每轮也会自动归位）
- 屏幕缩放非 100% 时，操作窗口坐标的脚本**必须**先声明 DPI-aware，否则 `GetSystemMetrics` 返回虚拟化尺寸（125% 下 2048 而非物理 2560），算出的"屏幕外"位置会落在屏幕里
- `pc_subscriber` 弹的 toast 会被 `notification_listener` 再抓一次（自己吃自己的尾巴），已用 `pc/toast_echo.jsonl` 指纹在 180 秒窗口内过滤
- pywinrt 3.2.1 需要 Python ≤3.12（3.14 报 cannot create instances）
- 语音/图片类通知：图片通知走视觉模型可判，语音只能看到"发来一条语音"而不知内容
- ReviOS 精简版需验证通知中心可用（本项目环境已验证）

## Roadmap

- [x] v0.1：QQ 等系统通知 + VPS 告警 + AI 分诊 + 手机推送
- [x] v0.2：微信/企业微信视觉识别，@所有人/@我 必推，夜间静默（00:00-08:00）
- [x] v0.2.1：SSH 隧道抗端口封锁、通知回流修复、服务文件日志与崩溃留痕、DPI 修正、窗口一键归位
- [ ] 钉钉 / 学习通等更多 App 深度适配
- [ ] 每日日报（AI 汇总当天事件/误判，22:00 推手机）
- [ ] 误判样本积累 → 未来 27B 模型微调
- [ ] WebSocket 订阅 + PWA 界面
- [ ] Go 重写（性能瓶颈出现后）

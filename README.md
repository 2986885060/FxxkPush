# FuckPush

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

## 分诊规则

硬规则层（VPS 本地，零内存零延迟）：进程 OOM、磁盘 >90%、root SSH 登录、监控单元 failed → 直接推送。

AI 层（PC）：灰色地带判断（新错误模式、频率异常、消息重要性），输出 JSON `{"label": "重要"|"忽略", "reason": "..."}`。内置 30 分钟滑动窗口去重（同类事件只报一次+计数），AI 调用失败时默认按重要推送（宁滥勿缺）。测试规则：内容含 `text` 的消息绕过 AI 直接推手机。

## 部署

### VPS

```bash
# ntfy (apt install ntfy 或 GitHub release 二进制)
sudo cp deploy/ntfy-server.yml /etc/ntfy/server.yml
# 创建用户与 token
NTFY_PASSWORD=xxx ntfy user add --role admin omo
ntfy token add omo
# 监控服务
sudo cp deploy/vps_monitor.py /opt/fuckpush/
# systemd unit 见 deploy/（Environment 里放 FP_NTFY_TOKEN）
```

### PC

```bash
uv venv .venv --python 3.12
uv pip install --python .venv httpx winotify winrt-runtime winrt-Windows.UI.Notifications.Management winrt-Windows.UI.Notifications winrt-Windows.Foundation winrt-Windows.ApplicationModel winrt-Windows.Foundation.Collections
# 写入 ntfy.secret（一行 token）与 triage_config.json（API key 等）
# 首次运行 probe_notifications.py 授权通知访问
# 自启：HKCU\...\Run 键或 pc/install_autostart.bat
```

## 配置文件（不入库，自建）

- `vps.secret` — VPS SSH 密码（`host port user password`）
- `pc/ntfy.secret` — ntfy token
- `pc/triage_config.json` — `{"base_url": "...", "api_key": "...", "model": "mimo-v2.5", ...}`

## 已知边界

- 微信 4.x / 企业微信 PC 端不触发 Windows toast（自绘通知），UIA 树为黑盒，PC 端无法监听——备选方案：手机通知镜像、OCR 浮层、wxauto Plus
- pywinrt 3.2.1 需要 Python ≤3.12（3.14 报 cannot create instances）
- 本机若有 TUN 代理（v2rayN/sing-box），SSH 直连端口可能被截，注意换端口
- ReviOS 精简版需验证通知中心可用（本项目环境已验证）

## Roadmap

- [ ] 每日日报（AI 汇总当天事件/误判，22:00 推手机）
- [ ] 误判样本积累 → 未来 27B 模型微调
- [ ] WebSocket 订阅 + PWA 界面
- [ ] Go 重写（性能瓶颈出现后）

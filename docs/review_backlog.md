# 审查遗留项档案（P2 backlog）

来源：第 8 轮只读审查（监控/告警链路端到端有效性，收官轮），报告原文见
`cache/delegation/live/deleg_370870d1/task-0.log`（会话消息 id=7730，11591 chars 全文）。

**状态图例**：`[x]` 已修 · `[ ]` 留档未修

> 行号基于报告当时的 HEAD=`889ac14`。r8（`947f10c`）改动了 `pc/watchdog.py`
> （+326 行），其中 **watchdog 的行号已漂移**，定位请按函数名/代码串，不要按行号。

## P2 清单（9 条，1 已修 / 8 留档）

- [x] **P2-7　900 字符硬截断无省略标记** — 原 `watchdog.py:384,510`
  截断会砍掉 detail 尾部的「影响/排查」且看不出被截。
  **r8 已修**：`_cut()` 统一 title/message/toast 三处，超长追加 `…(已截断)`，
  AST 断言保证无残留裸切片。

- [ ] **P2-1　告警内容定位性不足** — 原 `watchdog.py:609-614`
  标题用术语（`quiet/link/procs`），手机上看到「[FxxkPush] quiet 故障 5分钟」
  不知道是什么；`影响:` 对 disk/quiet 硬写「对应服务不可用」（disk 满影响的是
  可观测性/state 写入，quiet 是「疑似停摆」）；`排查:` 对 quiet/procs 已知具体
  服务却不给具体文件；link 只给「N 条错误/M 条正常」不带最近错误样例。
  修法：标题走中文语义映射（quiet→日志静默、link→链路报错）；影响按检查项
  枚举；排查输出 `pc/logs/<stem>.log`；detail 尾部附最近一条匹配 `_ERR_PAT`
  的原文（截 120 字）。VPS 硬规则消息缺持续时长与 `journalctl -u` 路径 → 并入。

- [ ] **P2-2　无跨检查项合并/去重** — 原 `watchdog.py:570-623`
  同一故障域连坐多项时逐项各发一条：隧道挂 → health+link 两条（实测 17:25:08 /
  17:26:28），恢复再两条 —— 一次故障 4 条；最坏 5 项同红 = 5 条并发 + 5 条恢复，
  每项各自按 1800s 重复 → 多项长期红时 240 条/天。
  修法：同轮内把所有红项合成一条「N 项异常」（detail 逐项列出），恢复也合并；
  RENOTIFY 改成全局+单项双键。无夜间免打扰属设计取舍，不算缺陷。

- [ ] **P2-3　微信窗口被最小化/关闭 → 采集静默停摆但三项全绿**
  — `wechat_vision_listener.py:147-167,377-393,472-488`
  窗口宽 < min_w(400/500) → `find_window` 返回 None → 每轮只打一行
  `window not found`；`scaned += 1` 照常 → 心跳 `2/2 apps scanned` 绿、quiet 绿；
  watchdog `_ERR_PAT` 要求 300s 内 ≥3 条错误而 wechat 每 30min 才 1 条 → link 绿。
  **发现时长：永不**。修法：连续 2 轮 `window not found`/`capture failed` →
  直推 fp-phone（或把「本轮 scanned < TARGETS 数」纳入判据）。

- [ ] **P2-4　notification_listener 运行期 WinRT 权限被撤 → 返回空但心跳照打**
  — `notification_listener.py:381-383,437-443`
  启动期拒绝访问会 `return 1` → procs 检查能抓 ✓；**运行期**被撤时
  `get_notifications` 返回空（代码注释自述），零错误、`idle heartbeat: seen=…`
  照常 → quiet/link 双绿。**发现时长：永不**。
  修法：周期重新查询 listener 的 access status，或把「连续 N 分钟 seen 无变化
  且 pending 恒 0」做成分级可疑信号。

- [ ] **P2-5　listener 的「看门人已死」告警只有单通道**
  — `notification_listener.py:362-374`
  仅走本地隧道，无 SSH/toast 兜底；watchdog 死因若是网络/隧道（常见），这条告警
  本身发不出，要等网络恢复后 1800s 节流窗口外才补发。
  修法：复用 watchdog 的 `push_vps`/`push_toast`（注意 P1-2 的 try 边界）。

- [ ] **P2-6　AI 二道闸可能吞掉 VPS 硬规则**
  — `deploy/vps_monitor.py:6`（docstring 称 "direct ntfy push to phone"）vs
  `:32,93-94`（实际发 fp-vps）vs `ai_triager.py:63,427,477-518`（照常分诊+dedup）
  实测该 topic 的启动 push 已被判「忽略」静默归档。硬规则若被判忽略 → 手机不响
  （PC 端 `pc_subscriber` 仍会 toast priority=4，故不算全哑）。
  修法：`handle_event` 对 `fp-vps` 且 priority≥4 直接 `push_phone`（与 watchdog
  「绕过 AI」同一哲学），并把 docstring/comment 统一。

- [ ] **P2-8　VPS `save_state` 非原子 + `_state` 形状无校验**
  — `deploy/vps_monitor.py:45-54,56-65`
  `write_text` 直写（PC 侧早已是 tmp+replace），被 OOM kill/断电打断 → 半截 JSON：
  `load_state` 有 try，退化成丢冷却状态（可自愈）；但若变成「合法 JSON 但类型
  不对」（手工改坏），`allowed()` 的 `.get` 会在 `check_*` 里恒抛 → 走
  `gray("monitor-error")` 每 60s 一次，**硬规则永久失效**，而灰区又过 AI
  大概率被忽略。修法：`load_state` 后校验 `cooldowns/counts` 必为 dict，否则
  重置；写盘走 tmp+`os.replace`。

- [ ] **P2-9　wechat 08:00 换挡的 quiet 余量只有约 2 分钟**
  — `wechat_vision_listener.py:465-488` + `watchdog.py:75`
  夜间循环 600s 一行 → 8:00 后先 `sleep(1800)` 再扫 → 相邻日志间隔恒 ≈2400s，
  加上 2 个 app 各 90s AI 超时最多 2580s，阈值 2700s，余量 ≈120s。AI 慢一点
  就会在每天 08:40 左右误报一次。修法：夜间/换挡期在 sleep 里分段打心跳，
  或阈值提到 3300s（先修 P1-1 语义再调 —— P1-1 已在 r8 修完，本条可直接动）。

## 已在 r8 顺手修掉、但不在上面 9 条里的项

（commit message 里编号为 P2-13/14/15，属 r8 新发现的连带项，非报告原文条目）

- **check_vps 的 gray.log 尾行解析失败不判红**（is-active 已覆盖，不因一行脏数据
  制造假告警）
- **canary/push_local 响应体解析失败不影响送达判定**（try 包住）
- **时钟回拨下 canary 间隔用 `_elapsed` 钳制**（负差值立即重发一次，多发无害，
  好过金丝雀永久哑掉）

## 处理建议

P2-6、P2-8 优先级最高（都能让**硬规则永久失效**，属"监控自己坏了还没人知道"
的同类）；P2-3、P2-4 是"永不发现"型盲区；P2-1、P2-2 影响体验不致命；
P2-5、P2-9 是边界场景。下一轮审查前顺手清掉即可。

#!/usr/bin/env bash
# FxxkPush VPS 一键部署：ntfy + 服务端配置 + 管理员账号/token + 监控 systemd。
#
# 用法（root，可重复执行）：
#   bash deploy/setup_vps.sh
# 可选环境变量：
#   FP_PUBLIC_IP=1.2.3.4     # 公网 IP 覆盖自动探测
#   FP_NTFY_USER=admin       # 管理员用户名（默认 fxxkpush）
#   FP_NTFY_PASSWORD=xxx     # 管理员密码（默认随机生成并打印）
#
# 跑完输出「手机订阅所需的全部信息」和「粘贴到 fp.config.json 的 token」。
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ADMIN_USER="${FP_NTFY_USER:-fxxkpush}"

echo "== FxxkPush VPS 一键部署 =="

# ---- 0) root ----
if [ "$(id -u)" -ne 0 ]; then
    echo "[失败] 请用 root 运行：sudo bash deploy/setup_vps.sh"
    exit 1
fi

# ---- 1) 安装 ntfy ----
if command -v ntfy >/dev/null 2>&1; then
    echo "[1/5] ntfy 已安装：$(ntfy version 2>/dev/null || ntfy --version 2>/dev/null | head -1)"
else
    echo "[1/5] 安装 ntfy（apt）..."
    apt-get update -y
    DEBIAN_FRONTEND=noninteractive apt-get install -y ntfy
fi

# ---- 2) 公网 IP ----
if [ -z "${FP_PUBLIC_IP:-}" ]; then
    FP_PUBLIC_IP="$(curl -fsS -4 --max-time 8 https://api.ipify.org || true)"
fi
if [ -z "$FP_PUBLIC_IP" ]; then
    echo "[失败] 自动探测公网 IP 失败，用环境变量重跑：FP_PUBLIC_IP=x.x.x.x bash $0"
    exit 1
fi
echo "[2/5] 公网 IP: $FP_PUBLIC_IP"

# ---- 3) 服务端配置 + 启动 ----
echo "[3/5] 写入 /etc/ntfy/server.yml（base-url 指向 http://${FP_PUBLIC_IP}:2586）"
sed "s|^base-url:.*|base-url: \"http://${FP_PUBLIC_IP}:2586\"|" \
    "$HERE/ntfy-server.yml" > /etc/ntfy/server.yml
systemctl enable --now ntfy
for _ in 1 2 3 4 5; do
    if curl -fsS http://127.0.0.1:2586/v1/health >/dev/null 2>&1; then
        echo "      ntfy health OK"
        break
    fi
    sleep 1
done

# ---- 4) 管理员账号 + token ----
echo "[4/5] 创建管理员 ${ADMIN_USER} ..."
if [ -z "${FP_NTFY_PASSWORD:-}" ]; then
    FP_NTFY_PASSWORD="$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 16)"
fi
# 用户已存在（复跑）时 add 会报错，忽略并继续签发 token
NTFY_PASSWORD="$FP_NTFY_PASSWORD" ntfy user add --role admin "$ADMIN_USER" \
    >/dev/null 2>&1 || echo "      用户已存在（复跑场景），继续..."
# set -e + pipefail 下命令替换失败会直接杀死脚本（曾导致装到一半无声退出、
# 无任何汇总）：临时关 errexit 捕获，失败给指引并跳过 monitor，不中断输出。
set +e
TOKEN="$(ntfy token add "$ADMIN_USER" 2>/dev/null | tr -d '\r' | tail -n1 | tr -d ' ')"
set -e
if [[ "$TOKEN" != tk_* ]]; then
    echo "[警告] token 获取失败（输出：${TOKEN:-空}）"
    echo "       排查：systemctl status ntfy；修复后重跑本脚本（幂等）。monitor 将跳过部署。"
    TOKEN=""
fi

# ---- 5) 监控 systemd（拿到 token 才部署）----
if [ -n "$TOKEN" ]; then
    echo "[5/5] 部署 fuckpush-monitor ..."
    install -d -m 755 /opt/fuckpush
    cp "$HERE/vps_monitor.py" /opt/fuckpush/vps_monitor.py
    cat > /etc/systemd/system/fuckpush-monitor.service <<UNIT
[Unit]
Description=FxxkPush VPS monitor
After=network.target ntfy.service

[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/fuckpush/vps_monitor.py
Environment=FP_NTFY_TOKEN=${TOKEN}
Environment=FP_NTFY_URL=http://127.0.0.1:2586/
Environment=FP_SELF_IPS=${FP_PUBLIC_IP}
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT
    chmod 600 /etc/systemd/system/fuckpush-monitor.service   # 含 token，收紧权限
    systemctl daemon-reload
    systemctl enable --now fuckpush-monitor
    sleep 1
    systemctl is-active --quiet fuckpush-monitor && echo "      monitor active" \
        || echo "      [警告] monitor 未激活，排查：journalctl -u fuckpush-monitor -n 50"
else
    echo "[5/5] 跳过 monitor 部署（无有效 token）"
fi

# ---- 汇总 ----
cat <<SUMMARY

================ FxxkPush VPS 部署完成 ================
订阅服务器:  http://${FP_PUBLIC_IP}:2586
订阅话题:    fp-phone
账号:        ${ADMIN_USER}
密码:        ${FP_NTFY_PASSWORD}
token:       ${TOKEN:-<获取失败 —— 修复后重跑本脚本>}
             ↑ 复制到 PC 的 fp.config.json → ntfy.token

手机端:      装 ntfy App → 订阅 → 服务器/话题/账号密码照上面填
             （开高优先级 + 震动）
PC 端:       解压/克隆仓库 → 双击 setup.bat（向导自动完成其余步骤）
如启用 ufw:  ufw allow 2586/tcp && ufw reload
======================================================
SUMMARY

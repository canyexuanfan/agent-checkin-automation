#!/bin/bash
# WorkBuddy 每日积分签到 —— macOS 一键安装（launchd 每天 09:05）
# 用法：bash install_macos.sh        安装/重装
#      bash install_macos.sh remove 卸载
set -e

LABEL="com.tongchengniu.wbcheckin"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DIR="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$DIR/wb_checkin.py"

if [ "$1" = "remove" ]; then
    launchctl unload "$PLIST" 2>/dev/null || true
    rm -f "$PLIST"
    echo "已卸载：$LABEL"
    exit 0
fi

PY="$(command -v python3 || true)"
[ -z "$PY" ] && { echo "找不到 python3，请先装 Python 3"; exit 1; }
[ -f "$SCRIPT" ] && echo "脚本：$SCRIPT" || { echo "找不到 $SCRIPT"; exit 1; }

AUTH="$HOME/Library/Application Support/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info"
[ -f "$AUTH" ] || echo "提醒：没找到凭据文件，请先打开一次 WorkBuddy 客户端登录"

mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PY</string>
        <string>$SCRIPT</string>
        <string>--verbose</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$DIR</string>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key><integer>9</integer>
        <key>Minute</key><integer>5</integer>
    </dict>
    <key>StandardOutPath</key>
    <string>$DIR/launchd.out.log</string>
    <key>StandardErrorPath</key>
    <string>$DIR/launchd.err.log</string>
    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
EOF

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"

echo "已安装：$PLIST（每天 09:05 自动签到）"
echo "--- 立即试跑一次验证 ---"
"$PY" "$SCRIPT" --verbose

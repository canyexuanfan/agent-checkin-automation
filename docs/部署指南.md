# 部署指南

## 0. 环境

- Windows 10/11，Python 3.10+
- `pip install cryptography`（ima / 豆包 / dumate 的本地凭据解密需要）
- 建议把整个仓库放到固定目录（下文以 `%USERPROFILE%\agent-checkin-automation` 为例）

## 1. 各平台凭据准备

### WorkBuddy（workbuddy-checkin/）
无需准备：脚本自动定位本机 WorkBuddy（CodeBuddy）客户端的 auth 文件。
多账号可安装 `wb-switch` 工具（accounts.json），详见目录内文档。

### MiniMax（minimax-checkin/）
无需准备：脚本从 `%APPDATA%\MiniMax\minimax-agent-cn-config.json` 与
`mcode-public/auth.json` 读取登录态；token 失效时 `token_guard.py` 会尝试无头刷新。

### 百度搭子 DuMate（dumate-checkin/）
无需准备：脚本解密 `%APPDATA%\qianfan-desktop-app\auth.json` 里的加密 cookie
（密钥在同目录 `.cookie-key`），需本机登录过 DuMate 桌面端。

### 宠物旅行（workbuddy-travel/）
需要 `~/.wb-switch/accounts.json`（多账号管理工具保存的 token 列表）；
只有明文 token 的账号也能跑，加密信封 token 需自备 `wb_auth_decrypt.py` 助手放同目录。

### 豆包工作台（doubao-work-checkin/）
1. 装有豆包工作台客户端并登录
2. `python decrypt_all_profiles.py` → 生成 `cookies_default.json`（已 gitignore）
3. `copy device.example.json device.json`，从客户端任一带签名请求的 URL query 里抄
   `web_id` / `tea_uuid` / `web_tab_id` 填入
4. `python dw_checkin.py` 验证

### 腾讯 ima（ima-checkin/）
1. 本机 ima 客户端保持登录
2. `python extract_credentials.py` → 生成 `credentials.json`（已 gitignore；等同账号钥匙勿外传）
3. `python ima_checkin.py` 验证；refresh_token 失效时重开 ima 后重新提取

## 2. 计划任务（Windows）

每日主签到建议错峰排布（示例）：

| 时间 | 任务 |
|------|------|
| 00:05 | workbuddy / minimax |
| 00:06 | ima |
| 00:07 | dumate |
| 00:45 / 08:30 / 12:45 | supervisor 巡检兜底 |

注册方式（管理员不需要，当前用户即可）：

```powershell
$action  = New-ScheduledTaskAction -Execute "pythonw.exe" -Argument "C:\path\to\agent-checkin-automation\ima-checkin\ima_checkin.py"
$trigger = New-ScheduledTaskTrigger -Daily -At "00:06"
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 1)
Register-ScheduledTask -TaskName "ima每日算力签到" -Action $action -Trigger $trigger -Settings $settings
```

巡检任务可直接导入 `docs/task.example.xml`（先改里面的脚本路径），它每天三个时点运行
`supervisor/supervisor.py`：扫描当天日志，未成功的任务自动补跑，最多 3 轮。

## 3. 判定规则

每个脚本把结果追加写到同目录 `*.log`（UTF-8，`[时间] 内容` 格式），
supervisor 用"成功标记 + 退出码"判定当天是否成功：
- 成功标记：`签到成功 / 已签到 / 领取成功 / 已派出` 等
- 豆包与 ima 有"只读探测"前置：无活动投放、今日已领时视为已处理，不发任何签到请求

## 4. 安全注意事项

- 所有 `credentials.json / token.json / cookies*.json / device.json` 已在 `.gitignore`，请勿分享
- 脚本失败不会自动重试，这是防风控的刻意设计；连续失败先看日志再人工处理
- 请遵守各平台用户协议，脚本仅供个人学习与效率自动化

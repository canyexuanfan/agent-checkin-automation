# Agent 签到自动化套件

一组纯 Python 标准库实现的「每日积分/算力」自动签到脚本：读取本机客户端已有的登录凭据，调用官方接口完成签到，无需抓包、无需常驻浏览器，配合 Windows 计划任务 + 巡检兜底实现全自动。

> 仅用于学习与个人效率自动化，请遵守各平台服务条款；脚本只读本机凭据、只调官方接口，不上传任何数据。

## 支持的平台

| 目录 | 平台 | 奖励 | 凭据来源 |
|------|------|------|----------|
| `workbuddy-checkin/` | WorkBuddy（CodeBuddy） | 每日积分 | 客户端本地 auth 文件（自动定位） |
| `minimax-checkin/` | MiniMax Code | 每日积分 | `%APPDATA%\MiniMax` 配置/auth store |
| `dumate-checkin/` | 百度搭子 DuMate | 每日积分 | `%APPDATA%\qianfan-desktop-app` 加密 cookie（本地解密） |
| `workbuddy-travel/` | WorkBuddy 宠物旅行 | 派出/领取 | `~/.wb-switch/accounts.json` |
| `ima-checkin/` | 腾讯 ima | 每日 50 算力 | ima 客户端扩展 Cookie（`extract_credentials.py` 提取） |
| `supervisor/` | 签到补跑巡检 | — | 无（兜底调度器） |

## 共同设计原则

1. **幂等安全**：先只读查询状态，"已签到/不可签"绝不发写请求；只在可领取时才签到
2. **失败不重试**：签到请求失败立即收手，交给巡检在下一个触发点补跑（防风控的关键纪律）
3. **零依赖**：全部使用 Python 标准库（个别脚本需要 `cryptography` 做本地解密）
4. **本地凭据不出门**：所有凭据文件都在 `.gitignore` 中，脚本只在本机读取

## 快速开始

```bash
git clone https://github.com/canyexuanfan/agent-checkin-automation.git
cd agent-checkin-automation
```

每个子目录是一个独立工具，各自的使用说明见 `docs/deploy-guide.md` 与脚本头部注释。典型部署：

1. 安装 Python 3.10+（`cryptography` 按需 `pip install cryptography`）
2. 按指南准备各平台的本地凭据文件
3. 手动跑一次确认成功：`python ima-checkin/ima_checkin.py`
4. 用 `docs/task.example.xml` 注册 Windows 计划任务（每日 00:05~00:07 错峰触发）
5. 注册 `supervisor` 巡检任务（00:45 / 08:30 / 12:45），失败自动补跑

## 目录结构

```
├── supervisor/            # 巡检兜底：检查当天日志，失败自动补跑
├── workbuddy-checkin/     # WorkBuddy 每日积分（含安装脚本与详细文档）
├── minimax-checkin/       # MiniMax 每日积分（含 token 自愈守卫）
├── dumate-checkin/        # 百度搭子每日积分
├── workbuddy-travel/      # 宠物旅行多账号闭环（先领后派）
├── ima-checkin/           # ima 每日算力（含凭据提取）
└── docs/                  # 部署指南与计划任务模板
```

## 许可

- 除 `workbuddy-checkin/` 外，本项目代码以 MIT 许可发布（见 [LICENSE](LICENSE)）
- `workbuddy-checkin/` 沿用上游作者（同城牛 · 阿may）的许可：仅供个人自用、禁止商用，详见该目录下的 [LICENSE.md](workbuddy-checkin/LICENSE.md)

> 说明：豆包工作台签到模块未随本仓库发布（其凭据与设备参数强依赖个人客户端环境，且尚未经作者完整验证），本仓库仅收录已验证可用的模块。

## 免责声明

本项目仅供学习交流与个人自动化使用，与所涉平台官方无关。请自行承担使用风险，勿用于商业用途或高频滥用（所有脚本均已内置防风控限流纪律）。

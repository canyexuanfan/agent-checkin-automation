# 自动领取 workBuddy 积分的脚本

每天自动调 WorkBuddy 的签到接口把积分领了，不用手动打开客户端。
Windows 和 Mac 都能用，**同一份脚本**，装的方式不一样（Windows 用计划任务，Mac 用 launchd）。

- Windows：2026-09-08 在阿may 本机实测跑通（首次签到 +100 积分，连续 6 天）
- macOS：脚本路径逻辑已按 Mac 写好，安装脚本已备；**本机是 Windows，Mac 那半边没实机验证过**，第一次在 Mac 上装完请看一眼有没有报错

---

## 零、你拿到这份，先做三件事

1. **确认你电脑上装了 WorkBuddy 客户端，而且是登录状态**——脚本是借用客户端已经存在本机的登录凭据去签到的，没装没登录它什么也做不了。
2. **把整个文件夹复制到本地磁盘**再用（比如 `C:\tools\wb-checkin\`，Mac 放 `~/tools/wb-checkin/`）。别直接在 OneDrive 共享盘里跑，会同步冲突。
3. **跑一次对应平台的安装脚本**（下面第二、第三节），它会自动装好定时、并立刻试跑一次给你看结果。

安装脚本认的是「它自己所在的文件夹」，所以你放哪个目录都行，不用改任何路径。

---

## 一、文件清单

四个文件放同一个目录即可（下文命令里的 `C:\tools\wb-checkin` 是示例，换成你自己放的路径）：

| 文件 | 干什么的 |
|------|---------|
| `wb_checkin.py` | 主脚本，双平台通用，自动认平台找凭据 |
| `install_windows.ps1` | Windows 一键装计划任务（可重复跑，重装不会重复建） |
| `install_macos.sh` | Mac 一键装 launchd 定时 |
| `run_silent.vbs` | Windows 静默启动器，装的时候自动生成，**别手改** |
| `checkin.log` | 运行日志，每次跑都记一笔（第一次跑之后才有） |

> 为什么建议放 `C:\tools` 这类本地目录、不放 OneDrive：日志天天在变，放同步盘会一直触发同步、也会污染知识库的 git 快照。

---

## 二、Windows 怎么用

### 装（阿may 本机已装好；换电脑、任务被删、或你是新拿到这份的人，跑这一句）

```
powershell -ExecutionPolicy Bypass -File C:\tools\wb-checkin\install_windows.ps1
```

装完会立刻试跑一次，看到「签到成功」或「今天已签到过了」就是通的。

- 想换时间：`... install_windows.ps1 -Time "08:30"`
- 想卸载：`... install_windows.ps1 -Remove`

### 手动跑一次

```
python C:\tools\wb-checkin\wb_checkin.py --verbose
```

### 现在装的是什么

- 计划任务名：**WorkBuddy每日积分签到**，每天 **09:05**
- 勾了「错过了就补跑」（StartWhenAvailable），关机或没到点开机第二天开机会补上
- 走 `wscript //B` 启动，**不弹黑框**
- 只在你登录 Windows 后跑（要读你账号下的凭据，这是必须的）

---

## 三、Mac 怎么用

### 装

把四个文件拷到 Mac 上任意目录（比如 `~/tools/wb-checkin/`），然后：

```
bash ~/tools/wb-checkin/install_macos.sh
```

装完会立刻试跑一次验证。

- 卸载：`bash install_macos.sh remove`
- 换时间：编辑脚本里 `<key>Hour</key><integer>9</integer>` 那两行，改完重跑一次安装

### 手动跑一次

```
python3 ~/tools/wb-checkin/wb_checkin.py --verbose
```

### 装的是什么

- launchd 任务：`~/Library/LaunchAgents/com.tongchengniu.wbcheckin.plist`
- 每天 09:05；Mac 那会儿在睡眠的话，launchd 会在**唤醒后补跑一次**（这点比 cron 强，cron 错过就是错过了）
- 额外日志：同目录 `launchd.out.log` / `launchd.err.log`（launchd 自己的输出，排错时看）

### Mac 上可能卡住的地方

`~/Library/Application Support/` 在新版 macOS 属于受保护目录。如果日志报「Auth 文件不存在」但文件明明在，去 **系统设置 → 隐私与安全性 → 完全磁盘访问权限**，把终端（或 Python）加进去。

---

## 四、脚本干了什么（接口说明）

两个接口，都是 POST，请求体是空的 `{}`：

| 用途 | 地址 |
|------|------|
| 查签到状态 | `https://copilot.tencent.com/billing/meter/checkin-status` |
| 执行签到 | `https://copilot.tencent.com/billing/meter/daily-checkin` |

请求头三个：

```
Authorization: Bearer {accessToken}
Content-Type: application/json
X-User-Id: {uid}
```

签到成功返回：

```json
{"code": 0, "msg": "OK", "data": {"credit": 100, "streak_days": 6, "is_streak_day": false}}
```

今天已经签过返回（注意 HTTP 是 400 不是 200）：

```json
{"code": 10001, "msg": "今天已签到，请明天再来", "data": null}
```

---

## 五、凭据在哪（两个平台不一样）

脚本自动按平台找，不用手配。凭据是 WorkBuddy 客户端自己存的明文 JSON：

| 平台 | 路径 |
|------|------|
| Windows | `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop.info` |
| macOS | `~/Library/Application Support/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info` |

同目录下如果出现 `workbuddy-desktop.info.logged-out` 这个文件，说明客户端已登出，脚本会直接报错提醒，不会瞎试。

**token 有效期约 60 天**（当前这个到 **2026-11-07**）。过期不用做别的，打开一次 WorkBuddy 客户端它自己会刷新写回文件。脚本在剩不到 7 天时会在日志里提醒你。

脚本只在本地读这个文件，不往任何地方上传。

---

## 六、踩过的坑（都是实测出来的，别再踩一遍）

**1. 网上的手册是 Mac 路径，Windows 完全对不上**
Windows 没有 `~/Library/Application Support`，实际在 `%LOCALAPPDATA%\CodeBuddyExtension`。照抄 Mac 路径会一直报「Auth 文件不存在」。

**2. 状态接口是失真的，不能拿它判断今天签没签**
实测 `checkin-status` 一直返回 `today_checked_in: false`、`total_credits: 0`、`streak_days: 0`，但签到接口返回的却是「连续 6 天」。原因是活动 `active: false` 时这些字段不回填。
**所以脚本不看状态，直接调签到接口**，靠返回码判断结果。

**3. 重复签到返回的是 HTTP 400，不是 200**
业务码 10001「今天已签到」。写脚本时如果只处理 2xx，这里会当成失败报错。脚本对 400 也解析 JSON，把 10001 当成功。

**4. Windows 计划任务直接跑 python 会弹黑框**
所以套了一层 `run_silent.vbs` + `wscript //B`。这个 vbs **必须是纯 ASCII**，写中文注释进去会让 wscript 静默失败（老坑，见知识库「计划任务两大坑」）。

**5. PowerShell 5.1 读 UTF-8 无 BOM 的 .ps1 会乱码**
`install_windows.ps1` 里有中文，存成无 BOM 的 UTF-8 时中文变乱码，还会连带把引号配对搞乱导致语法报错。**必须存成 UTF-8 with BOM。**（`install_macos.sh` 反过来，bash 不吃 BOM，得存无 BOM。）

**6. 计划任务的 WorkingDirectory 必须设**
不设的话目录一动就静默死（错误码 -2147024629），这个坑之前吃过一次。

---

## 七、出问题了怎么查

**第一步永远是看日志**：`checkin.log`，Mac 上还可以看 `launchd.err.log`。

| 日志里写的 | 什么原因 | 怎么办 |
|-----------|---------|--------|
| Auth 文件不存在 | 没登录，或路径不对 | 打开 WorkBuddy 客户端登录；Mac 上还要给终端「完全磁盘访问权限」 |
| 检测到 logout marker | 已登出 | 打开客户端重新登录 |
| Token 已过期 | 超过 60 天没开客户端 | 打开一次 WorkBuddy 客户端，会自动刷新 |
| 今天已签到过了 | 今天领过了 | 正常，不是错误，退出码是 0 |
| HTTP 401 / 403 | token 失效 | 重新登录 WorkBuddy |
| 日志里压根没新记录 | 定时没触发 | Windows：任务计划程序看「上次运行结果」是不是 0；Mac：`launchctl list \| grep wbcheckin` |

**确认定时任务还活着：**

```
# Windows
Get-ScheduledTaskInfo -TaskName "WorkBuddy每日积分签到"

# Mac
launchctl list | grep wbcheckin
```

---

## 八、还没做的

- **token 自动刷新**：现在到期得手动开一次客户端。客户端的 refresh 机制没逆向，`refreshToken` 有效期 90 天，理论上能在脚本里自己刷，要花时间研究签发端。
- **积分余额查询**：`/billing/meter/get-user-resource` 这个接口没试过。
- **签到成功推个通知**：现在只写日志，没主动告诉你。

---

---

## 九、版权与使用范围

本工具及本手册由**同城牛 · 阿may** 出品，免费提供给同城牛联盟成员、内部团队与学员**个人自用**。

- 可以自己用、可以在团队内传、可以照着改成自己顺手的样子
- **禁止任何形式的商用**：不得售卖、不得打包进付费产品或付费课程、不得作为对外收费服务的一部分
- 转发时请保留本说明与出处

脚本只在你自己电脑上读取 WorkBuddy 客户端已有的登录凭据，用来调用官方签到接口，**不上传、不外发你的任何数据**，也不碰你的密码。凭据文件是 WorkBuddy 客户端自己以明文存在本机的，本工具只读不写。

---

*2026-09-08 建成，Windows 实测通过 · 同城牛 · 阿may*

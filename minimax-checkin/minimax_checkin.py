#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MiniMax Code 每日积分自动签到（Windows 专用，本地读取本机登录态）

思路与 WorkBuddy 那个脚本一致：只读取本机 MiniMax 桌面端已保存的登录凭据，
调官方接口把每日积分领了，省得手动开客户端。区别是 MiniMax 的请求头要带
逆向出来的 yy / x-signature 签名，下面 sign_request() 已按客户端算法复刻。

凭据位置（本机实测）：
    %APPDATA%\\MiniMax\\minimax-agent-cn-config.json
    -> tokens.accessToken  (JWT，明文)
    -> user.realUserID     (日志里 user_id 用的就是这个)

接口（本机日志 + 已逆向脚本双重确认）：
    POST  agent.minimaxi.com/v1/api/user/renewal          # 续期，相当于先登录
    GET   agent.minimaxi.com/minimax-cloud/api/v1/signin/status
    POST  agent.minimaxi.com/minimax-cloud/api/v1/signin/claim

用法:
    python minimax_checkin.py            # 续期 + 查状态 + 签到
    python minimax_checkin.py --status  # 只查状态(只读，不签到)
    python minimax_checkin.py --verbose # 打印完整响应

注释：脚本只把 token 发给官方域名 agent.minimaxi.com，不会上传任何数据。
幂等：同一天重复运行只会返回“已签到”，不会重复扣分。
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

HOST = "https://agent.minimaxi.com"
RENEW_PATH = "/v1/api/user/renewal"
STATUS_PATH = "/minimax-cloud/api/v1/signin/status"
CLAIM_PATH = "/minimax-cloud/api/v1/signin/claim"

UA = ("MiniMaxAgent Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) MiniMax/3.0.65 Chrome/148.0.7778.280 Safari/537.36")

LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "minimax-checkin.log")


# ----------------------------------------------------------------------------
# 配置读取
# ----------------------------------------------------------------------------
def config_path():
    base = os.environ.get("APPDATA") or os.path.expanduser(r"~\AppData\Roaming")
    return os.path.join(base, "MiniMax", "minimax-agent-cn-config.json")


def mcode_auth_path():
    """新版客户端(3.0.72+)的 OAuth 令牌存储：桌面端开机自启会定期用 refreshToken
    刷新 accessToken 并回写此文件，是最稳定的本机 token 来源。"""
    return os.path.join(os.path.expanduser("~"), ".minimax", "auth", "prod", "cn",
                        "mcode-public", "auth.json")


def load_token_from_mcode_auth():
    """从新版客户端 auth store 取 accessToken（mmoat_ 前缀，约 6 小时有效）。
    多账号记录时取 expiresAtMs 最大（最新）的那条。返回 token 或 None。"""
    p = mcode_auth_path()
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        best, best_exp = None, -1
        for rec in (data.get("records") or {}).values():
            if not isinstance(rec, dict):
                continue
            tok = rec.get("accessToken") or ""
            exp = rec.get("expiresAtMs") or 0
            if tok.startswith("mmoat_") and exp >= best_exp:
                best, best_exp = tok, exp
        return best
    except Exception as e:
        log("警告：读取新版 auth store 失败：%s" % e)
        return None


CLIENT_EXE = r"E:\Program\MiniMax\MiniMax Code\MiniMax Code.exe"
# 2026-10-23 重大升级：从客户端 asar 挖出 OAuth 端点，refresh_token (mmort_) 可脱离
# 客户端自主轮换续期（grant_type=refresh_token, client_id=mcode-public），刷新后必须
# 把新的 access/refresh token 回写 auth.json 并递增 generation（模拟 broker 行为），
# 否则旧 refresh token 作废会导致客户端要求重新登录。已实测打通（generation 225→226）。
OAUTH_TOKEN_URL = "https://account.minimax.cn/oauth2/token"
OAUTH_CLIENT_ID = "mcode-public"


def mcode_auth_fresh_ms():
    """auth.json 里最新记录的 expiresAtMs（不读令牌值，只看过期时间）；文件不存在返回 0。"""
    p = mcode_auth_path()
    if not os.path.exists(p):
        return 0
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        return max((r.get("expiresAtMs") or 0
                    for r in (data.get("records") or {}).values()
                    if isinstance(r, dict)), default=0)
    except Exception:
        return 0


def client_running():
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq MiniMax Code.exe"],
                             capture_output=True, timeout=20)
        # 中文系统 tasklist 输出 GBK，避免 UnicodeDecodeError
        return "MiniMax Code.exe" in out.stdout.decode("gbk", errors="ignore")
    except Exception:
        return False


def refresh_via_oauth(max_wait_s=90):
    """首选无头刷新：直接调 account.minimax.cn/oauth2/token 用 mmort_ refreshToken
    轮换出新 accessToken/refreshToken，并回写 auth.json（generation+1）。
    全程纯 HTTP，不依赖客户端进程（2026-10-23 实测打通）。"""
    p = mcode_auth_path()
    if not os.path.exists(p):
        log("无头刷新失败：auth.json 不存在")
        return False
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        key, rec = next(iter((data.get("records") or {}).items()))
        rt = rec.get("refreshToken")
        if not rt:
            log("无头刷新失败：auth.json 里没有 refreshToken")
            return False
    except Exception as e:
        log("无头刷新失败：读取 auth.json 异常：%s" % e)
        return False
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "client_id": OAUTH_CLIENT_ID,
        "refresh_token": rt,
    }).encode()
    req = urllib.request.Request(
        OAUTH_TOKEN_URL, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "User-Agent": "MiniMaxCode/3.0.72"})
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        out = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        log("无头刷新失败：OAuth 端点 HTTP %s：%s"
            % (e.code, e.read().decode("utf-8", "ignore")[:200]))
        return False
    except Exception as e:
        log("无头刷新失败：网络异常：%s" % e)
        return False
    if not out.get("access_token"):
        log("无头刷新失败：OAuth 响应缺少 access_token")
        return False
    now_ms = int(time.time() * 1000)
    rec["accessToken"] = out["access_token"]
    if out.get("refresh_token"):
        rec["refreshToken"] = out["refresh_token"]  # 轮换：必须回写，否则作废
    rec["expiresAtMs"] = now_ms + int(out.get("expires_in", 3600)) * 1000
    rec["generation"] = int(rec.get("generation", 0)) + 1
    rec.setdefault("tokenType", out.get("token_type", "Bearer"))
    data["records"][key] = rec
    json.dump(data, open(p, "w", encoding="utf-8"), ensure_ascii=False)
    # 同步 auth-state.json 的 generation/expiresAtMs（客户端用它们判断新旧）
    try:
        sp = mcode_auth_path().replace("auth.json", "auth-state.json")
        if os.path.exists(sp):
            st = json.load(open(sp, encoding="utf-8"))
            st["generation"] = rec["generation"]
            st["expiresAtMs"] = rec["expiresAtMs"]
            json.dump(st, open(sp, "w", encoding="utf-8"), ensure_ascii=False)
    except Exception:
        pass
    log("无头刷新成功（OAuth）：generation=%d，新 token 有效期至 %s"
        % (rec["generation"],
           datetime.fromtimestamp(rec["expiresAtMs"] / 1000).strftime("%m-%d %H:%M")))
    return True


def self_heal_token(max_wait_s=180):
    """所有本机 token 来源都 401 时的自愈（分两级）：
    1) 无头：OAuth refreshToken 自主轮换（纯 HTTP，不依赖客户端，首选）；
    2) 兜底：客户端没运行就拉起 GUI，等它刷新 auth.json（客户端存在但
       OAuth 通道也挂了时的最后手段）。
    成功后返回新 token。"""
    now_ms = lambda: int(time.time() * 1000)
    # 第一级：无头 OAuth 刷新
    if refresh_via_oauth():
        tok = load_token_from_mcode_auth()
        if tok:
            return tok
    # 第二级：拉起 GUI 客户端
    baseline = mcode_auth_fresh_ms()
    if not client_running():
        if not os.path.exists(CLIENT_EXE):
            log("自愈失败：找不到客户端 %s" % CLIENT_EXE)
            return None
        log("无头刷新未触发，客户端未运行，自动拉起 %s（会弹出窗口，属正常现象）..." % CLIENT_EXE)
        try:
            # DETACHED_PROCESS：脱离本脚本生命周期，签到进程退出后客户端继续刷新
            subprocess.Popen([CLIENT_EXE], creationflags=0x00000008)
        except Exception as e:
            log("自愈失败：无法启动客户端：%s" % e)
            return None
    else:
        log("客户端已在运行，等待其刷新...")
    deadline = time.time() + max_wait_s
    while time.time() < deadline:
        time.sleep(10)
        exp = mcode_auth_fresh_ms()
        if exp > max(baseline, now_ms() + 5 * 60 * 1000):
            tok = load_token_from_mcode_auth()
            if tok:
                log("自愈成功：客户端已刷新登录态（新 token 有效期至 %s）"
                    % datetime.fromtimestamp(exp / 1000).strftime("%m-%d %H:%M"))
                return tok
    log("自愈未果：等待 %ds 后 auth.json 仍未刷新（客户端可能未登录/休眠中/网络异常）" % max_wait_s)
    return None


def load_auth():
    """读取本机登录态（按可靠性排序）：
    1) 新版客户端 auth store 的 mmoat_ accessToken（客户端持续自刷新，首选）；
    2) 脚本自己持久化的 token.json（旧版 JWT）；
    3) Local Storage leveldb 明文 JWT（旧版兜底）。
    不再依赖 config.tokens（新版客户端已清空该字段并改用加密 Cookie）。
    uid 始终以 config 里的 realUserID 为准（账户稳定标识）。"""
    p = config_path()
    if not os.path.exists(p):
        raise RuntimeError("找不到 MiniMax 配置：%s（请先打开一次 MiniMax Code 桌面端登录）" % p)
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    uid = str((data.get("user") or {}).get("realUserID") or (data.get("user") or {}).get("userID"))
    token = load_token_from_mcode_auth()
    if token:
        return token, uid
    token = load_token_from_file()
    if not token:
        # 兜底：直接从客户端 Local Storage 的明文 JWT 提取（无需解密、不碰客户端锁死的 Cookie）
        token = extract_token_from_leveldb()
    if not token or not uid:
        raise RuntimeError("找不到可用 token，请打开 MiniMax Code 桌面端登录，或手动把 token 写入 token.json")
    return token, uid


def token_file_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "token.json")


def load_token_from_file():
    p = token_file_path()
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f).get("token")
    except Exception:
        return None


def save_token_to_file(token):
    """把可用的 token 原子写入脚本自己的 token.json（绝不回写 config/Cookie，
    避免把客户端正在用的令牌作废导致用量加载失败）。"""
    p = token_file_path()
    tmp = p + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"token": token,
                       "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
                      f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
        return True
    except Exception as e:
        log("警告：写 token.json 失败：%s" % e)
        try:
            os.remove(tmp)
        except Exception:
            pass
        return False


def extract_token_from_leveldb():
    """从客户端 Local Storage(leveldb) 的明文 JWT 里挑 exp 最大且未过期的那个。
    这是新版本客户端唯一能从本机只读拿到的有效 token（Cookie 已被加密且独占锁死）。
    返回 token 字符串或 None。"""
    import re as _re
    root = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~/.config"),
                        "MiniMax", "Local Storage", "leveldb")
    if not os.path.isdir(root):
        return None
    pat = _re.compile(rb'eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+')

    def b64url(s):
        s += b'=' * (-len(s) % 4)
        return base64.urlsafe_b64decode(s)

    cands = []
    for fn in os.listdir(root):
        fp = os.path.join(root, fn)
        if not os.path.isfile(fp):
            continue
        try:
            data = open(fp, "rb").read()
        except Exception:
            continue  # 客户端可能临时锁住个别 .ldb，跳过即可
        for mm in pat.finditer(data):
            tok = mm.group(0).decode()
            try:
                exp = json.loads(b64url(tok.split(".")[1].encode())).get("exp", 0)
            except Exception:
                exp = 0
            cands.append((exp, tok))
    if not cands:
        return None
    cands.sort(reverse=True)
    return cands[0][1]


# ----------------------------------------------------------------------------
# 签名（复刻 MiniMax 桌面端算法）
# ----------------------------------------------------------------------------
def md5_hex(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def encode_uri_component(s: str) -> str:
    """忠实复刻 JavaScript 的 encodeURIComponent：
    仅放行 A-Za-z0-9 以及 - _ . ! ~ * ' ( )，其余按 %XX（大写）编码。"""
    return re.sub(r"([^A-Za-z0-9\-_.!~*'()])",
                  lambda m: "%%%02X" % ord(m.group(1)), s)


def build_params(uid: str, now_ms: int):
    """本机 signin 日志里抓到的真实参数顺序（uuid/device_id 用本机实测值）。"""
    return [
        ("device_platform", "web"),
        ("biz_id", "3"),
        ("app_id", "3001"),
        ("version_code", "22201"),
        ("unix", str(now_ms)),
        ("timezone_offset", "28800"),
        ("is_desktop", "1"),
        ("desktop_version", ""),
        ("sys_language", "zh"),
        ("lang", "zh"),
        ("uuid", "da1d5085-eb02-44d4-8e97-901604497e62"),
        ("device_id", "51702562"),
        ("os_name", "Windows"),
        ("browser_name", "Chrome"),
        ("device_memory", "16"),
        ("cpu_core_num", "20"),
        ("browser_language", "zh-CN"),
        ("browser_platform", "Win32"),
        ("user_id", uid),
        ("op_ticket", "undefined"),
        ("screen_width", "1707"),
        ("screen_height", "960"),
        ("client", "desktop"),
    ]


def sign_request(path, uid, method, body, now_ms):
    """返回 (full_url, headers)。签名对 token 无依赖，token 仅作请求头。"""
    now_sec = now_ms // 1000
    params = build_params(uid, now_ms)
    query = urllib.parse.urlencode(params)
    has_search_params_path = "%s?%s" % (path, query)

    body_str = "" if method.lower() == "get" else json.dumps(body or {}, ensure_ascii=False)

    # x-signature = md5("{秒时间戳}I*7Cf%WZ#S&%1RlZJ&C2{body}")
    x_signature = md5_hex("%dI*7Cf%%WZ#S&%%1RlZJ&C2%s" % (now_sec, body_str))
    # yy = md5( encodeURIComponent(带参路径) + "_" + "{}" + md5(毫秒时间戳) + "ooui" )
    inner = md5_hex(str(now_ms))
    yy = md5_hex(encode_uri_component(has_search_params_path) + "_" + "{}" + inner + "ooui")

    headers = {
        "token": "",  # 调用方填充
        "yy": yy,
        "x-timestamp": str(now_sec),
        "x-signature": x_signature,
        "origin": "https://agent.minimaxi.com",
        "referer": "https://agent.minimaxi.com/",
        "user-agent": UA,
    }
    if method.lower() != "get":
        headers["content-type"] = "application/json"
    return HOST + path + "?" + query, headers


# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------
def make_request(path, uid, token, method="GET", body=None, timeout=30):
    now_ms = int(round(datetime.now(timezone.utc).timestamp() * 1000))
    url, headers = sign_request(path, uid, method, body, now_ms)
    # 新版 OAuth 令牌(mmoat_)走 Authorization: Bearer；旧版 JWT 走 token 头
    if token.startswith("mmoat_"):
        headers.pop("token", None)
        headers["Authorization"] = "Bearer " + token
    else:
        headers["token"] = token
    payload = json.dumps(body if body is not None else {}).encode("utf-8") if method.lower() != "get" else None
    req = urllib.request.Request(url, data=payload, method=method)
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"code": -1, "msg": raw[:300]}
    except urllib.error.URLError as e:
        return -1, {"code": -1, "msg": "网络错误：%s" % e}


# ----------------------------------------------------------------------------
# 日志
# ----------------------------------------------------------------------------
def log(msg):
    line = "[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ----------------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------------
def resp_ok(res):
    """MiniMax 三个接口分别用 statusInfo.code / base_resp.status_code / code 包成功态，统一判。"""
    if not isinstance(res, dict):
        return False
    if res.get("code") == 0:
        return True
    if (res.get("statusInfo") or {}).get("code") == 0:
        return True
    if (res.get("base_resp") or {}).get("status_code") == 0:
        return True
    return False


def main():
    ap = argparse.ArgumentParser(description="MiniMax Code 每日积分签到")
    ap.add_argument("--status", action="store_true", help="只查询签到状态(只读)")
    ap.add_argument("--verbose", "-v", action="store_true", help="打印完整响应")
    args = ap.parse_args()

    try:
        token, uid = load_auth()
    except RuntimeError as e:
        log("失败：%s" % e)
        return 2

    if args.verbose:
        log("账号 realUserID=%s  凭据文件=%s" % (uid, config_path()))

    # 1) 用现有 token 查状态（只读 GET；绝不做令牌轮换，避免把客户端正在用的令牌作废）
    code, res = make_request(STATUS_PATH, uid, token, "GET")
    if args.verbose:
        log("状态响应 HTTP %s：%s" % (code, json.dumps(res, ensure_ascii=False)))
    if code in (401, 403):
        # token 失效：依次尝试其它本机来源（新版 auth store / token.json / Local Storage JWT）
        log("token 失效(HTTP %s)，尝试从本机存储重新提取最新 token..." % code)
        for alt in [load_token_from_mcode_auth(), load_token_from_file(),
                    extract_token_from_leveldb()]:
            if alt and alt != token:
                code, res = make_request(STATUS_PATH, uid, alt, "GET")
                if code not in (401, 403):
                    token = alt
                    if not alt.startswith("mmoat_"):
                        save_token_to_file(token)  # 只回写旧版 JWT，mmoat_ 由客户端自管
                    log("已切换到新的 token 来源并验证通过")
                    break
        if code in (401, 403):
            # 最后手段：拉起客户端刷新登录态（自愈），成功则重试一次
            tok3 = self_heal_token()
            if tok3:
                code, res = make_request(STATUS_PATH, uid, tok3, "GET")
                if code not in (401, 403):
                    token = tok3
                    log("已通过自愈恢复，继续签到流程")
        if code in (401, 403):
            log("失败：Token 无效且自愈未成功，请打开 MiniMax Code 桌面端重新登录")
            return 2

    d = (res or {}).get("data") or {}
    already = False
    if resp_ok(res):
        days = d.get("days") or []
        today = next((x for x in days if x.get("is_today")), None)
        if today is not None:
            already = today.get("status") == 3
            signed = "已签" if already else "未签"
            log("当前状态：今天是第 %s 天（%s），今日积分 %s"
                % (today.get("day_no"), signed, today.get("points")))
        else:
            log("当前状态：连续 %s天  今日积分=%s  总积分=%s"
                % (d.get("streak_days"), d.get("today_credit"), d.get("total_credits")))
    else:
        log("状态查询返回：code=%s msg=%s" % ((res or {}).get("code"), (res or {}).get("msg")))

    if args.status:
        return 0

    # 3) 签到
    if already:
        log("今天已签到过了，无需重复")
        return 0
    code, res = make_request(CLAIM_PATH, uid, token, "POST", {})
    if args.verbose:
        log("签到响应 HTTP %s：%s" % (code, json.dumps(res, ensure_ascii=False)))

    biz = (res or {}).get("code")
    if resp_ok(res) or biz == 0:
        dd = (res or {}).get("data") or {}
        log("签到成功：第 %s 天 +%s 积分（%s 天内有效）"
            % (dd.get("day_no"), dd.get("points"),
               "30" if dd.get("expire_at_ms") else "?"))
        return 0
    msg = str((res or {}).get("msg", ""))
    if "已签到" in msg or "已经" in msg or biz in (10001, 40001):
        log("今天已签到过了（%s），视为成功" % (res or {}).get("msg"))
        return 0
    if code in (401, 403):
        log("签到时 token 失效(HTTP %s)，尝试自愈后重试..." % code)
        t4 = self_heal_token()
        if t4:
            code, res = make_request(CLAIM_PATH, uid, t4, "POST", {})
            if resp_ok(res) or (res or {}).get("code") == 0:
                dd = (res or {}).get("data") or {}
                log("自愈后签到成功：第 %s 天 +%s 积分" % (dd.get("day_no"), dd.get("points")))
                return 0
        log("签到失败：HTTP %s，Token 无效且自愈未成功，请打开客户端重新登录" % code)
        return 2
    log("签到失败：HTTP %s code=%s msg=%s" % (code, biz, (res or {}).get("msg")))
    return 1


if __name__ == "__main__":
    sys.exit(main())

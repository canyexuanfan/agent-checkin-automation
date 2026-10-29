#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MiniMax 签到令牌守卫（每日 23:30 由计划任务"MiniMax签到令牌守卫"运行）

作用：保证 00:05 的签到任务拿到有效的 mmoat_ token（实测有效期约 1 小时）。
    - 读 ~/.minimax/auth/.../auth.json 的 expiresAtMs（只读过期时间，不碰令牌值）；
    - 判断标准：token 必须能撑到【次日 00:10】（00:05 签到 + 5 分钟裕量），
      而不是"当前未临期"——旧版在 23:30 发现剩余 23 分钟就放行，导致
      23:53 过期、00:05 签到必然 401（2026-10-23 排查结论）；
    - 撑不到 → 无头刷新：直接调 account.minimax.cn/oauth2/token 用 mmort_
      refreshToken 轮换续期（纯 HTTP，不依赖客户端，2026-10-23 实测打通，
      刷新后回写 auth.json 并递增 generation，模拟 broker 行为）；
    - OAuth 也失败 → 兜底拉起 MiniMax Code.exe（GUI），等它刷新。

退出码：0 = token 可用或刷新成功；1 = 刷新失败（00:05 签到脚本仍有自愈兜底）。
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

CLIENT_EXE = r"E:\Program\MiniMax\MiniMax Code\MiniMax Code.exe"
OAUTH_TOKEN_URL = "https://account.minimax.cn/oauth2/token"
OAUTH_CLIENT_ID = "mcode-public"
AUTH_JSON = os.path.join(os.path.expanduser("~"), ".minimax", "auth", "prod", "cn",
                         "mcode-public", "auth.json")
LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "minimax-checkin.log")


def log(msg):
    line = "[%s] [守卫] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def auth_fresh_ms():
    if not os.path.exists(AUTH_JSON):
        return 0
    try:
        with open(AUTH_JSON, encoding="utf-8") as f:
            data = json.load(f)
        return max((r.get("expiresAtMs") or 0
                    for r in (data.get("records") or {}).values()
                    if isinstance(r, dict)), default=0)
    except Exception as e:
        log("警告：读取 auth.json 失败：%s" % e)
        return 0


def refresh_via_oauth():
    """无头刷新：OAuth refreshToken 轮换，成功后回写 auth.json（generation+1）。
    返回 (ok, new_expires_ms)。"""
    try:
        with open(AUTH_JSON, encoding="utf-8") as f:
            data = json.load(f)
        key, rec = next(iter((data.get("records") or {}).items()))
        rt = rec.get("refreshToken")
        if not rt:
            log("无头刷新失败：auth.json 里没有 refreshToken")
            return False, 0
    except Exception as e:
        log("无头刷新失败：读取 auth.json 异常：%s" % e)
        return False, 0
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
        log("无头刷新失败：OAuth HTTP %s：%s"
            % (e.code, e.read().decode("utf-8", "ignore")[:200]))
        return False, 0
    except Exception as e:
        log("无头刷新失败：网络异常：%s" % e)
        return False, 0
    if not out.get("access_token"):
        log("无头刷新失败：OAuth 响应缺少 access_token")
        return False, 0
    now_ms = int(time.time() * 1000)
    rec["accessToken"] = out["access_token"]
    if out.get("refresh_token"):
        rec["refreshToken"] = out["refresh_token"]  # 轮换：必须回写，否则作废
    rec["expiresAtMs"] = now_ms + int(out.get("expires_in", 3600)) * 1000
    rec["generation"] = int(rec.get("generation", 0)) + 1
    rec.setdefault("tokenType", out.get("token_type", "Bearer"))
    data["records"][key] = rec
    json.dump(data, open(AUTH_JSON, "w", encoding="utf-8"), ensure_ascii=False)
    try:
        sp = AUTH_JSON.replace("auth.json", "auth-state.json")
        if os.path.exists(sp):
            st = json.load(open(sp, encoding="utf-8"))
            st["generation"] = rec["generation"]
            st["expiresAtMs"] = rec["expiresAtMs"]
            json.dump(st, open(sp, "w", encoding="utf-8"), ensure_ascii=False)
    except Exception:
        pass
    return True, rec["expiresAtMs"]


def wait_refresh(baseline, max_wait_s):
    deadline = time.time() + max_wait_s
    while time.time() < deadline:
        time.sleep(10)
        exp = auth_fresh_ms()
        if exp > max(baseline, int(time.time() * 1000) + 5 * 60 * 1000):
            return exp
    return 0


def client_running():
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq MiniMax Code.exe"],
                             capture_output=True, timeout=20)
        return "MiniMax Code.exe" in out.stdout.decode("gbk", errors="ignore")
    except Exception:
        return False


def main():
    now_ms = int(time.time() * 1000)
    exp = auth_fresh_ms()
    if exp == 0:
        log("auth.json 不存在或无有效记录（可能从未登录过新版客户端），退出")
        return 1
    # 判断标准：token 必须覆盖到次日 00:10（00:05 签到 + 5 分钟裕量）
    tomorrow = datetime.now().replace(hour=0, minute=10, second=0, microsecond=0)
    if tomorrow <= datetime.now():
        tomorrow += timedelta(days=1)
    need_until_ms = int(tomorrow.timestamp() * 1000)
    remain_min = (exp - now_ms) / 60000.0
    cover_min = (exp - need_until_ms) / 60000.0
    if cover_min > 0:
        log("token 剩余 %.0f 分钟，可覆盖到次日 %s（富余 %.0f 分钟），无需处理"
            % (remain_min, tomorrow.strftime("%m-%d %H:%M"), cover_min))
        return 0

    log("token 剩余 %.0f 分钟，撑不到次日 00:10（缺口 %.0f 分钟），开始无头刷新（OAuth）..."
        % (remain_min, -cover_min))
    baseline = exp
    ok, new_exp = refresh_via_oauth()
    if ok:
        log("无头刷新成功，新 token 有效期至 %s"
            % datetime.fromtimestamp(new_exp / 1000).strftime("%m-%d %H:%M"))
        return 0
    log("OAuth 无头刷新失败，改用兜底：拉起 GUI 客户端...")
    if not client_running():
        if not os.path.exists(CLIENT_EXE):
            log("找不到客户端 %s，无法兜底" % CLIENT_EXE)
            return 1
        try:
            subprocess.Popen([CLIENT_EXE], creationflags=0x00000008)  # DETACHED_PROCESS
        except Exception as e:
            log("拉起客户端失败：%s" % e)
            return 1
    else:
        log("客户端已在运行，等待其刷新...")
    exp2 = wait_refresh(baseline, 240)
    if exp2:
        log("GUI 兜底刷新成功，有效期至 %s"
            % datetime.fromtimestamp(exp2 / 1000).strftime("%m-%d %H:%M"))
        return 0
    log("刷新失败：00:05 签到脚本仍有 OAuth 自愈兜底；若也失败需手动打开客户端检查登录")
    return 1


if __name__ == "__main__":
    sys.exit(main())

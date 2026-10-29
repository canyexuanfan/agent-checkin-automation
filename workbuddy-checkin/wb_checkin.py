#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy 每日积分自动签到（Windows / macOS 通用，同一份脚本两边都能跑）

用法:
    Windows:  python  wb_checkin.py            # 执行签到
    macOS:    python3 wb_checkin.py
              python3 wb_checkin.py --status   # 只查状态，不签到
              python3 wb_checkin.py --verbose  # 详细输出

凭据自动按平台定位本机 WorkBuddy 客户端的 auth 文件:
    Windows  %LOCALAPPDATA%\\CodeBuddyExtension\\Data\\Public\\auth\\
    macOS    ~/Library/Application Support/CodeBuddyExtension/Data/Public/auth/
只在本地读取，不上传任何数据。

完整说明见同目录《自动领取workBuddy积分的脚本.md》。

---
出品：同城牛 · 阿may（2026-09-08）
免费供同城牛联盟成员、内部团队与学员个人自用，禁止任何形式的商用。
只读取本机 WorkBuddy 客户端已有的登录凭据调官方接口，不上传任何数据。
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE = "https://copilot.tencent.com"
STATUS_URL = BASE + "/billing/meter/checkin-status"
CHECKIN_URL = BASE + "/billing/meter/daily-checkin"

LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "checkin.log")


def auth_dir():
    """跨平台定位 WorkBuddy auth 目录。"""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser(
            r"~\AppData\Local"
        )
        return os.path.join(base, "CodeBuddyExtension", "Data", "Public", "auth")
    if sys.platform == "darwin":
        return os.path.expanduser(
            "~/Library/Application Support/CodeBuddyExtension/Data/Public/auth"
        )
    return os.path.expanduser("~/.config/CodeBuddyExtension/Data/Public/auth")


def log(msg):
    line = "[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# 可选的 token 信封解密助手：放在本脚本同目录即可，没有则只支持明文 token
DECRYPT_HELPER_DIR = os.path.dirname(os.path.abspath(__file__))


def _unwrap_token(value):
    """兼容新旧 auth 格式：明文字符串直接返回；{\"$wbEncrypted\":1,...} 信封自动解密。"""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if DECRYPT_HELPER_DIR not in sys.path:
            sys.path.insert(0, DECRYPT_HELPER_DIR)
        from wb_auth_decrypt import unwrap
        return unwrap(value)
    raise RuntimeError("无法识别的 accessToken 类型: %s" % type(value).__name__)


def load_auth_session():
    """读取本地 auth 文件，返回 (token, uid, nickname)。"""
    d = auth_dir()
    info = os.path.join(d, "workbuddy-desktop.info")

    if os.path.exists(info + ".logged-out"):
        raise RuntimeError("检测到 logout marker：WorkBuddy 已登出，请打开客户端重新登录")
    if not os.path.exists(info):
        raise RuntimeError("Auth 文件不存在：%s（请打开 WorkBuddy 客户端登录）" % info)

    with open(info, encoding="utf-8") as f:
        data = json.load(f)

    account = data.get("account") or {}
    auth = data.get("auth") or {}

    # 若顶层 account 不完整，退回 accounts 里 lastLogin=true 的那个
    if not auth.get("accessToken"):
        for acc in data.get("accounts", []):
            if acc.get("account", {}).get("lastLogin") and acc.get("auth", {}).get(
                "accessToken"
            ):
                account = acc["account"]
                auth = acc["auth"]
                break

    token = _unwrap_token(auth.get("accessToken"))
    uid = account.get("uid")
    if not token or not uid:
        raise RuntimeError("Auth 文件里没有可用的 accessToken/uid，请重新登录 WorkBuddy")

    expires_at = auth.get("expiresAt") or 0
    if expires_at and expires_at < time.time() * 1000:
        raise RuntimeError(
            "Token 已过期（%s），请打开 WorkBuddy 客户端让它自动刷新"
            % datetime.fromtimestamp(expires_at / 1000).strftime("%Y-%m-%d")
        )

    nickname = account.get("nickname")
    if not isinstance(nickname, str) or not nickname:
        nickname = "uid:%s" % uid
    return token, uid, nickname, expires_at


def make_request(url, token, uid, body=None, timeout=30):
    """发 POST，返回 (http_status, json_body)。非 2xx 也解析业务 JSON。"""
    payload = json.dumps(body if body is not None else {}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Content-Type", "application/json")
    req.add_header("X-User-Id", uid)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"code": -1, "msg": raw[:300]}


WBSWITCH_ACCOUNTS_PATH = os.path.expanduser(r"~\.wb-switch\accounts.json")


def _is_enterprise(acc):
    """企业账号没有签到机制（wb-switch 实测报“企业账号不支持该操作”），跳过。"""
    if acc.get("enterpriseId"):
        return True
    pr = acc.get("profile_raw") or {}
    return bool(pr.get("enterpriseId")) or pr.get("type") in ("ultimate", "enterprise")


def multi_account_checkin(args):
    """遍历 wb-switch 保存的全部账号逐个签到，与客户端当前登录状态无关。

    wb-switch (C:\\Users\\<user>\\.wb-switch\\accounts.json) 明文保存了每个
    曾登录过的账号的 access_token / uid / domain，正好作为多账号凭据源。
    任一账号失败不影响其它账号；全部成功返回 0。
    """
    if not os.path.exists(WBSWITCH_ACCOUNTS_PATH):
        log("多账号：未找到 wb-switch accounts.json，跳过多账号签到")
        return 0
    try:
        with open(WBSWITCH_ACCOUNTS_PATH, "r", encoding="utf-8") as f:
            accounts = json.load(f)
    except Exception as e:
        log("多账号：读取 accounts.json 失败：%s" % e)
        return 0

    rc = 0
    now_ms = time.time() * 1000
    for acc in accounts:
        nickname = acc.get("nickname") or "?"
        try:
            if _is_enterprise(acc):
                log("多账号 [%s]：企业账号没有签到机制，跳过" % nickname)
                continue
            token = acc.get("access_token") or (acc.get("auth_raw") or {}).get("accessToken")
            uid = acc.get("uid") or (acc.get("profile_raw") or {}).get("uid")
            if not token or not uid:
                log("多账号 [%s]：缺少 token/uid，跳过" % nickname)
                rc = rc or 1
                continue
            exp = acc.get("expiresAt")
            if exp and exp < now_ms:
                log("多账号 [%s]：token 已过期，等 wb-switch 刷新后再签（本次跳过）" % nickname)
                rc = rc or 1
                continue
            if args.verbose and exp:
                log("多账号 [%s]：token 剩余 %.1f 天" % (nickname, (exp - now_ms) / 86400000.0))

            # 先走官方主域 copilot.tencent.com；401/403 时退回账号自带 domain
            base = BASE
            code, res = make_request(STATUS_URL, token, uid)
            if code in (401, 403) and acc.get("domain"):
                base = "https://" + acc["domain"]
                code, res = make_request(base + "/billing/meter/checkin-status", token, uid)
            if code in (401, 403):
                log("多账号 [%s]：token 无效（HTTP %s），跳过" % (nickname, code))
                rc = rc or 1
                continue

            data = (res or {}).get("data") or {}
            if args.verbose:
                log(
                    "多账号 [%s] 状态：今日已签到=%s 连续=%s天"
                    % (nickname, data.get("today_checked_in"), data.get("streak_days"))
                )
            if args.status:
                continue

            code, res = make_request(base + "/billing/meter/daily-checkin", token, uid)
            biz = (res or {}).get("code")
            if biz == 0:
                d = res.get("data") or {}
                log(
                    "多账号 [%s] 签到成功：+%s 积分，连续 %s 天"
                    % (nickname, d.get("credit"), d.get("streak_days"))
                )
            elif biz == 10001 or "已签到" in str(res.get("msg", "")):
                log("多账号 [%s]：今天已签到过了，视为成功" % nickname)
            elif code in (401, 403):
                log("多账号 [%s]：签到失败 HTTP %s，token 无效" % (nickname, code))
                rc = rc or 1
            else:
                log(
                    "多账号 [%s]：签到失败 HTTP %s code=%s msg=%s"
                    % (nickname, code, biz, res.get("msg"))
                )
                rc = rc or 1
        except Exception as e:
            log("多账号 [%s]：异常 %s: %s" % (nickname, type(e).__name__, e))
            rc = rc or 1
    return rc


def check_current_account(args):
    """签客户端当前登录的账号（读 auth 文件）。返回 0 成功 / 1 失败 / 2 需重新登录。"""
    try:
        token, uid, nickname, expires_at = load_auth_session()
    except RuntimeError as e:
        log("失败：%s" % e)
        return 2
    except Exception as e:
        # 权限/文件缺失等：不阻断后面的多账号流程（多账号不依赖 auth 文件）
        log("当前登录账号读取失败（%s: %s），继续多账号签到" % (type(e).__name__, e))
        return 2

    left_days = (expires_at / 1000 - time.time()) / 86400 if expires_at else 0
    if args.verbose:
        plat = {"win32": "Windows", "darwin": "macOS"}.get(sys.platform, sys.platform)
        log("平台：%s  凭据目录：%s" % (plat, auth_dir()))
        log("账号：%s  token 剩余 %.1f 天" % (nickname, left_days))
    if 0 < left_days < 7:
        log("提醒：token 只剩 %.1f 天，记得打开一次 WorkBuddy 客户端刷新" % left_days)

    # 查状态
    code, res = make_request(STATUS_URL, token, uid)
    if args.verbose:
        log("状态响应 HTTP %s：%s" % (code, json.dumps(res, ensure_ascii=False)))

    if code in (401, 403):
        log("失败：HTTP %s，Token 无效或权限不足，请重新登录 WorkBuddy" % code)
        return 2

    data = (res or {}).get("data") or {}
    if res.get("code") == 0:
        log(
            "当前状态：今日已签到=%s  连续=%s天  今日积分=%s  总积分=%s"
            % (
                data.get("today_checked_in"),
                data.get("streak_days"),
                data.get("today_credit"),
                data.get("total_credits"),
            )
        )
    else:
        log("状态查询返回：code=%s msg=%s" % (res.get("code"), res.get("msg")))

    if args.status:
        return 0

    # 执行签到
    code, res = make_request(CHECKIN_URL, token, uid)
    if args.verbose:
        log("签到响应 HTTP %s：%s" % (code, json.dumps(res, ensure_ascii=False)))

    biz = (res or {}).get("code")
    if biz == 0:
        d = res.get("data") or {}
        log(
            "签到成功：+%s 积分，连续 %s 天"
            % (d.get("credit"), d.get("streak_days"))
        )
        return 0
    if biz == 10001 or "已签到" in str(res.get("msg", "")):
        log("今天已签到过了（%s），视为成功" % res.get("msg"))
        return 0
    if code in (401, 403):
        log("签到失败：HTTP %s，Token 无效，请重新登录 WorkBuddy" % code)
        return 2

    log("签到失败：HTTP %s code=%s msg=%s" % (code, biz, res.get("msg")))
    return 1


def main():
    ap = argparse.ArgumentParser(description="WorkBuddy 每日积分签到（支持多账号）")
    ap.add_argument("--status", action="store_true", help="只查询签到状态，不签到")
    ap.add_argument("--verbose", "-v", action="store_true", help="打印完整响应")
    args = ap.parse_args()

    # 一、客户端当前登录账号（auth 文件）
    rc_current = check_current_account(args)
    # 二、wb-switch 保存的全部账号（与登录状态无关，个人号逐个签、企业号跳过）
    rc_multi = multi_account_checkin(args)

    return rc_current or rc_multi


if __name__ == "__main__":
    sys.exit(main())

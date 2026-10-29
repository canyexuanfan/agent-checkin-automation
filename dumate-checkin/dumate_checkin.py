#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
百度搭子(DuMate) 每日积分自动签到（Windows 专用，本地读取本机登录态）

思路与 wb-checkin / minimax-checkin 一致：只读取本机桌面端已保存的登录凭据，
调官方接口把每日积分领了。凭据是 DuMate(Electron) 自己持久化的加密 cookie 罐，
密钥就在旁边的 .cookie-key 文件里（AES-256-GCM，布局 base64(iv(12)+tag(16)+ciphertext)），
本脚本在内存中解密后直接请求官方域名，不会上传任何数据。

凭据位置（本机实测）：
    %APPDATA%\\qianfan-desktop-app\\auth.json   -> accountProfiles[].encryptedCookies
    %APPDATA%\\qianfan-desktop-app\\.cookie-key -> 32 字节 AES-256-GCM 密钥
    （顶层 "cookies" 字段是当前活跃账号的镜像，优先用它）

接口（DuMate resources/app.asar 内 renderer 代码逆向确认）：
    GET  https://console.bce.baidu.com/api/dumate/points/loginBonusInfo   # 状态（只读）
    POST https://console.bce.baidu.com/api/dumate/points/loginBonus       # 领取每日登录积分

关键请求头：Cookie + csrftoken(取 bce-user-info cookie 值去引号) + X-Requested-With + Origin + Referer

用法:
    python dumate_checkin.py            # 查状态 + 签到
    python dumate_checkin.py --status   # 只查状态（只读，不签到）
    python dumate_checkin.py --verbose  # 打印完整响应

幂等：同一天重复运行只会返回已领取，不会重复发积分。
依赖：pip install cryptography（AES-GCM 解密）
"""

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

HOST = "https://console.bce.baidu.com"
INFO_PATH = "/api/dumate/points/loginBonusInfo"
CLAIM_PATH = "/api/dumate/points/loginBonus"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36")
REFERER = "https://console.bce.baidu.com/dumate/enterprise/console"

LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dumate-checkin.log")


def log(msg):
    line = "[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ----------------------------------------------------------------------------
# 凭据读取与解密
# ----------------------------------------------------------------------------
def app_dir():
    base = os.environ.get("APPDATA") or os.path.expanduser(r"~\AppData\Roaming")
    return os.path.join(base, "qianfan-desktop-app")


def load_encrypted_cookies():
    p = os.path.join(app_dir(), "auth.json")
    if not os.path.exists(p):
        raise RuntimeError("找不到 DuMate 登录态：%s（请先打开一次 DuMate 桌面端并登录）" % p)
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    # 顶层 cookies 是活跃账号镜像；没有就用第一个带 encryptedCookies 的 profile
    enc = data.get("cookies")
    if not enc:
        for prof in data.get("accountProfiles") or []:
            if prof.get("encryptedCookies"):
                enc = prof["encryptedCookies"]
                break
    if not enc:
        raise RuntimeError("auth.json 中没有 encryptedCookies，请打开 DuMate 桌面端重新登录")
    return enc


def decrypt_cookies(enc):
    key_path = os.path.join(app_dir(), ".cookie-key")
    if not os.path.exists(key_path):
        raise RuntimeError("找不到密钥文件 .cookie-key（%s）" % key_path)
    with open(key_path, "rb") as f:
        key = f.read()
    raw = base64.b64decode(enc)
    iv, tag, ct = raw[:12], raw[12:28], raw[28:]
    pt = AESGCM(key).decrypt(iv, ct + tag, None)
    cookies = json.loads(pt.decode("utf-8"))
    if not isinstance(cookies, list):
        raise RuntimeError("cookie 载荷不是数组，格式可能已变更")
    return cookies


def build_cookie_header(cookies, host="console.bce.baidu.com"):
    parts = []
    for c in cookies:
        domain = (c.get("domain") or "").lstrip(".")
        if domain and (domain in host or host.endswith("." + domain) or host == domain):
            v = c.get("value") or ""
            if v:
                parts.append("%s=%s" % (c["name"], v))
    return "; ".join(parts)


def extract_csrf(cookies):
    for c in cookies:
        if c.get("name") == "bce-user-info" and c.get("value"):
            return c["value"].strip('"')
    return ""


def call_api(path, cookie_hdr, csrf, method="GET", body=None):
    headers = {
        "Cookie": cookie_hdr,
        "User-Agent": UA,
        "Referer": REFERER,
        "Accept": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": HOST,
        "csrftoken": csrf,
    }
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(HOST + path, data=data, method=method, headers=headers)
    try:
        r = urllib.request.urlopen(req, timeout=20)
        return r.status, json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8", "replace"))
        except Exception:
            return e.code, {}


# ----------------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true", help="只查状态，不签到")
    ap.add_argument("--verbose", action="store_true", help="打印完整响应")
    args = ap.parse_args()

    enc = load_encrypted_cookies()
    cookies = decrypt_cookies(enc)
    cookie_hdr = build_cookie_header(cookies)
    csrf = extract_csrf(cookies)
    if not cookie_hdr:
        raise RuntimeError("没有可用于 console.bce.baidu.com 的 cookie，请打开 DuMate 重新登录")
    if not csrf:
        raise RuntimeError("找不到 bce-user-info cookie（csrftoken 来源），请打开 DuMate 重新登录")

    code, info = call_api(INFO_PATH, cookie_hdr, csrf)
    if args.verbose:
        log("状态响应 HTTP %s：%s" % (code, json.dumps(info, ensure_ascii=False)))
    if code != 200 or info.get("code") != 0:
        log("状态查询失败 HTTP %s：%s" % (code, json.dumps(info, ensure_ascii=False)[:200]))
        return 2
    result = info.get("result") or {}
    days = result.get("signInDays") or []
    log("当前状态：累计签到 %s 天，累计领取 %s 积分，今日%s"
        % (len(days), result.get("totalPoints"), "已签" if result.get("hasIssued") else "未签"))
    if args.status:
        return 0

    code, resp = call_api(CLAIM_PATH, cookie_hdr, csrf, method="POST")
    if args.verbose:
        log("签到响应 HTTP %s：%s" % (code, json.dumps(resp, ensure_ascii=False)))
    if code == 200 and resp.get("code") == 0 and resp.get("success"):
        log("签到成功（每日登录积分已入账/今日已领取过，幂等不重复发）")
        return 0
    msg = ""
    m = resp.get("message")
    if isinstance(m, dict):
        msg = m.get("global") or ""
    elif isinstance(m, str):
        msg = m
    if "过期" in msg or "重新登录" in msg:
        log("失败：%s —— 请打开 DuMate 桌面端重新登录一次，脚本下次运行即可恢复" % msg)
        return 3
    log("签到失败 HTTP %s：%s" % (code, json.dumps(resp, ensure_ascii=False)[:200]))
    return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as e:
        log("失败：%s" % e)
        raise SystemExit(2)

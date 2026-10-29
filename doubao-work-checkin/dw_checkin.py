# -*- coding: utf-8 -*-
"""豆包工作自动签到（纯 HTTP，无客户端/无浏览器）

核心纪律（防风控）：
  1. 只读探测 landing/get（等同打开面板，零风险）先行
  2. chosen_type=2 已签到 / 0 无活动投放 → 绝不发 checkin
  3. chosen_type=1 才签到，且每账号每天最多 1 次尝试（状态文件硬限制）
  4. checkin 失败不做任何重试
"""
import json, os, sys, time, urllib.parse, urllib.request
from datetime import datetime, date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from abogus import ABogus

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(BASE_DIR, 'dw_checkin.log')
STATE = os.path.join(BASE_DIR, 'dw_state.json')
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36 SamanthaDoubaoWork/2.32.14'

# 账号清单：name -> cookie json 文件（用 decrypt_all_profiles.py 从豆包工作客户端提取）
# cookie 文件不进 git（见 .gitignore）；多账号就在列表里加条目
ACCOUNTS = [
    {'name': 'default', 'cookies': os.path.join(BASE_DIR, 'cookies_default.json')},
]

# 设备参数：复制 device.example.json 为 device.json 并填入本机值
# （从豆包工作客户端任意一个带签名的 web 请求 URL query 里抄 web_id / tea_uuid / web_tab_id）
try:
    with open(os.path.join(BASE_DIR, 'device.json'), encoding='utf-8') as _f:
        _DEV = json.load(_f)
except Exception:
    _DEV = {}

BASE_PARAMS = [
    ('version_code', '20800'), ('language', 'zh'), ('device_platform', 'web'),
    ('doubao_device_platform', 'web'), ('aid', '497858'), ('real_aid', '497858'),
    ('pkg_type', 'release_version'), ('pc_version', '2.32.14'),
    ('doubao_pc_version', '5.72.91'), ('web_id', _DEV.get('web_id', '')),
    ('tea_uuid', _DEV.get('tea_uuid', '')), ('samantha_web', '1'),
    ('web_platform', 'work_desktop'), ('use-olympus-account', '1'),
    ('web_tab_id', _DEV.get('web_tab_id', '')),
    ('runtime_version', '5.72.91'), ('runtime', 'web'),
]
SCENE = 'pc_incentive_task_mvp_v3'


def log(msg):
    line = '[%s] %s' % (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), msg)
    print(line)
    with open(LOG, 'a', encoding='utf-8') as f:
        f.write(line + '\n')


def load_state():
    try:
        return json.load(open(STATE, encoding='utf-8'))
    except Exception:
        return {}


def api_call(path, cookies, scene_body=True):
    """带离线签名的 API 调用。返回 (http_ok, parsed_dict)"""
    cookie_pairs = cookies if isinstance(cookies, dict) else {c['name']: c['value'] for c in cookies}
    ms_token = cookie_pairs.get('msToken', '')
    params = BASE_PARAMS + [('msToken', ms_token)]
    qs = urllib.parse.urlencode(params)
    a_bogus = ABogus(user_agent=UA).generate_a_bogus(dict(params))
    url = 'https://www.doubao.com' + path + '?' + qs + '&a_bogus=' + urllib.parse.quote(a_bogus, safe='')
    cookie_str = '; '.join(f'{k}={v}' for k, v in cookie_pairs.items())
    body = json.dumps({'scene': SCENE}).encode() if scene_body else None
    req = urllib.request.Request(url, data=body, method='POST', headers={
        'User-Agent': UA,
        'Content-Type': 'application/json',
        'Agw-Js-Conv': 'str',
        'Referer': 'https://www.doubao.com/pc/agent-explore?scroll_to=tasks',
        'Cookie': cookie_str,
    })
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        return True, json.loads(resp.read().decode())
    except Exception as e:
        log('  请求异常: %s' % e)
        return False, {}


def process_account(acct, state):
    name = acct['name']
    today = date.today().isoformat()
    cookies = json.load(open(acct['cookies'], encoding='utf-8'))

    # 只读探测
    ok, d = api_call('/alice/ug/campaign/landing/get', cookies)
    if not ok:
        log('[%s] 查询失败（网络/cookie）' % name)
        return 1
    if d.get('code') != 0:
        log('[%s] landing/get 业务错误 code=%s msg=%s' % (name, d.get('code'), d.get('msg')))
        return 1
    data = d.get('data')
    if not data:
        log('[%s] landing/get data 为空（签名/cookie 失效，需重新解密 cookie）' % name)
        return 1

    chosen = data.get('chosen_type')
    if chosen == 2:
        log('[%s] 已签到（今日已完成）' % name)
        return 0
    if chosen == 0:
        log('[%s] 无活动投放（服务端未给该账号投放签到活动，风控冷却中），不尝试签到' % name)
        return 2
    if chosen != 1:
        log('[%s] 未知 chosen_type=%s，不尝试签到' % (name, chosen))
        return 2

    # 绿灯：签到页已投放。检查每日尝试限制
    rec = state.setdefault(name, {})
    if rec.get('last_attempt') == today:
        log('[%s] 今日签到页已投放，但今天已尝试过签到（每日1次硬限制），跳过' % name)
        return 2

    log('[%s] 签到页已投放（chosen_type=1），执行每日一次签到...' % name)
    rec['last_attempt'] = today
    json.dump(state, open(STATE, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)

    ok2, d2 = api_call('/alice/ug/campaign/landing/checkin', cookies)
    if ok2 and d2.get('code') == 0 and (d2.get('data') or {}).get('checked_in'):
        log('[%s] 签到成功！+200 额度' % name)
        return 0
    log('[%s] 签到失败 code=%s msg=%s（不重试，等明天只读探测绿灯后再试）' % (
        name, d2.get('code') if ok2 else 'NETERR', d2.get('msg', '')))
    return 1


def main():
    state = load_state()
    rc = 0
    for acct in ACCOUNTS:
        try:
            r = process_account(acct, state)
            rc = max(rc, r)
        except Exception as e:
            log('[%s] 异常: %s' % (acct['name'], e))
            rc = 1
    return rc


if __name__ == '__main__':
    sys.exit(main())

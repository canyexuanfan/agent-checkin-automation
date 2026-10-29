# -*- coding: utf-8 -*-
"""从 ima.copilot 客户端本地 Cookie 库提取签到凭证 -> credentials.json
原始字节复制读取（客户端运行中也能读），v10 AES-GCM 解密，剥离 32 字节完整性前缀。
"""
import base64, ctypes, ctypes.wintypes, json, os, sqlite3, sys, tempfile
from datetime import datetime

UD = os.path.join(os.environ['LOCALAPPDATA'], 'ima.copilot', 'User Data')
WANTED = ("IMA-UID", "UID-TYPE", "IMA-GUID", "IMA-TOKEN", "IMA-REFRESH-TOKEN")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'credentials.json')


class DATA_BLOB(ctypes.Structure):
    _fields_ = [('cbData', ctypes.wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_char))]


def dpapi(data):
    buf = ctypes.create_string_buffer(data, len(data))
    bi = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    bo = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(bi), None, None, None, None, 0, ctypes.byref(bo)):
        raise OSError('DPAPI 解密失败')
    out = ctypes.string_at(bo.pbData, bo.cbData)
    ctypes.windll.kernel32.LocalFree(bo.pbData)
    return out


def main():
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    with open(os.path.join(UD, 'Local State'), 'r', encoding='utf-8') as f:
        enc_key = base64.b64decode(json.load(f)['os_crypt']['encrypted_key'])
    aes_key = dpapi(enc_key[5:])

    cookies = {}
    for rel in ('Default/Extension Cookies', 'Default/Network/Cookies', 'Default/Cookies'):
        src = os.path.join(UD, rel.replace('/', os.sep))
        if not os.path.exists(src):
            continue
        try:
            with open(src, 'rb') as fp:
                data = fp.read()
        except OSError as e:
            print('[skip] %s 读取失败: %s' % (rel, e))
            continue
        tmp = os.path.join(tempfile.gettempdir(), 'ima_cred_tmp.db')
        with open(tmp, 'wb') as fp:
            fp.write(data)
        con = sqlite3.connect(tmp)
        try:
            for name, ev in con.execute('SELECT name, encrypted_value FROM cookies'):
                if name not in WANTED or not ev:
                    continue
                if ev[:3] in (b'v10', b'v11'):
                    plain = AESGCM(aes_key).decrypt(ev[3:15], ev[15:], None)
                    cookies[name] = plain[32:].decode('utf-8', 'replace')  # 剥离完整性前缀
        finally:
            con.close()
        if cookies:
            print('[ok] %s -> %d 个字段' % (rel, len(cookies)))
            break

    missing = [k for k in ('IMA-UID', 'UID-TYPE', 'IMA-GUID', 'IMA-REFRESH-TOKEN') if not cookies.get(k)]
    if missing:
        sys.exit('[错误] 缺少字段: %s' % missing)
    cred = {
        'ima_uid': cookies['IMA-UID'],
        'uid_type': cookies['UID-TYPE'],
        'ima_guid': cookies['IMA-GUID'],
        'refresh_token': cookies['IMA-REFRESH-TOKEN'],
        'saved_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }
    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump(cred, f, ensure_ascii=False, indent=1)
    print('完成 ->', OUT)
    for k, v in cred.items():
        print('  %s = %s...%s (len=%d)' % (k, v[:6], v[-4:], len(v)) if len(v) > 12 else '  %s = %s' % (k, v))


if __name__ == '__main__':
    main()

# -*- coding: utf-8 -*-
"""解密豆包工作全部 Profile 的 cookie（客户端需已关闭）。
v10 格式：AES-256-GCM(key=LocalState encrypted_key 经 DPAPI)，
明文 = 前 32 字节设备签名 + 后段 ASCII hex 真值。
输出：cookies_default.json / cookies_profile1.json / cookies_profile2.json
"""
import base64, ctypes, ctypes.wintypes as wt, glob, hashlib, json, os, shutil, sqlite3, struct, sys, tempfile

UD = os.path.join(os.environ.get('LOCALAPPDATA', ''), 'DoubaoWork', 'User Data')
OUT = r'C:/tools/doubao-work-checkin'
PROFILES = [('Default', 'cookies_default.json'), ('Profile 1', 'cookies_profile1.json'), ('Profile 2', 'cookies_profile2.json')]


def dpapi_decrypt(data):
    class BLOB(ctypes.Structure):
        _fields_ = [('cbData', wt.DWORD), ('pbData', ctypes.POINTER(ctypes.c_char)),
                    ('KeySize', wt.DWORD), ('KeyData', ctypes.POINTER(ctypes.c_char))]
    buf = ctypes.create_string_buffer(data, len(data))
    bi = BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)), 0, None)
    out = ctypes.c_void_p(); out_len = wt.DWORD()
    if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(bi), None, None, None, None, 0, ctypes.byref(out)):
        raise OSError('DPAPI failed')
    try:
        return ctypes.string_at(out.value, out_len.value)
    finally:
        ctypes.windll.kernel32.LocalFree(out)


def get_aes_key():
    ls = json.load(open(os.path.join(UD, 'Local State'), encoding='utf-8'))
    enc = base64.b64decode(ls['os_crypt']['encrypted_key'])
    assert enc[:5] == b'DPAPI', enc[:5]
    return dpapi_decrypt(enc[5:])


def aesgcm_decrypt(key, enc):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce, ct = enc[3:15], enc[15:]
    plain = AESGCM(key).decrypt(nonce, ct, None)
    if plain[:32] and len(plain) > 32:
        # 前 32 字节设备签名 + ASCII hex 真值
        hexpart = plain[32:]
        try:
            return bytes.fromhex(hexpart.decode('ascii')).decode('utf-8', 'ignore')
        except Exception:
            return plain[32:].decode('utf-8', 'ignore')
    return plain.decode('utf-8', 'ignore')


def copy_db(src, dst):
    for suffix in ('', '-wal', '-journal'):
        s = src + suffix
        if os.path.exists(s):
            shutil.copyfile(s, dst + suffix)


def main():
    key = get_aes_key()
    print('AES key ok, len=%d' % len(key))
    tmpdir = tempfile.mkdtemp()
    for prof, outname in PROFILES:
        src = os.path.join(UD, prof, 'Network', 'Cookies')
        if not os.path.exists(src):
            src = os.path.join(UD, prof, 'Cookies')
        if not os.path.exists(src):
            print(f'{prof}: 无 Cookie 库，跳过'); continue
        tmp = os.path.join(tmpdir, prof.replace(' ', '_') + '_Cookies')
        try:
            copy_db(src, tmp)
            con = sqlite3.connect(tmp)
            rows = con.execute(
                "SELECT host_key, name, encrypted_value, path, is_secure, is_httponly, "
                "expires_utc FROM cookies WHERE host_key LIKE '%doubao%'").fetchall()
            con.close()
        except Exception as e:
            print(f'{prof}: 读取失败 {e}'); continue
        cookies = {}
        for host, name, enc, path, secure, httponly, exp in rows:
            if not enc:
                continue
            try:
                if enc[:3] == b'v10':
                    cookies[name] = aesgcm_decrypt(key, enc)
                elif enc[:3] == b'v20':
                    cookies[name] = '(v20 app-bound，暂不支持)'
                else:
                    cookies[name] = enc.decode('utf-8', 'ignore')
            except Exception as e:
                cookies[name] = f'(解密失败: {e})'
        json.dump(cookies, open(os.path.join(OUT, outname), 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
        keynames = [n for n in cookies if n in ('sid', 'sid_tt', 'sessionid', 'msToken', 'uid_tt', 'sse_sid')]
        print(f'{prof}: {len(cookies)} 条 doubao cookie → {outname}；关键: {keynames}')
        # 打印 sid 指纹用于区分账号
        sid = cookies.get('sid') or cookies.get('sid_tt') or ''
        if sid:
            print(f'   {prof} sid={sid[:10]}...{sid[-6:]} len={len(sid)}')


if __name__ == '__main__':
    main()

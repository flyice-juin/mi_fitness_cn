# -*- coding: utf-8 -*-
"""小米运动健康 — 扫码登录提取 token(免密码、免邮箱/短信验证码)

用途:MiFitnessHass 集成的「Manual tokens」登录方式需要一组账号凭证,
本脚本用小米账号「扫码登录」拿齐这些字段(国内账号绕开邮箱验证码死路)。

流程:
  1. 向 account.xiaomi.com 申请二维码,自动在浏览器打开
  2. 用手机「小米账号 App / 系统扫一扫」扫码并确认
  3. 脚本轮询到登录成功,提取 ssecurity/serviceToken/userId/cUserId/passToken
  4. 实测健康数据接口验证 token 是否可用(并测试裸域/cn 子域哪个通)
  5. 保存 token.json,并按 MiFitnessHass 表单字段名打印出来

依赖:仅 requests  (pip install requests)
"""

import base64
import hashlib
import json
import os
import random
import sys
import time
import webbrowser
from urllib.parse import parse_qs, urlparse

try:
    import requests
except ImportError:
    print("缺少 requests 库,请先执行:  py -m pip install requests")
    sys.exit(1)

try:  # Windows 控制台默认 GBK,防止打印异常
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ── 常量(与小米运动健康 CN 版 App 同源的登录体系)─────────────────────────
SID = "miothealth"
STS_URL = "https://sts-hlth.io.mi.com/healthapp/sts"
QR_LOGIN_URL = "https://account.xiaomi.com/longPolling/loginUrl"
# 国内 App 走裸域;MiFitnessHass 拼区域子域(cn.hlth.io.mi.com),两个都测
API_BASES = ["https://hlth.io.mi.com", "https://cn.hlth.io.mi.com"]

# 登录用 UA = 小米运动健康 CN 版(com.mi.health)App 指纹
UA_LOGIN = (
    "Dalvik/2.1.0 (Linux; U; Android 12; V2284A Build/ab8c0d1.1) "
    "APP/mi.health APPV/353001 MK/VjIyODRB SDKV/5.3.0.release.68 "
    "CPN/com.mi.health PassportSDK/"
)
# 数据接口 UA(亲友SDK 实测可用形态)
UA_API = "Android-12-3.53.1-vivo-V2284A"

TOKEN_FILE = "token.json"
MAX_WAIT = 300  # 扫码等待总时长(秒)


def parse_mi(text: str) -> dict:
    """解析小米 API 带 &&&START&&& 前缀的 JSON 响应。"""
    body = text
    if body.startswith("&&&START&&&"):
        body = body[len("&&&START&&&"):]
    return json.loads(body)


def ms() -> str:
    return str(int(time.time() * 1000))


# ── Step 1+2+3: 扫码登录 ────────────────────────────────────────────────────
def qr_login() -> dict:
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA_LOGIN,
        "Content-Type": "application/x-www-form-urlencoded",
    })
    # 设备指纹 cookie(httpx 的 cookies.set 无 domain 等价于发给所有主机)
    s.cookies.set("deviceId", "an_" + os.urandom(16).hex())
    device_id = s.cookies.get("deviceId")

    # 1) 申请二维码
    r = s.get(QR_LOGIN_URL, params={
        "_qrsize": "480",
        "qs": f"%3Fsid%3D{SID}%26_json%3Dtrue",
        "callback": STS_URL,
        "_hasLogo": "false",
        "sid": SID,
        "serviceParam": "",
        "_locale": "zh_CN",
        "_dc": ms(),
    }, timeout=15)
    qr = parse_mi(r.text)
    qr_img, login_url, lp = qr.get("qr", ""), qr.get("loginUrl", ""), qr.get("lp", "")
    if not qr_img or not lp:
        print("[x] 获取二维码失败,响应如下:")
        print(r.text[:500])
        sys.exit(1)

    print("[1] 二维码已生成,请用手机「小米账号 App / 系统相机扫一扫」扫码并在手机上确认登录")
    print(f"    二维码图片: {qr_img}")
    if login_url:
        print(f"    备选(手机浏览器直接打开): {login_url}")
    try:
        webbrowser.open(qr_img)
        print("    (已自动在浏览器打开二维码,若没弹出请手动打开上面的链接)")
    except Exception:
        pass

    # 2) 长轮询等扫码结果
    print("[2] 等待扫码确认...(最长 %d 秒)" % MAX_WAIT)
    start = time.time()
    data = {}
    while time.time() - start < MAX_WAIT:
        try:
            pr = s.get(lp, timeout=65)
        except requests.Timeout:
            continue
        except requests.RequestException as e:
            print(f"    轮询网络错误,重试: {e}")
            time.sleep(2)
            continue
        if pr.status_code != 200:
            time.sleep(2)
            continue
        try:
            data = parse_mi(pr.text)
        except Exception:
            continue
        if data.get("ssecurity"):
            break
        print(f"    轮询返回(非最终态): {pr.text[:120]}")
        time.sleep(2)
    else:
        print("[x] 扫码超时,请重跑本脚本")
        sys.exit(1)

    # 3) 提取凭证 + 跟随 location 拿 serviceToken
    creds = {
        "user_id": str(data.get("userId", "")),
        "pass_token": data.get("passToken", ""),
        "c_user_id": data.get("cUserId", ""),
        "ssecurity": data["ssecurity"],
        "service_token": "",
        "device_id": device_id,
    }
    location = data.get("location", "")
    if location:
        lr = s.get(location, timeout=15, allow_redirects=True)
        token = lr.cookies.get("serviceToken")
        if not token:
            token = s.cookies.get("serviceToken", "")
        if not token:  # 兜底:serviceToken 可能挂在重定向链的查询参数里
            try:
                qs = parse_qs(urlparse(lr.url).query)
                token = qs.get("serviceToken", [""])[0]
            except Exception:
                token = ""
        if not token:
            print("[!] 未能从 STS 重定向拿到 serviceToken(登录态本身已成功,token.json 仍会保存)")
        creds["service_token"] = token
    else:
        print("[!] 登录响应未返回 location,无法取 serviceToken")

    # 4) STS 设备登记(亲友SDK 同款,非致命)
    try:
        er = s.get(STS_URL, params={
            "d": device_id, "ticket": "0", "pwd": "0",
            "p_ts": ms(), "fid": "0", "p_lm": "2", "p_ur": "CN",
        }, timeout=15)
        print(f"[3] STS 登记: {er.text[:60]}")
    except Exception as e:
        print(f"[3] STS 登记失败(不影响): {e}")

    return creds


# ── RC4 + 签名(与 MiFitnessHass api.py 完全同构,纯标准库实现)───────────────
def rc4(key: bytes, data: bytes, drop: int = 0) -> bytes:
    S = list(range(256))
    j = 0
    for i in range(256):
        j = (j + S[i] + key[i % len(key)]) & 0xFF
        S[i], S[j] = S[j], S[i]
    i = j = 0
    out = bytearray()
    total = drop + len(data)
    for idx in range(total):
        i = (i + 1) & 0xFF
        j = (j + S[i]) & 0xFF
        S[i], S[j] = S[j], S[i]
        k = S[(S[i] + S[j]) & 0xFF]
        if idx >= drop:
            out.append(data[idx - drop] ^ k)
    return bytes(out)


def gen_nonce() -> str:
    b = (random.getrandbits(64) - 2 ** 63).to_bytes(8, "big", signed=True)
    b += int(time.time() * 1000 / 60000).to_bytes(4, "big")
    return base64.b64encode(b).decode()


def signed_nonce(ssecurity: str, nonce: str) -> str:
    h = hashlib.sha256()
    h.update(base64.b64decode(ssecurity))
    h.update(base64.b64decode(nonce))
    return base64.b64encode(h.digest()).decode()


def fc4_sign(method: str, url: str, params: dict, security: str) -> str:
    path = urlparse(url).path
    parts = [method.upper(), path]
    for k in sorted(params):
        parts.append(f"{k}={params[k]}")
    parts.append(security)
    return base64.b64encode(hashlib.sha1("&".join(parts).encode("utf-8")).digest()).decode()


def rc4_apply(key_b64: str, text: str) -> str:
    enc = rc4(base64.b64decode(key_b64), text.encode("utf-8"), drop=1024)
    return base64.b64encode(enc).decode()


def rc4_undo(key_b64: str, payload_b64: str) -> bytes:
    raw = base64.b64decode(payload_b64)
    return rc4(base64.b64decode(key_b64), raw, drop=1024)


def verify_api(creds: dict) -> list:
    """对两个候选域名各发一次签名 GET,返回可用域名列表。"""
    ok_bases = []
    for base in API_BASES:
        host = urlparse(base).netloc
        s = requests.Session()
        s.headers.update({"User-Agent": UA_API, "HandleParams": "true", "region_tag": "cn"})
        for name, val in (("userId", creds["user_id"]), ("cUserId", creds["c_user_id"]),
                          ("serviceToken", creds["service_token"]), ("locale", "zh_CN")):
            s.cookies.set(name, val, domain=host)

        url = f"{base}/app/v1/data/get_fitness_data_by_watermark"
        nonce = gen_nonce()
        snonce = signed_nonce(creds["ssecurity"], nonce)
        data_str = json.dumps({"userid": creds["user_id"], "limit": 1, "watermark": 0},
                              separators=(",", ":"))
        rc4_hash = fc4_sign("GET", url, {"data": data_str}, snonce)
        enc_data = rc4_apply(snonce, data_str)
        enc_hash = rc4_apply(snonce, rc4_hash)
        params = {
            "data": enc_data,
            "rc4_hash__": enc_hash,
            "signature": fc4_sign("GET", url, {"data": enc_data, "rc4_hash__": enc_hash}, snonce),
            "ssecurity": creds["ssecurity"],
            "_nonce": nonce,
        }
        try:
            r = s.get(url, params=params, timeout=20)
        except Exception as e:
            print(f"    {host}: 请求异常 {e}")
            continue
        if r.status_code == 401:
            print(f"    {host}: HTTP 401(token 无效或域名不匹配)")
            continue
        if r.status_code != 200:
            print(f"    {host}: HTTP {r.status_code} {r.text[:80]}")
            continue
        try:
            result = json.loads(rc4_undo(snonce, r.text.strip()))
        except Exception as e:
            print(f"    {host}: 解密失败 {e}(响应前 60 字符: {r.text[:60]})")
            continue
        if result.get("code") == 0:
            n = len((result.get("result") or {}).get("data_list") or [])
            print(f"    {host}: 通过 (code=0, 返回 {n} 条数据)")
            ok_bases.append(base)
        else:
            print(f"    {host}: code={result.get('code')} {str(result)[:120]}")
    return ok_bases


def main():
    print("=" * 60)
    print("小米运动健康 扫码登录 → 提取 MiFitnessHass 所需 token")
    print("=" * 60)
    creds = qr_login()

    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        json.dump(creds, f, ensure_ascii=False, indent=2)
    print(f"[4] 凭证已保存到 {os.path.abspath(TOKEN_FILE)}")

    print("[5] 实测健康数据接口...")
    ok = verify_api(creds) if creds["service_token"] else []
    if not ok:
        print("    (没有域名通过 —— 不影响 token.json 保存,但集成里可能要再排查)")

    print()
    print("=" * 60)
    print("按 MiFitnessHass「Manual tokens」表单逐项填写:")
    print("=" * 60)
    print(f"  Region        : cn")
    print(f"  User ID       : {creds['user_id']}")
    print(f"  C User ID     : {creds['c_user_id']}")
    print(f"  Ssecurity     : {creds['ssecurity']}")
    print(f"  Service Token : {creds['service_token']}")
    print(f"  Pass Token    : {creds['pass_token']}")
    print(f"  Phone ID      : (留空)")
    if ok:
        worked = "、".join(urlparse(b).netloc for b in ok)
        print(f"  实测可用域名   : {worked}")
    print()
    print("填完提交即可。token.json 请妥善保管,等同账号密码,不要外传。")


if __name__ == "__main__":
    main()

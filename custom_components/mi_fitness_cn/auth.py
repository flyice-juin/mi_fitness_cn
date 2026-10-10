# -*- coding: utf-8 -*-
"""小米账号认证 —— 浏览器链接登录(免密码/免验证码/免手机)。

登录哲学:不在代码里模拟 App 登录接口。小米对"陌生设备模拟客户端"
层层设卡(短信/图形码/设备信任),程序硬闯是死路;而浏览器登录是小米
为人类设计的正门 —— 万一触发风控,验证就在小米自己的页面里交互式完成。

流程(设备流登录,类似电视上登 YouTube):
  1. GET  /longPolling/loginUrl (sid=miothealth) → {qr, loginUrl, lp, timeout}
  2. 用户在电脑浏览器打开 loginUrl → 登录(如需验证在页面内完成)→ 确认
  3. GET  {lp} 长轮询 → 登录成功返回 ssecurity/userId/passToken/cUserId/location
  4. GET  {location} (STS) → serviceToken cookie

passToken 之后由 coordinator 在 serviceToken 过期时静默换新
(try_silent_token_refresh),正常情况用户不会再见到登录流程。
"""

from __future__ import annotations

import base64
import json
import logging
import random
import time
from urllib.parse import parse_qs, urlparse

import requests

_LOGGER = logging.getLogger(__name__)

SID = "miothealth"
QR_LOGIN_URL = "https://account.xiaomi.com/longPolling/loginUrl"
STS_HEALTH_URL = "https://sts-hlth.io.mi.com/healthapp/sts"
XIAOMI_LOGIN_URL = "https://account.xiaomi.com/pass/serviceLogin"

# 登录用 UA = 小米运动健康 CN 版 App 指纹(passToken 续期与 STS 同源需要)
_UA_LOGIN = (
    "Dalvik/2.1.0 (Linux; U; Android 12; V2284A Build/ab8c0d1.1) "
    "APP/mi.health APPV/353001 MK/VjIyODRB SDKV/5.3.0.release.68 "
    "CPN/com.mi.health PassportSDK/"
)

_MAX_POLL_SECONDS = 300  # 登录链接有效期上限(小米侧 timeout 一般 300s)


class XiaomiLoginError(Exception):
    """Base login error."""


class LoginResult:
    """Successful login credentials."""

    def __init__(
        self,
        user_id: str,
        c_user_id: str,
        ssecurity: str,
        service_token: str,
        pass_token: str = "",
    ) -> None:
        self.user_id        = user_id
        self.c_user_id     = c_user_id
        self.ssecurity     = ssecurity
        self.service_token = service_token
        # passToken —— 比 serviceToken 长寿,用于过期后静默续期
        self.pass_token    = pass_token


def parse_mi_response(text: str) -> dict:
    """解析小米 API 带 &&&START&&& 前缀的 JSON 响应。"""
    body = text
    if body.startswith("&&&START&&&"):
        body = body[len("&&&START&&&"):]
    return json.loads(body)


def make_login_session() -> requests.Session:
    dev_hex = "".join(random.choices("0123456789abcdef", k=32))
    s = requests.Session()
    s.headers.update({
        "User-Agent":   _UA_LOGIN,
        "Content-Type": "application/x-www-form-urlencoded",
    })
    s.cookies.update({"sdkVersion": "3.8.6", "deviceId": f"an_{dev_hex}"})
    return s


def _get_service_token(session: requests.Session, location: str) -> str:
    """跟随 STS 重定向,从响应 cookie(或重定向查询参数)提取 serviceToken。"""
    r = session.get(location, timeout=15)
    token = r.cookies.get("serviceToken")
    if not token:
        token = session.cookies.get("serviceToken", "")
    if not token:
        try:
            qs = parse_qs(urlparse(r.url).query)
            token = qs.get("serviceToken", [""])[0]
        except Exception:
            token = ""
    if not token:
        raise XiaomiLoginError("未能从 STS 重定向获取 serviceToken")
    return token


class XiaomiLoginSession:
    """一次浏览器链接登录会话:qr_login_start() 取链接,qr_login_poll() 等确认。"""

    def __init__(self) -> None:
        self._session        = make_login_session()
        self._lp_url         = ""
        self._deadline       = 0.0
        self.login_url       = ""
        self.qr_image_b64    = ""
        self.timeout_seconds = _MAX_POLL_SECONDS

    def qr_login_start(self) -> dict:
        """申请登录链接与二维码。返回 {login_url, qr_image_b64, timeout}。"""
        r = self._session.get(QR_LOGIN_URL, params={
            "_qrsize": "480",
            "qs": f"%3Fsid%3D{SID}%26_json%3Dtrue",
            "callback": STS_HEALTH_URL,
            "_hasLogo": "false",
            "sid": SID,
            "serviceParam": "",
            "_locale": "zh_CN",
            "_dc": str(int(time.time() * 1000)),
        }, timeout=15)
        qr = parse_mi_response(r.text)
        login_url = qr.get("loginUrl", "")
        lp = qr.get("lp", "")
        if not login_url or not lp:
            raise XiaomiLoginError(f"获取登录链接失败: {str(qr)[:200]}")

        self.login_url = login_url
        self._lp_url   = lp
        timeout = min(float(qr.get("timeout", _MAX_POLL_SECONDS)), _MAX_POLL_SECONDS)
        self.timeout_seconds = timeout
        self._deadline = time.time() + timeout

        # 二维码图片随会话取回(base64 内嵌展示;手机扫码是可选捷径)
        try:
            qr_img = qr.get("qr", "")
            if qr_img:
                img = self._session.get(qr_img, timeout=10)
                self.qr_image_b64 = base64.b64encode(img.content).decode()
        except Exception as exc:
            _LOGGER.warning("二维码图片取回失败(不影响链接登录): %s", exc)

        return {
            "login_url":    login_url,
            "qr_image_b64": self.qr_image_b64,
            "timeout":      timeout,
        }

    def qr_login_poll(self) -> LoginResult:
        """长轮询等待用户在浏览器完成登录。成功返回完整凭证。"""
        if not self._lp_url:
            raise XiaomiLoginError("请先调用 qr_login_start()")

        while time.time() < self._deadline:
            try:
                r = self._session.get(self._lp_url, timeout=65)
            except requests.Timeout:
                continue
            except requests.RequestException as exc:
                _LOGGER.warning("登录轮询网络错误,重试: %s", exc)
                time.sleep(2)
                continue
            if r.status_code != 200:
                time.sleep(2)
                continue
            try:
                data = parse_mi_response(r.text)
            except Exception:
                time.sleep(2)
                continue
            if not data.get("ssecurity"):
                _LOGGER.debug("登录轮询等待中: %s", r.text[:120])
                time.sleep(2)
                continue

            # 登录成功 —— 提取凭证 + 跟随 location 拿 serviceToken
            location = data.get("location", "")
            return LoginResult(
                user_id=str(data.get("userId", "")),
                c_user_id=data.get("cUserId", ""),
                ssecurity=data["ssecurity"],
                service_token=(
                    _get_service_token(self._session, location) if location else ""
                ),
                pass_token=data.get("passToken", ""),
            )

        raise XiaomiLoginError("登录链接已过期,请重新发起登录")


# ── passToken 静默续期(serviceToken 过期时 coordinator 调用)────────────────

def _exchange_pass_token(
    session: requests.Session,
    user_id: str,
    pass_token: str,
    sid: str = SID,
) -> dict:
    """passToken cookie 换 ssecurity + location → serviceToken。免 2FA。"""
    session.cookies.set("passToken", pass_token, domain="account.xiaomi.com")
    session.cookies.set("userId",    user_id,    domain="account.xiaomi.com")

    try:
        r = session.get(
            XIAOMI_LOGIN_URL,
            params={"_json": "true", "sid": sid, "_locale": "zh_CN"},
            timeout=15,
        )
        data = parse_mi_response(r.text)
        ssec = data.get("ssecurity", "")
        if data.get("code", -1) != 0 or not ssec:
            _LOGGER.warning(
                "passToken 续期: serviceLogin 未返回凭证 code=%s", data.get("code")
            )
            return {}
        location = data.get("location", "")
        result = {
            "ssecurity":     ssec,
            "service_token": "",
            "user_id":       str(data.get("userId", user_id)),
            "c_user_id":     data.get("cUserId", ""),
            "pass_token":    data.get("passToken", pass_token),
        }
        if location:
            try:
                result["service_token"] = _get_service_token(session, location)
            except Exception as sts_exc:
                _LOGGER.warning("passToken 续期: STS 失败: %s", sts_exc)
        return result
    except Exception as exc:
        _LOGGER.warning("passToken 续期异常: %s", exc)
    return {}


def try_silent_token_refresh(pass_token: str, user_id: str) -> dict:
    """用 passToken 静默换取新的 ssecurity + serviceToken。失败返回空 dict。"""
    if not pass_token or not user_id:
        return {}
    session = make_login_session()
    creds = _exchange_pass_token(session, user_id, pass_token, sid=SID)
    if creds.get("ssecurity") and creds.get("service_token"):
        _LOGGER.info(
            "passToken 静默续期成功: ssec_len=%d tok_len=%d",
            len(creds["ssecurity"]), len(creds["service_token"]),
        )
        return {
            "ssecurity":     creds["ssecurity"],
            "service_token": creds["service_token"],
            "user_id":       creds.get("user_id", user_id),
            "c_user_id":     creds.get("c_user_id", ""),
            "pass_token":    pass_token,  # 保留原 passToken,不做轮换
        }
    _LOGGER.warning("passToken 静默续期失败(可能已过期,需重新走浏览器登录)")
    return {}

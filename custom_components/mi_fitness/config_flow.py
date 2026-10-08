"""Config flow for Mi Fitness —— 浏览器链接登录(唯一登录方式)。

流程:选区域 → 展示登录链接(电脑浏览器点开登录/确认,二维码备用)
→ 后台长轮询等确认 → 拿到凭证自动创建条目。全程无密码、无验证码、
无需手机,风控(若有)在小米自己的页面里交互式完成。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .auth import XiaomiLoginError, XiaomiLoginSession
from .const import (
    CONF_C_USER_ID,
    CONF_PASS_TOKEN,
    CONF_REGION,
    CONF_SERVICE_TOKEN,
    CONF_SSECURITY,
    CONF_USER_ID,
    DEFAULT_REGION,
    DOMAIN,
    REGIONS,
)

_LOGGER = logging.getLogger(__name__)


class MiFitnessConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Mi Fitness."""

    VERSION = 1

    def __init__(self) -> None:
        self._region = DEFAULT_REGION
        self._login_session: XiaomiLoginSession | None = None
        self._login_info: dict[str, Any] | None = None
        self._qr_task: asyncio.Task | None = None
        self._qr_result = None
        self._qr_error: Exception | None = None
        self._status_note = ""
        self._reauth_entry = None

    # ── 入口:选区域 ──────────────────────────────────────────────────────────

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._region = user_input[CONF_REGION]
            return await self.async_step_browser_login()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_REGION, default=DEFAULT_REGION): vol.In(REGIONS),
            }),
        )

    # ── 浏览器链接登录 ────────────────────────────────────────────────────────

    async def _qr_poll_loop(self) -> None:
        """后台长轮询:用户在浏览器确认后拿凭证,并自动推进配置流。"""
        try:
            self._qr_result = await self.hass.async_add_executor_job(
                self._login_session.qr_login_poll
            )
        except Exception as exc:  # noqa: BLE001 — 轮询失败统一呈现给用户
            self._qr_error = exc
            _LOGGER.warning("浏览器登录轮询失败: %s", exc)
        # 无论成功失败都自动推进:成功 → 落库;失败 → 换新链接重新展示
        try:
            self.hass.async_create_task(
                self.hass.config_entries.flow.async_configure(self.flow_id)
            )
        except Exception:
            _LOGGER.debug("配置流自动推进失败,可手动点「提交」", exc_info=True)

    def _reset_login(self) -> None:
        self._login_session = None
        self._login_info = None
        self._qr_task = None
        self._qr_result = None
        self._qr_error = None

    async def async_step_browser_login(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """直接展示进度页:前端自动轮询,登录确认后自动完成(零点击)。"""
        # 登录已完成 → 落库
        if self._qr_result is not None:
            return await self._finish_login(self._qr_result)

        # 轮询失败(链接过期等)自动推进到这里(任务已结束)→ 重置重生成
        if (
            self._qr_error is not None
            and self._qr_task is not None
            and self._qr_task.done()
        ):
            note = str(self._qr_error)
            self._reset_login()
            self._status_note = f"{note};已自动生成新的登录链接"
            return await self.async_step_browser_login()

        # 首次进入:申请登录链接 + 启动后台轮询
        if self._login_session is None:
            self._login_session = XiaomiLoginSession()
            try:
                self._login_info = await self.hass.async_add_executor_job(
                    self._login_session.qr_login_start
                )
            except XiaomiLoginError as exc:
                _LOGGER.warning("申请登录链接失败: %s", exc)
                return self.async_abort(reason="link_start_failed")
            self._qr_task = self.hass.async_create_task(self._qr_poll_loop())

        # 进度页:说明里带登录链接与二维码,前端轮询任务完成即自动关闭
        info = self._login_info or {}
        return self.async_show_progress(
            step_id="wait_login",
            progress_action="wait_login",
            progress_task=self._qr_task,
            description_placeholders={
                "login_url": info.get("login_url", ""),
                "qr_image":  info.get("qr_image_b64", ""),
                "timeout":   int(info.get("timeout", 0)),
                "status":    self._status_note,
            },
        )

    async def async_step_wait_login(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """进度页:登录任务结束后由 flow manager 自动推进到这里。"""
        if self._qr_result is not None:
            return await self._finish_login(self._qr_result)
        if self._qr_error is not None:
            note = str(self._qr_error)
            self._reset_login()
            self._status_note = f"{note};已自动生成新的登录链接"
            return await self.async_step_browser_login()
        # 任务未完成却被推进(前端重试等)→ 继续展示进度
        return await self.async_step_browser_login()

    async def async_step_wait_login(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """进度页:登录任务完成后由前端推进到这里。"""
        if self._qr_result is not None:
            return await self._finish_login(self._qr_result)
        if self._qr_error is not None:
            note = str(self._qr_error)
            self._reset_login()
            self._status_note = f"{note};已生成新的登录链接,请重新完成登录"
            return await self.async_step_browser_login()
        # 兜底:任务未完成(理论上不可达)——回登录表单
        return await self.async_step_browser_login()

    async def _finish_login(self, result) -> ConfigFlowResult:
        entry_data = {
            "auth_method":  "browser",
            CONF_USER_ID:       result.user_id,
            CONF_C_USER_ID:     result.c_user_id,
            CONF_SSECURITY:     result.ssecurity,
            CONF_SERVICE_TOKEN: result.service_token,
            CONF_REGION:        self._region,
        }
        if result.pass_token:
            entry_data[CONF_PASS_TOKEN] = result.pass_token

        # 重新认证:直接刷新原条目
        if self._reauth_entry is not None:
            entry = self._reauth_entry
            self.hass.config_entries.async_update_entry(entry, data=entry_data)
            self.hass.async_create_task(
                self.hass.config_entries.async_reload(entry.entry_id)
            )
            return self.async_abort(reason="reauth_successful")

        # 同账号已配置 → 刷新其凭证并重载(重新登录即换新凭证,无需删除重加)
        await self.async_set_unique_id(result.user_id)
        for entry in self._async_current_entries():
            if entry.unique_id == result.user_id:
                self.hass.config_entries.async_update_entry(entry, data=entry_data)
                self.hass.async_create_task(
                    self.hass.config_entries.async_reload(entry.entry_id)
                )
                return self.async_abort(reason="reauth_successful")
        self._abort_if_unique_id_configured()

        return self.async_create_entry(
            title=f"小米运动健康 ({result.user_id})",
            data=entry_data,
        )

    # ── 重新认证(serviceToken/passToken 均失效时)────────────────────────────

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        self._reauth_entry = self._get_reauth_entry()
        self._region = entry_data.get(CONF_REGION, DEFAULT_REGION)
        self._status_note = "凭证已过期,请重新在浏览器中登录"
        return await self.async_step_browser_login()

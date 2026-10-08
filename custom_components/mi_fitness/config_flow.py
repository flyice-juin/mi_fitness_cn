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
            return
        # 自动推进当前步骤(用户无需手动点提交)
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
        errors: dict[str, str] = {}

        # 登录已完成(自动推进或手动检查)→ 落库
        if self._qr_result is not None:
            return self._finish_login(self._qr_result)

        # 首次进入:申请登录链接 + 启动后台轮询
        if self._login_session is None:
            self._login_session = XiaomiLoginSession()
            try:
                self._login_info = await self.hass.async_add_executor_job(
                    self._login_session.qr_login_start
                )
            except XiaomiLoginError as exc:
                _LOGGER.warning("申请登录链接失败: %s", exc)
                self._login_session = None
                self._status_note = "获取登录链接失败,点「提交」重试"
                return self._show_browser_form(errors={"base": "link_start_failed"})
            self._qr_task = self.hass.async_create_task(self._qr_poll_loop())

        # 用户手动点了「提交」(检查登录状态)
        if user_input is not None and self._qr_task is not None and self._qr_task.done():
            if self._qr_error is not None:
                note = str(self._qr_error)
                self._reset_login()
                self._status_note = f"{note};已生成新的登录链接,请重新完成登录"
                return await self.async_step_browser_login()

        return self._show_browser_form(errors=errors)

    def _show_browser_form(self, errors: dict[str, str] | None = None) -> ConfigFlowResult:
        info = self._login_info or {}
        return self.async_show_form(
            step_id="browser_login",
            data_schema=vol.Schema({}),
            errors=errors or {},
            description_placeholders={
                "login_url": info.get("login_url", ""),
                "qr_image":  info.get("qr_image_b64", ""),
                "timeout":   int(info.get("timeout", 0)),
                "status":    self._status_note,
            },
        )

    def _finish_login(self, result) -> ConfigFlowResult:
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

        if self._reauth_entry is not None:
            entry = self._reauth_entry
            self.hass.config_entries.async_update_entry(entry, data=entry_data)
            self.hass.async_create_task(
                self.hass.config_entries.async_reload(entry.entry_id)
            )
            return self.async_abort(reason="reauth_successful")

        return self.hass.async_create_task(self._async_finish_create(entry_data, result))

    async def _async_finish_create(self, entry_data: dict, result) -> ConfigFlowResult:
        await self.async_set_unique_id(result.user_id)
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

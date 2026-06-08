"""中燃燃气查询 — 配置流程 (Config Flow)

三步登录：手机号 → 图片验证码 → 短信验证码 → 存储令牌。
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

import aiofiles
import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_CAPTCHA_CODE,
    CONF_CUST_CODE,
    CONF_ENVIR,
    CONF_MASTER_TOKEN,
    CONF_PHONE_NUMBER,
    CONF_SID,
    CONF_SMS_CODE,
    CONF_USER_ID,
    CONF_USER_NAME,
    DOMAIN,
    API_BASE_ONLINE,
    API_CAPTCHA,
    API_SEND_SMS,
    API_LOGIN,
)
from . import HazrApiClient

_LOGGER = logging.getLogger(__name__)

# ── 三步表单 Schema ──────────────────────────────────────────

# 第 1 步：手机号 + 开户人姓名
STEP_PHONE_SCHEMA = vol.Schema({
    vol.Required(CONF_PHONE_NUMBER): TextSelector(
        TextSelectorConfig(type=TextSelectorType.TEL, autocomplete="tel")
    ),
    vol.Required(CONF_USER_NAME): TextSelector(
        TextSelectorConfig(type=TextSelectorType.TEXT)
    ),
})

STEP_CAPTCHA_SCHEMA = vol.Schema({
    vol.Required(CONF_CAPTCHA_CODE): str,
})

# 第 3 步：短信验证码（纯文本输入）
STEP_SMS_SCHEMA = vol.Schema({
    vol.Required(CONF_SMS_CODE): TextSelector(
        TextSelectorConfig(type=TextSelectorType.TEXT)
    ),
})


# ══════════════════════════════════════════════════════════════
#  配置流主类 — 三步登录流程
# ══════════════════════════════════════════════════════════════

class HazrConfigFlow(ConfigFlow, domain=DOMAIN):

    VERSION = 1

    # ── 初始化 ───────────────────────────────────────────────

    def __init__(self) -> None:
        self._phone_number: str | None = None
        self._user_name: str | None = None
        self._captcha_image: str | None = None
        self._captcha_sid: str | None = None
        self._reauth = False
        self._client: HazrApiClient | None = None
        self._accounts: list[dict] | None = None
        self._tokens: dict | None = None
        self._ts: int = 0
        self._captcha_fail_time: float | None = None
        self._sms_cooldown = 180

    # ── 第 1 步：输入手机号 ────────────────────────────────

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            self._phone_number = user_input[CONF_PHONE_NUMBER]
            self._user_name = user_input[CONF_USER_NAME]

            await self.async_set_unique_id(self._phone_number)
            self._abort_if_unique_id_configured()

            session = async_get_clientsession(self.hass)
            try:
                captcha_data = await self._async_get_captcha(session)
                self._captcha_image = captcha_data.get("captcha_image")

                if self._captcha_image:
                    return await self.async_step_captcha()
                errors["base"] = "cannot_connect"
            except (aiohttp.ClientError, TimeoutError) as exc:
                _LOGGER.warning("获取验证码网络错误: %s", exc)
                errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_PHONE_SCHEMA,
            errors=errors,
        )

    # ── 第 2 步：输入图片验证码 ─────────────────────────────

    async def async_step_captcha(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        # 检查短信发送冷却
        if (
            self._captcha_fail_time
            and time.time() - self._captcha_fail_time < self._sms_cooldown
        ):
            remaining = int(
                self._sms_cooldown
                - (time.time() - self._captcha_fail_time)
            )
            _LOGGER.debug("captcha: sms cooldown active, %ds remaining", remaining)
            errors["base"] = "sms_cooldown"
            return self.async_show_form(
                step_id="captcha",
                data_schema=STEP_CAPTCHA_SCHEMA,
                errors=errors,
                description_placeholders={
                    "captcha_img": self._captcha_image or "no-image",
                },
            )

        if user_input is not None:
            captcha_code = user_input[CONF_CAPTCHA_CODE]

            session = async_get_clientsession(self.hass)
            try:
                success, err_msg = await self._async_send_sms(
                    session, captcha_code
                )
                if success:
                    _LOGGER.debug("captcha: send sms success, goto sms_code step")
                    self._captcha_fail_time = None
                    return await self.async_step_sms_code()
                _LOGGER.warning("captcha: send sms failed: %s", err_msg)
                errors[CONF_CAPTCHA_CODE] = err_msg
                self._captcha_fail_time = time.time()
                self._captcha_image = None
                self._async_clean_captcha_file()
            except (aiohttp.ClientError, TimeoutError) as exc:
                _LOGGER.warning("captcha: network error: %s", exc)
                errors["base"] = "cannot_connect"

        if not self._captcha_image:
            session = async_get_clientsession(self.hass)
            try:
                captcha_data = await self._async_get_captcha(session)
                self._captcha_image = captcha_data.get("captcha_image")
            except (aiohttp.ClientError, TimeoutError) as exc:
                _LOGGER.warning("请求验证码网络错误: %s", exc)
                errors["base"] = "cannot_connect"

        _LOGGER.debug("captcha image path: %s", self._captcha_image)
        return self.async_show_form(
            step_id="captcha",
            data_schema=STEP_CAPTCHA_SCHEMA,
            errors=errors,
            description_placeholders={
                "captcha_img": self._captcha_image or "no-image",
            },
        )

    # ── 第 3 步：输入短信验证码 ─────────────────────────────

    async def async_step_sms_code(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        # 进入第 3 步时清理验证码图片
        self._async_clean_captcha_file()
        self._captcha_image = None

        if user_input is not None:
            sms_code = user_input[CONF_SMS_CODE]

            session = async_get_clientsession(self.hass)
            try:
                tokens = await self._async_login(session, sms_code)
                if tokens:
                    client = HazrApiClient(
                        session,
                        self._phone_number,
                        tokens[CONF_MASTER_TOKEN],
                        tokens[CONF_SID],
                        tokens[CONF_USER_ID],
                    )
                    ts = await client.async_buried_point_add()
                    bind_data = await client.async_get_bind_gas_cust_list(ts)
                    accounts = bind_data.get("data", [])

                    if self._reauth:
                        return self.async_update_reload_and_abort(
                            self._get_reauth_entry(),
                            data_updates={
                                CONF_USER_NAME: self._user_name,
                                CONF_MASTER_TOKEN: tokens[CONF_MASTER_TOKEN],
                                CONF_SID: tokens[CONF_SID],
                            },
                        )

                    self._client = client
                    self._accounts = accounts
                    self._tokens = tokens
                    self._ts = ts
                    return await self.async_step_select_account()
                errors["base"] = "invalid_sms_code"
            except (aiohttp.ClientError, TimeoutError) as exc:
                _LOGGER.warning("登录网络错误: %s", exc)
                errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="sms_code",
            data_schema=STEP_SMS_SCHEMA,
            errors=errors,
        )

    # ── 第 4 步：选择燃气账户 ────────────────────────────

    async def async_step_select_account(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            idx = int(user_input["account"])
            acct = self._accounts[idx]
            cust_code = acct["custCode"]

            cust_info = await self._client.async_find_cust_info(
                cust_code, self._ts
            )
            cust_info_data = cust_info.get("data", {})
            envir = cust_info_data.get("envir", "2")
            self._client._cust_code = cust_code
            self._client._envir = envir

            return self.async_create_entry(
                title=str(self._phone_number),
                data={
                    CONF_PHONE_NUMBER: self._phone_number,
                    CONF_USER_NAME: self._user_name,
                    CONF_MASTER_TOKEN: self._tokens[CONF_MASTER_TOKEN],
                    CONF_SID: self._tokens[CONF_SID],
                    CONF_USER_ID: self._tokens[CONF_USER_ID],
                    CONF_CUST_CODE: cust_code,
                    CONF_ENVIR: envir,
                },
            )

        options = {}
        for i, acct in enumerate(self._accounts):
            label = f"{acct.get('custName', '')} | {acct.get('custCode', '')} | {acct.get('address', '')}"
            options[str(i)] = label

        schema = vol.Schema({
            vol.Required("account"): vol.In(options),
        })

        return self.async_show_form(
            step_id="select_account",
            data_schema=schema,
            errors=errors,
        )

    # ── 重新认证入口（令牌过期时触发） ─────────────────────

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        self._reauth = True
        self._phone_number = entry_data.get(CONF_PHONE_NUMBER)
        return await self.async_step_user()

    # ── 清理旧的验证码文件 ──────────────────────────────

    def _async_clean_captcha_file(self) -> None:
        if self._phone_number:
            filename = f"hazr_captcha_{self._phone_number}.jpg"
            filepath = os.path.join(
                self.hass.config.path("www"), "hazr", filename
            )
            try:
                os.remove(filepath)
            except FileNotFoundError:
                pass

    # ── 获取验证码图片 ─────────────────────────────────────

    async def _async_get_captcha(
        self, session: aiohttp.ClientSession
    ) -> dict[str, str | None]:
        tn = str(int(time.time() * 1000))
        url = f"{API_BASE_ONLINE}{API_CAPTCHA}"
        async with session.get(
            url,
            params={"flag": self._phone_number, "tn": tn},
            timeout=10,
        ) as resp:
            if resp.status != 200:
                raise aiohttp.ClientResponseError(
                    resp.request_info,
                    resp.history,
                    status=resp.status,
                )
            image_bytes = await resp.read()

            www_path = os.path.join(self.hass.config.path("www"), "hazr")
            os.makedirs(www_path, exist_ok=True)

            filename = f"hazr_captcha_{self._phone_number}.jpg"
            filepath = os.path.join(www_path, filename)
            async with aiofiles.open(filepath, "wb") as f:
                await f.write(image_bytes)

            _LOGGER.debug("captcha saved: %s (%d bytes)", filepath, len(image_bytes))

            base_url = (
                self.hass.config.external_url
                or self.hass.config.internal_url
                or f"http://{self.hass.config.api}"
            )
            return {"captcha_image": f"{base_url}/local/hazr/{filename}?t={int(time.time() * 1000)}"}

    # ── 校验验证码并发短信 ─────────────────────────────────

    async def _async_send_sms(
        self, session: aiohttp.ClientSession, captcha_code: str
    ) -> tuple[bool, str]:
        url = f"{API_BASE_ONLINE}{API_SEND_SMS}"
        body = {
            "codeKey": self._phone_number,
            "codeKeyValue": captcha_code,
            "mobile": self._phone_number,
        }
        _LOGGER.debug("sendsms request: POST %s body=%s", url, body)
        async with session.post(url, data=body,
            timeout=10,
        ) as resp:
            resp_text = await resp.text()
            _LOGGER.debug(
                "sendsms response: status=%d body=%s", resp.status, resp_text
            )
            if resp.status != 200:
                raise aiohttp.ClientResponseError(
                    resp.request_info,
                    resp.history,
                    status=resp.status,
                    message=f"SMS request failed: {resp_text}",
                )
            data = json.loads(resp_text)
            status = data.get("status")
            message = data.get("message", "")
            _LOGGER.debug("sendsms result: status=%s message=%s", status, message)
            if status in ("ok", "1"):
                return True, message
            return False, message or "验证码错误"

    async def _async_login(
        self, session: aiohttp.ClientSession, sms_code: str
    ) -> dict[str, str | int] | None:
        async with session.post(
            f"{API_BASE_ONLINE}{API_LOGIN}",
            data={
                "mobile": self._phone_number,
                "code": sms_code,
                "channelType": "6",
                "openId": "",
                "unionId": "",
            },
            timeout=15,
        ) as resp:
            resp_text = await resp.text()
            if resp.status == 200:
                data = json.loads(resp_text)
                if data.get("status") not in ("ok", "1"):
                    _LOGGER.debug(
                        "login failed: status=%s message=%s",
                        data.get("status"), data.get("message"),
                    )
                    return None
                user_data = data.get("data", {})
                return {
                    CONF_MASTER_TOKEN: data.get("masToken", ""),
                    CONF_SID: data.get("sid", ""),
                    CONF_USER_ID: user_data.get("id", 0),
                }
            _LOGGER.warning("login http error: status=%d", resp.status)
            _LOGGER.debug("login http error body: %s", resp_text)
            if resp.status == 401:
                return None
            raise aiohttp.ClientResponseError(
                resp.request_info,
                resp.history,
                status=resp.status,
            )

    
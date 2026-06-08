"""Support for hazr (中燃燃气查询) integration."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import random
import time
from datetime import timedelta
from typing import Any

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_CUST_CODE,
    CONF_ENVIR,
    CONF_MASTER_TOKEN,
    CONF_PHONE_NUMBER,
    CONF_SID,
    CONF_USER_NAME,
    CONF_USER_ID,
    DOMAIN,
    PLATFORMS,
    DEFAULT_SCAN_INTERVAL,
    API_BASE_ONLINE,
    API_BIND_GAS_CUST_LIST,
    API_BURIED_POINT,
    API_FIND_CUST_INFO,
    API_METHOD_ENCRYPT,
    API_PAYMENT_LOG,
    HazrApiError,
    HazrAuthError,
    HazrConnectionError,
)

_LOGGER = logging.getLogger(__name__)

HazrConfigEntry = ConfigEntry["HazrApiClient"]


class HazrApiClient:
    """API client for 中燃燃气 (China Gas) services."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        phone_number: str,
        mas_token: str,
        sid: str,
        user_id: int = 0,
        cust_code: str = "",
        envir: str = "",
        id_no: str = "",
    ) -> None:
        self._session = session
        self._phone = phone_number
        self._mas_token = mas_token
        self._sid = sid
        self._user_id = user_id
        self._cust_code = cust_code
        self._envir = envir
        self._id_no = id_no

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0)"
                " Gecko/20100101 Firefox/133.0"
            ),
            "x-mas-app-info": f"aaahg10001/{self._sid}",
        }

    async def async_get_gas_data(self) -> dict[str, Any]:
        """查询燃气数据，返回扁平字典供 sensor 使用。"""
        ts = await self.async_buried_point_add()
        now = time.localtime()
        end_time = time.strftime("%Y%m", now)
        # startTime = 上个月份往前推365天
        now_sec = time.mktime(now)
        last_month_sec = now_sec - 30 * 86400  # 回到上个月附近
        start_time = time.strftime(
            "%Y%m", time.localtime(last_month_sec - 365 * 86400)
        )

        inner = {
            "appKey": "HSH3141584003200621363QHL9",
            "custCode": self._cust_code,
            "endTime": end_time,
            "envir": self._envir,
            "idNo": self._id_no,
            "startTime": start_time,
            "timeStamp": ts,
        }
        self.generate_sign(inner)

        payload = self.request_method_encrypt(inner)
        async with self._session.post(
            f"{API_BASE_ONLINE}{API_METHOD_ENCRYPT}",
            headers=self._headers,
            data=payload,
            timeout=15,
        ) as resp:
            if resp.status == 401:
                raise HazrAuthError("Token expired")
            if resp.status != 200:
                raise HazrConnectionError(
                    f"methodEncrypt failed: {resp.status}"
                )
            resp_text = await resp.text()
            _LOGGER.debug("methodEncrypt response: status=%d body=%s", resp.status, resp_text)
            raw = json.loads(resp_text)
            data = raw.get("data", {})
            check_list = data.get("payGasCheckList", [])
            current = check_list[0] if check_list else {}

            # 额外查询 findCustInfo 获取真实余额
            balance_info = await self.async_get_balance(
                int(time.time() * 1000)
            )
            _LOGGER.debug(
                "findCustInfo response: cust_code=%s body=%s",
                self._cust_code, balance_info,
            )
            bal_data = balance_info.get("data", {})
            real_balance = bal_data.get("newCountMoney")

            # 查询缴费历史（不需要埋点，用 generate_signature）
            payment_log = await self.async_get_payment_log()

            return {
                "gas_balance": real_balance or data.get("balance"),
                "gas_usage": current.get("curQty"),
                "gas_bill": current.get("totalFee"),
                "gas_last_payment": (payment_log or [{}])[0].get("amount"),
                "gas_last_payment_history": payment_log,
                "gas_address": data.get("addr"),
                "gas_account_no": data.get("custCode"),
                "pay_gas_check_list": check_list,
            }

    async def async_validate_token(self) -> bool:
        """Check if stored tokens are still valid — 只调轻量接口，不拉全量数据。"""
        try:
            ts = int(time.time() * 1000)
            await self.async_get_balance(ts)
            return True
        except HazrAuthError:
            return False
        except HazrApiError:
            # Connection errors don't mean bad tokens
            return True

    async def async_buried_point_add(self) -> int:
        """发埋点事件，返回当前毫秒时间戳供后续查询签名使用。"""
        payload = {
            "appUseType": 0,
            "clickType": 10,
            "eventType": 1,
            "channelType": 6,
            "userId": self._user_id,
        }
        try:
            async with self._session.post(
                f"{API_BASE_ONLINE}{API_BURIED_POINT}",
                headers=self._headers,
                data=payload,
                timeout=10,
            ) as resp:
                if resp.status != 200:
                    _LOGGER.debug("buried point add failed: %d", resp.status)
        except (aiohttp.ClientError, TimeoutError):
            _LOGGER.debug("buried point add network error")
        return int(time.time() * 1000)

    async def async_find_cust_info(
        self, cust_code: str, id_no: str, timestamp: int
    ) -> dict[str, Any]:
        params = {
            "custCode": cust_code,
            "idNo": id_no,
            "orderType": "FJF",
            "timeStamp": timestamp,
        }
        self.generate_signature(params, id_field="custCode")
        async with self._session.post(
            f"{API_BASE_ONLINE}{API_FIND_CUST_INFO}",
            headers=self._headers,
            data=params,
            timeout=15,
        ) as resp:
            if resp.status != 200:
                raise HazrConnectionError(
                    f"findCustInfo failed: {resp.status}"
                )
            return await resp.json()

    async def async_get_balance(self, timestamp: int) -> dict[str, Any]:
        """查询燃气余额（定时调用，只传必要参数）。"""
        params = {
            "custCode": self._cust_code,
            "timeStamp": timestamp,
        }
        self.generate_signature(params, id_field="custCode")
        async with self._session.post(
            f"{API_BASE_ONLINE}{API_FIND_CUST_INFO}",
            headers=self._headers,
            data=params,
            timeout=15,
        ) as resp:
            if resp.status != 200:
                raise HazrConnectionError(
                    f"getBalance failed: {resp.status}"
                )
            return await resp.json()

    async def async_get_bind_gas_cust_list(
        self, timestamp: int
    ) -> dict[str, Any]:
        url = f"{API_BASE_ONLINE}{API_BIND_GAS_CUST_LIST}"
        params = {
            "userId": self._user_id,
            "state": "1",
            "timeStamp": timestamp,
        }
        self.generate_signature(params)
        _LOGGER.debug("getBindGasCustList request: POST %s data=%s headers=%s", url, params, self._headers)
        async with self._session.post(
            url,
            headers=self._headers,
            data=params,
            timeout=15,
        ) as resp:
            resp_text = await resp.text()
            _LOGGER.debug("getBindGasCustList response: status=%d body=%s", resp.status, resp_text)
            if resp.status != 200:
                raise HazrConnectionError(
                    f"getBindGasCustList failed: {resp.status}"
                )
            return json.loads(resp_text)

    async def async_get_payment_log(self) -> list[dict[str, Any]]:
        """查询缴费历史。不需要埋点，用 generate_signature。"""
        now = time.localtime()
        end_time = time.strftime("%Y%m%d", now)
        now_sec = time.mktime(now)
        start_sec = now_sec - 180 * 86400  # 6个月 ≈ 180天
        start_time = time.strftime("%Y%m%d", time.localtime(start_sec))

        params: dict[str, Any] = {
            "custCode": self._cust_code,
            "startTime": start_time,
            "endTime": end_time,
            "timeStamp": int(now_sec * 1000),
        }
        self.generate_signature(params, id_field="custCode")

        async with self._session.post(
            f"{API_BASE_ONLINE}{API_PAYMENT_LOG}",
            headers=self._headers,
            data=params,
            timeout=15,
        ) as resp:
            if resp.status == 401:
                raise HazrAuthError("Token expired")
            if resp.status != 200:
                raise HazrConnectionError(
                    f"getPaymentLog failed: {resp.status}"
                )
            resp_text = await resp.text()
            raw = json.loads(resp_text)
            return raw.get("data", [])

    async def async_update_tokens(
        self, mas_token: str, sid: str
    ) -> None:
        """Update stored tokens (called after reauth)."""
        self._mas_token = mas_token
        self._sid = sid

    def generate_signature(
        self, param_dict: dict, id_field: str = "custCode"
    ) -> dict:
        """生成 signature 字段 (identifier + yph1234567890 + timestamp → MD5)。"""
        if "timeStamp" not in param_dict:
            param_dict["timeStamp"] = int(time.time() * 1000)
        identifier = param_dict.get(id_field, "")
        if not identifier:
            for key in [
                "autoSrvId", "custCode", "compcode", "compCode", "userId", "userid", "mobile"
            ]:
                if key in param_dict and param_dict[key]:
                    identifier = param_dict[key]
                    break
        if identifier:
            raw = f"{identifier}yph1234567890{param_dict['timeStamp']}"
            param_dict["signature"] = hashlib.md5(
                raw.encode("utf-8")
            ).hexdigest()
        return param_dict

    def generate_sign(self, param_dict: dict) -> dict:
        """生成 sign 字段 (排序 + &key=KEY → MD5 大写)。"""
        if "timeStamp" not in param_dict:
            param_dict["timeStamp"] = int(time.time() * 1000)
        if "nonce" not in param_dict:
            param_dict["nonce"] = str(random.randint(10**15, 10**16 - 1))

        parts = []
        for k in sorted(param_dict.keys()):
            v = param_dict[k]
            if v is not None and v != "":
                parts.append(f"{k}={v}")
        query_string = "&".join(parts)
        key = "9QHLFB50ACA1424F91F31989942DDBD8"
        plain = f"{query_string}&key={key}"
        param_dict["sign"] = hashlib.md5(
            plain.encode("utf-8")
        ).hexdigest().upper()
        return param_dict

    def request_method_encrypt(self, inner_params: dict) -> dict:
        """将内层参数 JSON 序列化 → Base64 → 包装为 methodEncrypt.do 请求体。"""
        json_str = json.dumps(inner_params, separators=(",", ":"))
        params_b64 = base64.b64encode(
            json_str.encode("utf-8")
        ).decode("utf-8")
        return {
            "method": "getcustomerMoneyListForMpOl",
            "params": params_b64,
        }


async def async_setup_entry(
    hass: HomeAssistant, entry: HazrConfigEntry
) -> bool:
    """Set up hazr from a config entry."""
    phone_number = entry.data[CONF_PHONE_NUMBER]
    mas_token = entry.data[CONF_MASTER_TOKEN]
    sid = entry.data[CONF_SID]
    user_id = entry.data.get(CONF_USER_ID, 0)
    cust_code = entry.data.get(CONF_CUST_CODE, "")
    envir = entry.data.get(CONF_ENVIR, "")
    id_no = entry.data.get(CONF_USER_NAME, "")

    session = async_get_clientsession(hass)
    client = HazrApiClient(
        session, phone_number, mas_token, sid, user_id, cust_code, envir, id_no
    )

    # Validate tokens on setup
    try:
        valid = await client.async_validate_token()
        if not valid:
            raise ConfigEntryAuthFailed(
                "Token expired, please re-authenticate"
            )
    except HazrConnectionError as err:
        raise ConfigEntryNotReady(
            f"Could not connect to 中燃 API: {err}"
        ) from err

    # Create coordinator for periodic data refresh
    async def async_update_data() -> dict[str, Any]:
        """Fetch gas data from API."""
        try:
            return await client.async_get_gas_data()
        except HazrAuthError as err:
            raise ConfigEntryAuthFailed(
                "Token expired, please re-authenticate"
            ) from err
        except HazrApiError as err:
            raise UpdateFailed(str(err)) from err

    coordinator: DataUpdateCoordinator[dict[str, Any]] = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name=DOMAIN,
        update_method=async_update_data,
        update_interval=timedelta(minutes=DEFAULT_SCAN_INTERVAL),
    )

    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = client
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "coordinator": coordinator,
    }

    # Forward to sensor platform
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: HazrConfigEntry
) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
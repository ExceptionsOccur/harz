"""Constants for the hazr (中燃燃气查询) integration."""

from __future__ import annotations

from typing import Final

from homeassistant.const import Platform

DOMAIN: Final = "hazr"

# Config flow keys
CONF_PHONE_NUMBER: Final = "phone_number"
CONF_USER_NAME: Final = "user_name"
CONF_MASTER_TOKEN: Final = "masToken"
CONF_SID: Final = "sid"
CONF_USER_ID: Final = "user_id"
CONF_CUST_CODE: Final = "cust_code"
CONF_ENVIR: Final = "envir"
CONF_CAPTCHA_CODE: Final = "captcha_code"
CONF_SMS_CODE: Final = "sms_code"

# API endpoints
API_BASE_ONLINE: Final = "https://zrds.95007.com"
API_CAPTCHA: Final = "/controller/merchant/authCode.do"
API_SEND_SMS: Final = "/user/sendsms3.do"
API_LOGIN: Final = "/user/xcxMobileUserLogin"
API_BURIED_POINT: Final = "/tracking/buriedPointEvent/add"
API_BIND_GAS_CUST_LIST: Final = "/crm_controller/user/getBindGasCustList"
API_FIND_CUST_INFO: Final = "/crm_controller/user/findCustInfo"
API_METHOD_ENCRYPT: Final = "/controller/payfee/methodEncrypt.do"
API_PAYMENT_LOG: Final = "/crm_controller/payfee/getPaymentLogForHsh"

# Defaults
DEFAULT_SCAN_INTERVAL: Final = 360  # minutes (6 小时)
DEFAULT_TIMEOUT: Final = 10

# Platforms
PLATFORMS: Final = [Platform.SENSOR]


# Error messages
class HazrApiError(Exception):
    """Generic API error."""


class HazrAuthError(HazrApiError):
    """Authentication error — token expired or invalid."""


class HazrConnectionError(HazrApiError):
    """Connection error — cannot reach API."""


# Sensor definitions: key, name, unit, icon, device_class, state_class
SENSOR_TYPES: Final = {
    "gas_balance": {
        "key": "gas_balance",
        "name": "燃气余额",
        "unit": "CNY",
        "icon": "mdi:currency-cny",
        "device_class": "monetary",
        "state_class": "total",
    },
    "gas_usage": {
        "key": "gas_usage",
        "name": "本月用气量",
        "unit": "m³",
        "icon": "mdi:fire",
        "device_class": None,
        "state_class": "total_increasing",
    },
    "gas_bill": {
        "key": "gas_bill",
        "name": "本月账单",
        "unit": "CNY",
        "icon": "mdi:currency-cny",
        "device_class": "monetary",
        "state_class": "total",
    },
    "gas_last_payment": {
        "key": "gas_last_payment",
        "name": "上次缴费",
        "unit": "CNY",
        "icon": "mdi:currency-cny",
        "device_class": "monetary",
        "state_class": "total",
    },
    "gas_address": {
        "key": "gas_address",
        "name": "用气地址",
        "unit": None,
        "icon": "mdi:map-marker",
        "device_class": None,
        "state_class": None,
    },
    "gas_account_no": {
        "key": "gas_account_no",
        "name": "燃气户号",
        "unit": None,
        "icon": "mdi:card-account-details",
        "device_class": None,
        "state_class": None,
    },
    "gas_last_payment_history": {
        "key": "gas_last_payment_history",
        "name": "缴费记录",
        "unit": None,
        "icon": "mdi:receipt-text-clock",
        "device_class": None,
        "state_class": None,
    },
    "gas_monthly_detail": {
        "key": "gas_monthly_detail",
        "name": "月度用气明细",
        "unit": None,
        "icon": "mdi:calendar-month",
        "device_class": None,
        "state_class": None,
    },
}
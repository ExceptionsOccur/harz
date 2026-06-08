"""Sensor platform for hazr (中燃燃气查询) integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)

from . import HazrConfigEntry
from .const import (
    DOMAIN,
    CONF_PHONE_NUMBER,
    CONF_CUST_CODE,
    SENSOR_TYPES,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HazrConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up hazr sensor entities from a config entry."""
    if DOMAIN not in hass.data or entry.entry_id not in hass.data[DOMAIN]:
        _LOGGER.error("No data found for %s", entry.entry_id)
        return

    coordinator: DataUpdateCoordinator[dict[str, Any]] = hass.data[DOMAIN][
        entry.entry_id
    ]["coordinator"]

    phone_number = entry.data[CONF_PHONE_NUMBER]
    cust_code = entry.data.get(CONF_CUST_CODE, "")

    entities: list[HazrSensor] = []
    for sensor_key, sensor_config in SENSOR_TYPES.items():
        entities.append(
            HazrSensor(
                coordinator=coordinator,
                entry_id=entry.entry_id,
                phone_number=phone_number,
                cust_code=cust_code,
                sensor_key=sensor_key,
                sensor_config=sensor_config,
            )
        )

    async_add_entities(entities)


class HazrSensor(CoordinatorEntity[DataUpdateCoordinator[dict[str, Any]]], SensorEntity):
    """Sensor representing a single gas data point."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: DataUpdateCoordinator[dict[str, Any]],
        entry_id: str,
        phone_number: str,
        cust_code: str,
        sensor_key: str,
        sensor_config: dict[str, Any],
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)

        self._sensor_key = sensor_key
        self._sensor_config = sensor_config

        self._attr_unique_id = f"hazr_{cust_code}_{sensor_key}"
        self._attr_name = sensor_config.get("name", sensor_key)
        self._attr_translation_key = sensor_key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, phone_number)},
            name=f"中燃燃气 ({phone_number})",
            manufacturer="中燃燃气",
            model="中国燃气",
            entry_type="service",
        )

        self._attr_native_unit_of_measurement = sensor_config.get("unit")
        self._attr_icon = sensor_config.get("icon")
        self._attr_device_class = sensor_config.get("device_class")
        self._attr_state_class = sensor_config.get("state_class")

        # 货币类传感器（余额、账单、缴费）显示 2 位小数
        if sensor_config.get("device_class") == "monetary":
            self._attr_suggested_display_precision = 2

    @property
    def native_value(self) -> str | float | None:
        if self.coordinator.data is None:
            return None
        if self._sensor_key == "gas_monthly_detail":
            check_list = self.coordinator.data.get("pay_gas_check_list")
            return f"{len(check_list)}个月" if check_list else "无数据"
        if self._sensor_key == "gas_last_payment_history":
            payments = self.coordinator.data.get("gas_last_payment_history")
            return f"{len(payments)}条" if payments else "无数据"
        return self.coordinator.data.get(self._sensor_key)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.coordinator.data is None:
            return None
        if self._sensor_key == "gas_monthly_detail":
            check_list = self.coordinator.data.get("pay_gas_check_list")
            if not check_list:
                return None
            return {"months": check_list}
        if self._sensor_key == "gas_last_payment_history":
            payments = self.coordinator.data.get("gas_last_payment_history")
            if not payments:
                return None
            return {"payments": payments}
        return None

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success and super().available
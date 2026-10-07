from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import REVOLUTIONS_PER_MINUTE, UnitOfPower

from . import DOMAIN
from .entity import PumpEntity

ERRORS = {0x00: "ok"}


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    if discovery_info is None:
        return
    controller = hass.data[DOMAIN]
    async_add_entities([PumpSpeed(controller), PumpPower(controller), PumpStatusSensor(controller)])


class PumpSpeed(PumpEntity, SensorEntity):
    _attr_native_unit_of_measurement = REVOLUTIONS_PER_MINUTE
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, controller):
        super().__init__(controller, "speed", "Pool pump speed", "mdi:pump")

    @property
    def available(self):
        return self.controller.connected or self.controller.powered is False

    @property
    def native_value(self):
        if self.controller.powered is False:
            return 0
        status = self.controller.status
        return (status.rpm if status.running else 0) if status else None

    @property
    def extra_state_attributes(self):
        controller = self.controller
        return {
            "commanded_rpm": controller.commanded_rpm,
            "control": controller.control,
            "holding_remote_control": controller.holding_remote,
            "heat_interlock": controller.interlock_active,
            "heat_min_rpm": controller.heat_min_rpm,
            "min_rpm": controller.min_rpm,
        }


class PumpPower(PumpEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, controller):
        super().__init__(controller, "power", "Pool pump power", "mdi:flash")

    @property
    def available(self):
        return self.controller.connected or self.controller.powered is False

    @property
    def native_value(self):
        if self.controller.powered is False:
            return 0
        status = self.controller.status
        return status.watts if status else None


class PumpStatusSensor(PumpEntity, SensorEntity):
    """running / stopped / error NN / offline, with the raw details as attributes."""

    def __init__(self, controller):
        super().__init__(controller, "status", "Pool pump status", "mdi:information-outline")

    @property
    def native_value(self):
        controller = self.controller
        status = controller.status
        if controller.powered is False:
            return "no power"
        if not controller.connected or status is None:
            return "offline"
        if status.error:
            return f"error {status.error}"
        return "running" if status.running else "stopped"

    @property
    def extra_state_attributes(self):
        controller = self.controller
        status = controller.status
        return {
            "error_code": status.error if status else None,
            "drive_state": status.drive_state if status else None,
            "pump_mode": status.mode if status else None,
            "pump_clock": status.clock if status else None,
            "last_seen": controller.last_seen,
            "last_error": controller.last_error,
        }

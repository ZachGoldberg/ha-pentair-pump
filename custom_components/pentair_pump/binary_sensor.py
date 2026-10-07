from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity

from . import DOMAIN
from .entity import PumpEntity


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    if discovery_info is None:
        return
    controller = hass.data[DOMAIN]
    async_add_entities([PumpConnected(controller), HeatInterlock(controller)])


class PumpConnected(PumpEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, controller):
        super().__init__(controller, "connected", "Pool pump connected")

    @property
    def is_on(self):
        return self.controller.connected


class HeatInterlock(PumpEntity, BinarySensorEntity):
    """On while something needs flow (heater firing or cooling down, cleaner), so the pump is held at the minimum."""

    def __init__(self, controller):
        super().__init__(controller, "heat_interlock", "Pool pump high-speed hold", "mdi:speedometer")

    @property
    def is_on(self):
        return self.controller.interlock_active

    @property
    def extra_state_attributes(self):
        return {"min_rpm": self.controller.min_rpm, "min_speed_entity": self.controller.min_speed_entity, "heat_entities": self.controller.heat_entities}

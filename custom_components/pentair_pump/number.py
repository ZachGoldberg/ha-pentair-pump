from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import REVOLUTIONS_PER_MINUTE

from . import DOMAIN, MAX_RPM, MIN_RPM
from .entity import PumpEntity


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    if discovery_info is None:
        return
    async_add_entities([PumpTargetSpeed(hass.data[DOMAIN])])


class PumpTargetSpeed(PumpEntity, NumberEntity):
    """The speed Home Assistant runs the pump at. Setting it switches control to Home Assistant."""

    _attr_native_min_value = MIN_RPM
    _attr_native_max_value = MAX_RPM
    _attr_native_step = 50
    _attr_native_unit_of_measurement = REVOLUTIONS_PER_MINUTE
    _attr_mode = NumberMode.SLIDER

    def __init__(self, controller):
        super().__init__(controller, "target_speed", "Pool pump target speed", "mdi:speedometer")

    @property
    def native_value(self):
        return self.controller.target_rpm

    async def async_set_native_value(self, value):
        await self.controller.async_set_target_rpm(int(value))

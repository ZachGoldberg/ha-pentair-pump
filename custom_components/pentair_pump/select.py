from __future__ import annotations

from homeassistant.components.select import SelectEntity

from . import CONTROL_HOME_ASSISTANT, CONTROL_KEYPAD, DOMAIN
from .entity import PumpEntity

OPTIONS = {CONTROL_KEYPAD: "Pump keypad schedule", CONTROL_HOME_ASSISTANT: "Home Assistant"}


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    if discovery_info is None:
        return
    async_add_entities([PumpControl(hass.data[DOMAIN])])


class PumpControl(PumpEntity, SelectEntity):
    """Who decides the speed. The heat interlock applies either way."""

    _attr_options = list(OPTIONS.values())

    def __init__(self, controller):
        super().__init__(controller, "control", "Pool pump control", "mdi:account-cog")

    @property
    def current_option(self):
        return OPTIONS[self.controller.control]

    async def async_select_option(self, option):
        control = next(key for key, label in OPTIONS.items() if label == option)
        await self.controller.async_set_control(control)

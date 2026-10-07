from __future__ import annotations

from homeassistant.components.switch import SwitchEntity

from . import CONTROL_HOME_ASSISTANT, DOMAIN
from .entity import PumpEntity


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    if discovery_info is None:
        return
    async_add_entities([PumpRun(hass.data[DOMAIN])])


class PumpRun(PumpEntity, SwitchEntity):
    """Run/stop under Home Assistant control. The heat interlock overrides a stop while heating."""

    def __init__(self, controller):
        super().__init__(controller, "run", "Pool pump", "mdi:pump")

    @property
    def is_on(self):
        controller = self.controller
        if controller.control == CONTROL_HOME_ASSISTANT:
            return controller.run_requested
        return bool(controller.status and controller.status.running)

    async def async_turn_on(self, **kwargs):
        await self.controller.async_set_run(True)

    async def async_turn_off(self, **kwargs):
        await self.controller.async_set_run(False)

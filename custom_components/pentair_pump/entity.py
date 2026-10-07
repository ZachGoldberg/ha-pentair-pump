"""Shared base for the pump's entities: they all redraw whenever the controller finishes a cycle."""
from __future__ import annotations

from homeassistant.helpers.entity import Entity

from . import DOMAIN, PumpController


class PumpEntity(Entity):
    _attr_should_poll = False
    _attr_has_entity_name = False

    def __init__(self, controller: PumpController, key: str, name: str, icon: str | None = None) -> None:
        self.controller = controller
        self._attr_unique_id = f"{DOMAIN}_{key}"
        self._attr_name = name
        if icon:
            self._attr_icon = icon

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self.controller.add_listener(self.async_write_ha_state))

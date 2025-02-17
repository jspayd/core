"""Base class for protect data."""
from __future__ import annotations

import collections
from datetime import timedelta
import logging
from typing import Any

from pyunifiprotect import NotAuthorized, NvrError, ProtectApiClient
from pyunifiprotect.data import Bootstrap, WSSubscriptionMessage
from pyunifiprotect.data.base import ProtectDeviceModel

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval

from .const import CONF_DISABLE_RTSP, DEVICES_THAT_ADOPT, DEVICES_WITH_ENTITIES

_LOGGER = logging.getLogger(__name__)


class ProtectData:
    """Coordinate updates."""

    def __init__(
        self,
        hass: HomeAssistant,
        protect: ProtectApiClient,
        update_interval: timedelta,
        entry: ConfigEntry,
    ) -> None:
        """Initialize an subscriber."""
        super().__init__()

        self._hass = hass
        self._entry = entry
        self._hass = hass
        self._update_interval = update_interval
        self._subscriptions: dict[str, list[CALLBACK_TYPE]] = {}
        self._unsub_interval: CALLBACK_TYPE | None = None
        self._unsub_websocket: CALLBACK_TYPE | None = None

        self.last_update_success = False
        self.access_tokens: dict[str, collections.deque] = {}
        self.api = protect

    @property
    def disable_stream(self) -> bool:
        """Check if RTSP is disabled."""
        return self._entry.options.get(CONF_DISABLE_RTSP, False)

    async def async_setup(self) -> None:
        """Subscribe and do the refresh."""
        self._unsub_websocket = self.api.subscribe_websocket(
            self._async_process_ws_message
        )
        await self.async_refresh()

    async def async_stop(self, *args: Any) -> None:
        """Stop processing data."""
        if self._unsub_websocket:
            self._unsub_websocket()
            self._unsub_websocket = None
        if self._unsub_interval:
            self._unsub_interval()
            self._unsub_interval = None
        await self.api.async_disconnect_ws()

    async def async_refresh(self, *_: Any, force: bool = False) -> None:
        """Update the data."""

        # if last update was failure, force until success
        if not self.last_update_success:
            force = True

        try:
            updates = await self.api.update(force=force)
        except NvrError:
            if self.last_update_success:
                _LOGGER.exception("Error while updating")
            self.last_update_success = False
            # manually trigger update to mark entities unavailable
            self._async_process_updates(self.api.bootstrap)
        except NotAuthorized:
            await self.async_stop()
            _LOGGER.exception("Reauthentication required")
            self._entry.async_start_reauth(self._hass)
            self.last_update_success = False
        else:
            self.last_update_success = True
            self._async_process_updates(updates)

    @callback
    def _async_process_ws_message(self, message: WSSubscriptionMessage) -> None:
        if message.new_obj.model in DEVICES_WITH_ENTITIES:
            self.async_signal_device_id_update(message.new_obj.id)

    @callback
    def _async_process_updates(self, updates: Bootstrap | None) -> None:
        """Process update from the protect data."""

        # Websocket connected, use data from it
        if updates is None:
            return

        self.async_signal_device_id_update(self.api.bootstrap.nvr.id)
        for device_type in DEVICES_THAT_ADOPT:
            attr = f"{device_type.value}s"
            devices: dict[str, ProtectDeviceModel] = getattr(self.api.bootstrap, attr)
            for device_id in devices.keys():
                self.async_signal_device_id_update(device_id)

    @callback
    def async_subscribe_device_id(
        self, device_id: str, update_callback: CALLBACK_TYPE
    ) -> CALLBACK_TYPE:
        """Add an callback subscriber."""
        if not self._subscriptions:
            self._unsub_interval = async_track_time_interval(
                self._hass, self.async_refresh, self._update_interval
            )
        self._subscriptions.setdefault(device_id, []).append(update_callback)

        def _unsubscribe() -> None:
            self.async_unsubscribe_device_id(device_id, update_callback)

        return _unsubscribe

    @callback
    def async_unsubscribe_device_id(
        self, device_id: str, update_callback: CALLBACK_TYPE
    ) -> None:
        """Remove a callback subscriber."""
        self._subscriptions[device_id].remove(update_callback)
        if not self._subscriptions[device_id]:
            del self._subscriptions[device_id]
        if not self._subscriptions and self._unsub_interval:
            self._unsub_interval()
            self._unsub_interval = None

    @callback
    def async_signal_device_id_update(self, device_id: str) -> None:
        """Call the callbacks for a device_id."""
        if not self._subscriptions.get(device_id):
            return

        for update_callback in self._subscriptions[device_id]:
            update_callback()

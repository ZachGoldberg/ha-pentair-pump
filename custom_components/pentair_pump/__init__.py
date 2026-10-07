"""Pentair WhisperFlo / IntelliFlo VS pump over an RS-485-to-TCP bridge (Elfin EW11).

Every cycle (10 s, or immediately after a change) the controller either
  * reads the pump's status only, leaving it on its own keypad schedule ("keypad" control), or
  * holds remote control and sends run/stop + speed, then reads status ("home_assistant" control).

Speed floor: min_speed_entity (a sensor giving the minimum RPM, 0 = none) holds the pump at or above
that speed, taking remote control even in keypad mode. Legacy heating interlock: while any configured heat entity is not "off" (Compool pool/spa heater mode, and its
post-heat cool-down "heat delay"),
the pump is forced to run at no less than heat_min_rpm, taking remote control even in keypad mode.
pentair_pump.ensure_speed lets a script raise the pump and wait for it before turning a heater on.

YAML:
  pentair_pump:
    host: 192.168.20.8
    port: 9801
    address: 0x60          # pump address 1 at the keypad
    heat_min_rpm: 2800
    power_entities: [switch.pool_pool, switch.pool_spa]
    heat_entities:
      - select.pool_controller_pool_heater_mode
      - select.pool_controller_spa_heater_mode
      - binary_sensor.pool_controller_heat_delay_active
"""
from __future__ import annotations

import asyncio
import logging
from logging.handlers import RotatingFileHandler
import time
from collections.abc import Callable

import voluptuous as vol

from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.discovery import async_load_platform
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.storage import Store

from .protocol import (
    describe_frame,
    ACTION_REMOTE,
    ACTION_RUN,
    ACTION_SET,
    ACTION_STATUS,
    REGISTER_SPEED,
    PumpStatus,
    build_frame,
    parse_frames,
)

_LOGGER = logging.getLogger(__name__)
# Every frame sent and received, decoded (like njsPC's packet log). Written to its own rotating
# file; also echoed to the HA log when this integration's logger is at DEBUG.
_PACKETS = logging.getLogger(__name__ + ".packets")
PACKET_LOG_FILE = "pentair_pump_packets.log"
PACKET_LOG_BYTES = 5_000_000
PACKET_LOG_BACKUPS = 2

DOMAIN = "pentair_pump"
PLATFORMS = [Platform.SENSOR, Platform.NUMBER, Platform.SWITCH, Platform.SELECT, Platform.BINARY_SENSOR]

CONTROL_KEYPAD = "keypad"
CONTROL_HOME_ASSISTANT = "home_assistant"

MIN_RPM = 450
MAX_RPM = 3450
CYCLE_SECONDS = 5  # refresh remote control at least this often (njsPC: ~3-4 s)
REPLY_SECONDS = 1.0  # per try, as njsPC
CONTROL_TRIES = 2  # per control frame (04/06/01), as njsPC
SETTLE_SECONDS = 1.0  # after run/speed commands, before reading status (as njsPC)
STATUS_TRIES = 2  # per cycle
OFFLINE_AFTER_MISSES = 3  # consecutive cycles without a status reply before entities show offline
OUR_ADDRESS = 0x21  # posing as a remote controller, as nodejs-poolController does

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Required("host"): cv.string,
                vol.Optional("port", default=9801): cv.port,
                vol.Optional("address", default=0x60): vol.All(vol.Coerce(int), vol.Range(min=0x60, max=0x6F)),
                vol.Optional("heat_min_rpm", default=2800): vol.All(vol.Coerce(int), vol.Range(min=MIN_RPM, max=MAX_RPM)),
                vol.Optional("heat_entities", default=[]): cv.entity_ids,
                vol.Optional("power_entities", default=[]): cv.entity_ids,
                vol.Optional("min_speed_entity"): cv.entity_id,
                vol.Optional("packet_log", default=True): cv.boolean,
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)


class MissedReply(Exception):
    """The bridge is connected but the pump didn't answer this cycle."""


class PumpController:
    """Owns the TCP link, the desired state, and the latest pump status."""

    def __init__(self, hass: HomeAssistant, config: dict) -> None:
        self.hass = hass
        self.host = config["host"]
        self.port = config["port"]
        self.address = config["address"]
        self.heat_min_rpm = config["heat_min_rpm"]
        self.heat_entities = config["heat_entities"]
        # The circuits that feed the pump (Compool Pool / Spa); with all of them off the drive is unpowered and silent.
        self.power_entities = config["power_entities"]
        # A sensor giving the minimum RPM right now (0 = no minimum); the pump is held at or above it.
        self.min_speed_entity = config.get("min_speed_entity")
        self.store = Store(hass, 1, DOMAIN)

        # Desired state (persisted).
        self.control = CONTROL_KEYPAD
        self.target_rpm = 2000
        self.run_requested = True

        # Observed state.
        self.status: PumpStatus | None = None
        self.connected = False
        self.holding_remote = False
        self.last_seen: float | None = None
        self.last_error: str | None = None
        self.commanded_rpm: int | None = None
        self.commanded_running: bool | None = None
        # What we're telling the pump and why, e.g. "Run 2800 rpm" / "minimum for heater firing".
        self.command = "Status only"
        self.command_reason = "pump keypad schedule"
        self.last_command_frames: list[str] = []
        # pentair_pump.ensure_speed: treat as heating until this time, so the pump is already up
        # to speed when the heater mode changes.
        self.prepare_heat_until = 0.0

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._wake = asyncio.Event()
        self._listeners: list[Callable[[], None]] = []
        self._task: asyncio.Task | None = None
        self._status_waiters: list[asyncio.Future] = []
        self.missed_cycles = 0

    # ---- state the entities read -------------------------------------------------------------

    @property
    def heating(self) -> bool:
        if time.monotonic() < self.prepare_heat_until:
            return True
        for entity_id in self.heat_entities:
            state = self.hass.states.get(entity_id)
            if state is not None and state.state not in ("off", "unknown", "unavailable"):
                return True
        return False

    @property
    def powered(self) -> bool | None:
        if not self.power_entities:
            return None
        states = [self.hass.states.get(entity_id) for entity_id in self.power_entities]
        known = [state.state for state in states if state is not None and state.state not in ("unknown", "unavailable")]
        if not known:
            return None
        return "on" in known

    @property
    def min_rpm(self) -> int:
        """The speed floor right now: heat entities / ensure_speed give heat_min_rpm, plus min_speed_entity."""
        floor = self.heat_min_rpm if self.heating else 0
        if self.min_speed_entity:
            state = self.hass.states.get(self.min_speed_entity)
            try:
                floor = max(floor, int(float(state.state))) if state else floor
            except ValueError:
                pass
        return min(MAX_RPM, floor) if floor > 0 else 0

    @property
    def interlock_active(self) -> bool:
        return self.min_rpm > 0

    def floor_reasons(self) -> list[str]:
        reasons = []
        if self.heating:
            reasons.append("heating")
        if self.min_speed_entity:
            state = self.hass.states.get(self.min_speed_entity)
            if state is not None:
                reasons += [str(reason) for reason in (state.attributes.get("reasons") or [])]
        return reasons

    def desired(self) -> tuple[bool, bool, int]:
        """(take_control, run, rpm) for this cycle."""
        take_control, run, rpm, _reason = self.desired_with_reason()
        return take_control, run, rpm

    def desired_with_reason(self) -> tuple[bool, bool, int, str]:
        floor = self.min_rpm
        if self.control == CONTROL_HOME_ASSISTANT:
            run, rpm = self.run_requested, self.target_rpm
            reason = f"Home Assistant target {rpm} rpm" if run else "stopped by Home Assistant"
        elif floor:
            # Keypad schedule, but something needs flow: keep its speed if it's already fast enough.
            run, rpm = True, self.status.rpm if self.status and self.status.running else floor
            reason = "keypad schedule"
        else:
            return False, False, 0, "pump keypad schedule"
        if floor and (not run or rpm < floor):
            # Stopped by the schedule (or too slow) but something needs flow: run at exactly the
            # floor, not at whatever speed was last scheduled.
            reason = f"minimum {floor} rpm for {' + '.join(self.floor_reasons()) or 'flow'}"
            rpm = max(rpm, floor) if run else floor
            run = True
        return True, run, max(MIN_RPM, min(MAX_RPM, rpm)), reason

    # ---- listeners -----------------------------------------------------------------------------

    @callback
    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    @callback
    def _notify(self) -> None:
        for listener in list(self._listeners):
            listener()

    # ---- desired-state changes (from entities / services) --------------------------------------

    async def async_load(self) -> None:
        saved = await self.store.async_load() or {}
        self.control = saved.get("control", self.control)
        self.target_rpm = saved.get("target_rpm", self.target_rpm)
        self.run_requested = saved.get("run_requested", self.run_requested)

    def _save(self) -> None:
        self.store.async_delay_save(
            lambda: {"control": self.control, "target_rpm": self.target_rpm, "run_requested": self.run_requested}, 1
        )

    async def async_set_control(self, control: str) -> None:
        if control != self.control:
            _LOGGER.info("Pentair pump control: %s -> %s", self.control, control)
        self.control = control
        self._save()
        self.wake()

    async def async_set_target_rpm(self, rpm: int) -> None:
        # Setting a speed means "run at this speed now" (the dashboard slider, or the schedule
        # starting a block); the schedule's next off-block turns it back off.
        self.target_rpm = max(MIN_RPM, min(MAX_RPM, int(rpm)))
        _LOGGER.info("Pentair pump target speed set to %d rpm (run)", self.target_rpm)
        self.run_requested = True
        self.control = CONTROL_HOME_ASSISTANT
        self._save()
        self.wake()

    async def async_set_run(self, run: bool) -> None:
        if run != self.run_requested:
            _LOGGER.info("Pentair pump %s requested", "run" if run else "stop")
        self.run_requested = run
        self.control = CONTROL_HOME_ASSISTANT
        self._save()
        self.wake()

    @callback
    def wake(self) -> None:
        self._notify()
        self._wake.set()

    async def async_wait_for_speed(self, rpm: int, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.status and self.status.running and self.status.rpm >= rpm - 30:
                return True
            future = self.hass.loop.create_future()
            self._status_waiters.append(future)
            try:
                await asyncio.wait_for(future, max(0.1, deadline - time.monotonic()))
            except asyncio.TimeoutError:
                break
        return bool(self.status and self.status.running and self.status.rpm >= rpm - 30)

    # ---- the loop ------------------------------------------------------------------------------

    def start(self) -> None:
        self._task = self.hass.async_create_background_task(self._run(), "pentair_pump loop")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            # Wait for the loop to finish: it may be mid-read on the socket, and a second reader
            # would fail and silently skip handing the pump back.
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if self.holding_remote and self._writer:
            try:
                await self._control(ACTION_REMOTE, b"\x00")
            except Exception:  # noqa: BLE001 - best effort on shutdown
                pass
        await self._close()

    async def _run(self) -> None:
        while True:
            try:
                await self._cycle()
                if self.missed_cycles:
                    _LOGGER.info("Pentair pump answering again after %d missed cycle(s)", self.missed_cycles)
                self.missed_cycles = 0
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except MissedReply as error:
                # The link is fine, the pump just didn't answer (bus noise, a collision). Keep the
                # socket and the last status; only call it offline after several misses in a row.
                self.missed_cycles += 1
                self.last_error = str(error)
                if self.missed_cycles == OFFLINE_AFTER_MISSES and self.powered is not False:
                    _LOGGER.warning("Pentair pump not answering (%d cycles in a row)", self.missed_cycles)
                if self.missed_cycles >= OFFLINE_AFTER_MISSES:
                    self.connected = False
            except Exception as error:  # noqa: BLE001 - keep the loop alive, report via entities
                if self.powered is not False and (self.connected or self.last_error != str(error)):
                    _LOGGER.warning("Pentair pump cycle failed: %s", error)
                self.last_error = str(error)
                self.connected = False
                await self._close()
            self._notify()
            for future in self._status_waiters:
                if not future.done():
                    future.set_result(None)
            self._status_waiters.clear()
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), CYCLE_SECONDS)
            except asyncio.TimeoutError:
                pass

    async def _cycle(self) -> None:
        if self._writer is None:
            self._reader, self._writer = await asyncio.wait_for(asyncio.open_connection(self.host, self.port), 5)
            _LOGGER.info("Pentair pump: connected to %s:%s", self.host, self.port)
            _PACKETS.info("--- connected to %s:%s", self.host, self.port)
        take_control, run, rpm, reason = self.desired_with_reason()
        command = (f"Run {rpm} rpm" if run else "Stop") if take_control else "Status only"
        if (command, reason) != (self.command, self.command_reason):
            _LOGGER.info("Pentair pump command: %s (%s) — was %s (%s)", command, reason, self.command, self.command_reason)
            self.command, self.command_reason = command, reason
        if take_control and not self.holding_remote:
            _LOGGER.info("Pentair pump: taking remote control")
        self._cycle_frames: list[str] = []
        if take_control:
            await self._control(ACTION_REMOTE, b"\xFF")
            self.holding_remote = True
            await self._control(ACTION_RUN, b"\x0A" if run else b"\x04")
            if run:
                await self._control(ACTION_SET, REGISTER_SPEED + bytes([rpm >> 8, rpm & 0xFF]))
            self.commanded_running, self.commanded_rpm = run, rpm if run else 0
            # Give the drive a moment before asking for status, so it reports the new command.
            await asyncio.sleep(SETTLE_SECONDS)
        elif self.holding_remote:
            _LOGGER.info("Pentair pump: releasing to the keypad schedule")
            await self._control(ACTION_REMOTE, b"\x00")
            self.holding_remote = False
            self.commanded_running = self.commanded_rpm = None
        self.last_command_frames = self._cycle_frames
        status = None
        for _ in range(STATUS_TRIES):
            reply = await self._exchange(ACTION_STATUS)
            status = PumpStatus.from_data(reply.data) if reply else None
            if status is not None:
                break
        if status is None:
            raise MissedReply("pump did not answer the status request")
        self.status = status
        self.connected = True
        self.last_seen = time.time()

    async def _control(self, action: int, data: bytes) -> bool:
        """Send a control frame, retrying once if the pump doesn't acknowledge it."""
        for attempt in range(1, CONTROL_TRIES + 1):
            if await self._exchange(action, data) is not None:
                return True
            if attempt < CONTROL_TRIES:
                _PACKETS.info("retrying action %#04x (try %d of %d)", action, attempt + 1, CONTROL_TRIES)
        _LOGGER.debug("Pentair pump: no acknowledgement for action %#04x after %d tries", action, CONTROL_TRIES)
        return False

    async def _exchange(self, action: int, data: bytes = b""):
        """Send one frame and return the pump's reply to that action (or None)."""
        assert self._reader and self._writer
        outgoing = build_frame(self.address, OUR_ADDRESS, action, data)
        self._log_frame("TX", outgoing)
        if action != ACTION_STATUS and hasattr(self, "_cycle_frames"):
            self._cycle_frames.append(outgoing.hex(" "))
        self._writer.write(outgoing)
        await self._writer.drain()
        buffer = bytearray()
        deadline = time.monotonic() + REPLY_SECONDS
        while (left := deadline - time.monotonic()) > 0:
            try:
                chunk = await asyncio.wait_for(self._reader.read(256), left)
            except asyncio.TimeoutError:
                break
            if not chunk:
                raise ConnectionError("EW11 closed the connection")
            buffer += chunk
            for frame in parse_frames(bytes(buffer)):
                if (frame.source == self.address and frame.action == action and frame.checksum_ok
                        and _acknowledges(action, data, frame.data)):
                    self._log_frame("RX", bytes(buffer))
                    return frame
        self._log_frame("RX", bytes(buffer), timed_out=True)
        return None

    def _log_frame(self, direction: str, raw: bytes, timed_out: bool = False) -> None:
        if not (_PACKETS.isEnabledFor(logging.INFO) or _LOGGER.isEnabledFor(logging.DEBUG)):
            return
        if not raw:
            line = f"{direction} (no reply within {REPLY_SECONDS:.1f} s)" if timed_out else f"{direction} (empty)"
        else:
            frames = parse_frames(raw)
            meaning = " ; ".join(describe_frame(frame) for frame in frames) or "unparsed bytes"
            line = f"{direction} {meaning} | {raw.hex(' ')}" + (" (no matching reply)" if timed_out else "")
        _PACKETS.info(line)
        _LOGGER.debug("packet %s", line)

    async def _close(self) -> None:
        if self._writer:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
        self._reader = self._writer = None
        self.holding_remote = False


def _acknowledges(action: int, sent: bytes, reply: bytes) -> bool:
    """Is this reply really the pump's answer to what we sent? (njsPC checks the same way.)"""
    if action == ACTION_STATUS:
        return True
    if action == ACTION_SET and len(sent) == 4:
        return reply[:2] == sent[2:4]  # the speed ack echoes the RPM
    return reply == sent  # remote / run acks mirror the request


def _setup_packet_log(path: str) -> None:
    """Rotating file like njsPC's packet log (opened in the executor: file I/O)."""
    if any(isinstance(handler, RotatingFileHandler) for handler in _PACKETS.handlers):
        return
    handler = RotatingFileHandler(path, maxBytes=PACKET_LOG_BYTES, backupCount=PACKET_LOG_BACKUPS)
    handler.setFormatter(logging.Formatter("%(asctime)s.%(msecs)03d %(message)s", "%Y-%m-%d %H:%M:%S"))
    _PACKETS.addHandler(handler)
    _PACKETS.setLevel(logging.INFO)
    _PACKETS.propagate = False  # keep the raw traffic out of home-assistant.log


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    if DOMAIN not in config:
        return True
    controller = PumpController(hass, config[DOMAIN])
    await controller.async_load()
    if config[DOMAIN]["packet_log"]:
        await hass.async_add_executor_job(_setup_packet_log, hass.config.path(PACKET_LOG_FILE))
    hass.data[DOMAIN] = controller

    @callback
    def wake_on_change(event) -> None:
        # Must be a @callback: a plain function/lambda is run in a worker thread, off the event loop.
        controller.wake()

    if controller.heat_entities:
        # React to heater changes straight away rather than on the next 10 s cycle.
        async_track_state_change_event(hass, controller.heat_entities, wake_on_change)
    if controller.min_speed_entity:
        async_track_state_change_event(hass, [controller.min_speed_entity], wake_on_change)
    if controller.power_entities:
        async_track_state_change_event(hass, controller.power_entities, wake_on_change)

    async def ensure_speed(call: ServiceCall):
        rpm = controller.heat_min_rpm
        timeout = call.data.get("timeout", 45)
        # Behave as if heating for the wait plus a grace period; once the caller turns the heater
        # on, the heater entity keeps the interlock engaged on its own.
        controller.prepare_heat_until = time.monotonic() + timeout + 60
        controller.wake()
        reached = await controller.async_wait_for_speed(rpm, timeout)
        result = {"reached": reached, "rpm": controller.status.rpm if controller.status else None}
        if not reached and call.data.get("raise_on_failure", True):
            raise HomeAssistantError(f"Pool pump did not reach {rpm} RPM within {timeout}s (now {result['rpm']})")
        return result

    hass.services.async_register(
        DOMAIN,
        "ensure_speed",
        ensure_speed,
        schema=vol.Schema(
            {
                vol.Optional("timeout", default=45): vol.All(vol.Coerce(float), vol.Range(min=1, max=300)),
                vol.Optional("raise_on_failure", default=True): cv.boolean,
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )

    controller.start()

    async def on_stop(event) -> None:
        await controller.stop()

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, on_stop)

    for platform in PLATFORMS:
        hass.async_create_task(async_load_platform(hass, platform, DOMAIN, {}, config))
    return True

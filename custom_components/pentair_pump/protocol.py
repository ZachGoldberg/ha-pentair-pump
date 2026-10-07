"""Pentair IntelliFlo / WhisperFlo VS RS-485 framing.

Frame: FF 00 FF A5 00 <dst> <src> <action> <length> <data...> <checksum hi> <checksum lo>
where the checksum is the byte sum from A5 through the last data byte.
"""
from __future__ import annotations

from dataclasses import dataclass

PREAMBLE = b"\xFF\x00\xFF"

ACTION_SET = 0x01  # register write; 02 C4 hi lo = run at RPM
ACTION_REMOTE = 0x04  # FF = remote control, 00 = back to the keypad
ACTION_RUN = 0x06  # 0A = run, 04 = stop
ACTION_STATUS = 0x07

REGISTER_SPEED = b"\x02\xC4"


def build_frame(destination: int, source: int, action: int, data: bytes = b"") -> bytes:
    body = bytes([0xA5, 0x00, destination, source, action, len(data)]) + data
    checksum = sum(body)
    return PREAMBLE + body + bytes([checksum >> 8, checksum & 0xFF])


@dataclass
class Frame:
    destination: int
    source: int
    action: int
    data: bytes
    checksum_ok: bool


def parse_frames(buffer: bytes) -> list[Frame]:
    frames = []
    index = 0
    while (index := buffer.find(b"\xA5", index)) != -1 and index + 6 <= len(buffer):
        length = buffer[index + 5]
        end = index + 6 + length + 2
        if end > len(buffer):
            break
        body = buffer[index : index + 6 + length]
        checksum_ok = sum(body) == (buffer[end - 2] << 8 | buffer[end - 1])
        frames.append(Frame(buffer[index + 2], buffer[index + 3], buffer[index + 4], bytes(buffer[index + 6 : index + 6 + length]), checksum_ok))
        index = end if checksum_ok else index + 1
    return frames


@dataclass
class PumpStatus:
    running: bool
    mode: int
    drive_state: int
    watts: int
    rpm: int
    error: int
    clock: str

    @classmethod
    def from_data(cls, data: bytes) -> "PumpStatus | None":
        if len(data) < 15:
            return None
        return cls(
            running=data[0] == 0x0A,
            mode=data[1],
            drive_state=data[2],
            watts=data[3] << 8 | data[4],
            rpm=data[5] << 8 | data[6],
            error=data[10],
            clock=f"{data[13]:02d}:{data[14]:02d}",
        )


# ---- human-readable frames (packet log) ---------------------------------------------------------

ADDRESS_NAMES = {0x10: "controller", 0x21: "HA", 0x0F: "broadcast"}


def address_name(address: int) -> str:
    if 0x60 <= address <= 0x6F:
        return f"pump{address - 0x5F}"
    return ADDRESS_NAMES.get(address, f"0x{address:02x}")


def describe_frame(frame: Frame) -> str:
    """One line in the spirit of njsPC's packet log: who → who, what it means."""
    data = frame.data
    to_pump = 0x60 <= frame.destination <= 0x6F
    if frame.action == ACTION_REMOTE and len(data) == 1:
        meaning = "take remote control" if data[0] == 0xFF else "release to keypad" if data[0] == 0x00 else f"remote {data[0]:#04x}"
        if not to_pump:
            meaning = "ack " + meaning
    elif frame.action == ACTION_RUN and len(data) == 1:
        meaning = "run" if data[0] == 0x0A else "stop" if data[0] == 0x04 else f"run/stop {data[0]:#04x}"
        if not to_pump:
            meaning = "ack " + meaning
    elif frame.action == ACTION_SET and len(data) == 4 and data[:2] == REGISTER_SPEED:
        meaning = f"set speed {data[2] << 8 | data[3]} rpm"
    elif frame.action == ACTION_SET and len(data) == 2 and not to_pump:
        meaning = f"ack set → {data[0] << 8 | data[1]}"
    elif frame.action == ACTION_STATUS and not data:
        meaning = "status request"
    elif frame.action == ACTION_STATUS:
        status = PumpStatus.from_data(data)
        meaning = (
            f"status {'running' if status.running else 'stopped'} {status.rpm} rpm {status.watts} W"
            f" err {status.error} drive {status.drive_state} clock {status.clock}"
            if status else f"status (short, {len(data)} bytes)"
        )
    else:
        meaning = f"action {frame.action:#04x} data {data.hex(' ') or '-'}"
    checksum = "" if frame.checksum_ok else " BAD CHECKSUM"
    return f"{address_name(frame.source)}→{address_name(frame.destination)} {meaning}{checksum}"

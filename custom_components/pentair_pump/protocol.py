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

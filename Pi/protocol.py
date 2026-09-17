"""LaserHat binary wire protocol — host (Pi) side.

Frames are exchanged over /dev/ttyS0 in both directions.  This module is
the single Python source of truth for the format; the firmware mirror is
Firmware/protocol.h.  Keep them in sync.

Frame layout
------------
    SYNC(2: DE AD) | TYPE(1) | payload (length implied by TYPE)

Magic-word framing: a receiver scans for the 2-byte SYNC to find a frame
boundary, reads TYPE, then the fixed number of payload bytes that TYPE
implies.  There is no length field, no byte-stuffing, and no CRC:

  * Integrity is end-to-end — every command is answered with RSP_STATUS
    (the "status-as-ack"), so the broker confirms the MCU actually holds
    the values it sent; STATUS / event fields are range-checked on receipt.
  * The MCU's only inbound payload is CMD_CONFIG (bounded i/r/h, no
    full-range field), and the broker guarantees a CONFIG payload never
    contains the SYNC bytes (see avoid_magic), so the MCU's resync is
    exact without a CRC.

Pure stdlib.
"""

from __future__ import annotations

import struct
from typing import Iterator, Tuple

# --- framing -------------------------------------------------------------
SYNC = b"\xDE\xAD"                 # both bytes >= 0x99 (see avoid_magic)
# The only place SYNC could collide with a CONFIG payload is the low 16
# bits of ramp or hold (the only adjacent full-range byte pair); this is
# that 16-bit value, little-endian: r0=0xDE, r1=0xAD -> 0xADDE.
MAGIC16 = SYNC[0] | (SYNC[1] << 8)

# --- message types -------------------------------------------------------
# Host -> MCU (commands), high bit clear.
CMD_CONFIG        = 0x01    # i u16, r u32, h u32
CMD_TRIGGER       = 0x02    # —
CMD_QUERY         = 0x03    # —
CMD_SET_MODE      = 0x04    # mode u8 (MODE_LASER / MODE_ESTIM)
CMD_ESTIM_CONFIG  = 0x05    # pulse_dur_ticks u32, ipi_ticks u32
CMD_ABORT         = 0x06    # — (stop the running pulse / repeat train)

# MCU -> Host (responses + events), high bit set.
RSP_STATUS      = 0x81  # i u16, r u32, h u32, btn u8, phase u8, tick u32,
                         # mode u8, estim_dur u32, estim_ipi u32
EVT_PULSE_START = 0x82  # tick u32
EVT_PULSE_END   = 0x83  # tick u32
EVT_BUTTON      = 0x84  # mask u8, edges u8
EVT_TRAIN_END   = 0x85  # tick u32 (repeat train stopped; machine idle)

# Phase byte values in RSP_STATUS (ASCII 'W' / 'T' / 'G').
#   W  waiting (idle)
#   T  a pulse is in progress
#   G  in the 5 s gap between pulses of a repeat train (outputs safe)
PHASE_WAITING   = ord("W")
PHASE_TRIGGERED = ord("T")
PHASE_TRAIN_GAP = ord("G")
_PHASE_CHAR = {PHASE_WAITING: "W", PHASE_TRIGGERED: "T", PHASE_TRAIN_GAP: "G"}
_PHASE_BYTE = {v: k for k, v in _PHASE_CHAR.items()}

# Mode byte: bit 0 = stimulus type (laser / estim), bit 1 = REPEAT.  With
# REPEAT set, a trigger re-fires the configured single pulse every
# REPEAT_PERIOD_MS until CMD_ABORT (or B1), for repeated measurements.
MODE_LASER  = 0x00
MODE_ESTIM  = 0x01
MODE_REPEAT = 0x02
MODE_MASK   = 0x03
REPEAT_PERIOD_MS = 5000     # firmware constant; informational here

_CONFIG       = struct.Struct("<HII")       # i, r, h
_STATUS       = struct.Struct("<HIIBBIBII") # i, r, h, btn, phase, tick, mode, estim_dur, estim_ipi
_ESTIM_CONFIG = struct.Struct("<II")        # pulse_dur_ticks, ipi_ticks
_U16 = struct.Struct("<H")
_U32 = struct.Struct("<I")

# Payload length by type, per direction (decoders only accept their inbound
# set; an out-of-direction type is treated as unknown -> resync).
CMD_LEN = {CMD_CONFIG: 10, CMD_TRIGGER: 0, CMD_QUERY: 0,
           CMD_SET_MODE: 1, CMD_ESTIM_CONFIG: 8, CMD_ABORT: 0}
RSP_LEN = {RSP_STATUS: _STATUS.size, EVT_PULSE_START: 4, EVT_PULSE_END: 4,
           EVT_BUTTON: 2, EVT_TRAIN_END: 4}

# Field ranges, for validating a decoded STATUS (false-sync rejection).
INTENSITY_MIN, INTENSITY_MAX = 1, 320
TICKS_MIN, TICKS_MAX = 1, 10_000_000
ESTIM_TICKS_MIN, ESTIM_TICKS_MAX = 1, 1000


# --- frame encode --------------------------------------------------------
def encode_frame(msg_type: int, payload: bytes = b"") -> bytes:
    return SYNC + bytes([msg_type]) + payload


def avoid_magic(ticks: int) -> int:
    """Nudge a ramp/hold value so its low 16 bits can't equal the SYNC word,
    guaranteeing a CONFIG payload never contains the magic.  Excludes only
    the ~150 values per field whose low 16 bits == 0xADDE."""
    if ticks & 0xFFFF == MAGIC16:
        return ticks - 1 if ticks >= TICKS_MAX else ticks + 1
    return ticks


def pack_config(intensity: int, ramp: int, hold: int) -> bytes:
    return encode_frame(CMD_CONFIG, _CONFIG.pack(intensity, ramp, hold))


def pack_set_mode(mode: int) -> bytes:
    return encode_frame(CMD_SET_MODE, bytes([mode]))


def pack_estim_config(dur_ticks: int, ipi_ticks: int) -> bytes:
    return encode_frame(CMD_ESTIM_CONFIG, _ESTIM_CONFIG.pack(dur_ticks, ipi_ticks))


def pack_status(intensity: int, ramp: int, hold: int, button_mask: int,
                phase: str, tick: int, mode: int = MODE_LASER,
                estim_dur_ticks: int = 10, estim_ipi_ticks: int = 10) -> bytes:
    phase_byte = _PHASE_BYTE.get(phase, PHASE_TRIGGERED)
    return encode_frame(RSP_STATUS, _STATUS.pack(
        intensity, ramp, hold, button_mask, phase_byte, tick,
        mode, estim_dur_ticks, estim_ipi_ticks))


def unpack_config(payload: bytes) -> Tuple[int, int, int]:
    return _CONFIG.unpack(payload)


def unpack_status(payload: bytes) -> dict:
    i, r, h, btn, phase, tick, mode, estim_dur, estim_ipi = _STATUS.unpack(payload)
    return {
        "intensity": i, "ramp_ticks": r, "hold_ticks": h,
        "button_mask": btn,
        "phase": _PHASE_CHAR.get(phase, "?"),
        "tick": tick,
        "mode": mode,
        "estim_dur_ticks": estim_dur,
        "estim_ipi_ticks": estim_ipi,
    }


def status_in_range(fields: dict) -> bool:
    """Sanity-check a decoded STATUS to reject a false sync (no CRC)."""
    return (INTENSITY_MIN <= fields["intensity"] <= INTENSITY_MAX
            and TICKS_MIN <= fields["ramp_ticks"] <= TICKS_MAX
            and TICKS_MIN <= fields["hold_ticks"] <= TICKS_MAX
            and fields["button_mask"] <= 0x0F
            and fields["phase"] in ("W", "T", "G")
            and 0 <= fields["mode"] <= MODE_MASK
            and ESTIM_TICKS_MIN <= fields["estim_dur_ticks"] <= ESTIM_TICKS_MAX
            and ESTIM_TICKS_MIN <= fields["estim_ipi_ticks"] <= ESTIM_TICKS_MAX)


# --- stream decode (Pi inbound = responses/events) ----------------------
class StreamDecoder:
    """Feed raw RX bytes; iterate complete frames.

    Scans for the SYNC word, reads TYPE, then the type-implied payload
    length.  An unrecognised TYPE (or a SYNC that turns out to be inside
    data) costs one byte of resync.  No CRC — callers range-check STATUS.
    """

    def __init__(self, lengths: dict = None) -> None:
        self._buf = bytearray()
        self._len = RSP_LEN if lengths is None else lengths

    def feed(self, data: bytes) -> Iterator[Tuple[int, bytes]]:
        buf = self._buf
        buf.extend(data)
        while True:
            j = buf.find(SYNC)
            if j < 0:
                # Keep only a possible partial SYNC (last byte).
                if buf:
                    del buf[:-1]
                return
            if j:
                del buf[:j]                 # discard junk before SYNC
            if len(buf) < 3:
                return                       # need TYPE
            mtype = buf[2]
            plen = self._len.get(mtype)
            if plen is None:
                del buf[:1]                  # false SYNC; advance and rescan
                continue
            # A SYNC inside the would-be payload means this frame was
            # truncated (dropped byte) — resync at that inner SYNC.  The
            # window extends one byte past the payload so a SYNC whose DE is
            # the last payload byte (AD just after) is still caught; a SYNC
            # starting exactly at the payload end is the legit next frame.
            inner = buf.find(SYNC, 3, 3 + plen + 1)
            if inner != -1:
                del buf[:inner]
                continue
            if len(buf) < 3 + plen:
                return                       # need full payload
            payload = bytes(buf[3:3 + plen])
            del buf[:3 + plen]
            yield mtype, payload

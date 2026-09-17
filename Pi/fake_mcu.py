#!/usr/bin/env python3
"""Fake MSPM0 — a PTY that speaks the magic-framed protocol, for testing
broker.py / hat_client.py / web_app.py off-hardware.

Opens a pseudo-terminal, prints the slave device path on stdout, and
answers the wire protocol: every command is answered with RSP_STATUS
(status-as-ack).  A trigger fires one pulse (EVT_PULSE_START /
EVT_PULSE_END); with MODE_REPEAT set it re-fires every REPEAT_PERIOD_S
until CMD_ABORT, then EVT_TRAIN_END.  Phase is "T" during a pulse, "G"
between repeats, "W" idle.  (The real firmware repeats every 5 s; the
fake uses a short period so tests stay quick.)

    python3 fake_mcu.py            # prints e.g. /dev/pts/7
    python3 broker.py --device /dev/pts/7 --no-gpio --socket /tmp/lh.sock
"""

from __future__ import annotations

import os
import pty
import struct
import sys
import threading
import time
import tty

import protocol as proto

PULSE_HOLD_S = 0.15
REPEAT_PERIOD_S = 0.4        # firmware: 5 s (REPEAT_PERIOD_MS)


class FakeMCU:
    def __init__(self, fd: int):
        self._fd = fd
        self._cmd = proto.StreamDecoder(proto.CMD_LEN)   # decode commands
        self._lock = threading.Lock()
        self._t0 = time.monotonic()
        self._abort = threading.Event()
        self.state = {"intensity": 320, "ramp_ticks": 8000, "hold_ticks": 10000,
                      "button_mask": 0, "phase": "W",
                      "mode": proto.MODE_LASER,
                      "estim_dur_ticks": 10, "estim_ipi_ticks": 10}

    def _tick(self) -> int:
        return int((time.monotonic() - self._t0) * 100_000) & 0xFFFFFFFF

    def _send(self, msg_type: int, payload: bytes = b"") -> None:
        with self._lock:
            os.write(self._fd, proto.encode_frame(msg_type, payload))

    def _send_status(self) -> None:
        s = self.state
        self._send(proto.RSP_STATUS, proto._STATUS.pack(
            s["intensity"], s["ramp_ticks"], s["hold_ticks"],
            s["button_mask"], proto._PHASE_BYTE[s["phase"]], self._tick(),
            s["mode"], s["estim_dur_ticks"], s["estim_ipi_ticks"]))

    def _run_train(self) -> None:
        """Mirror of the firmware's pulse / repeat logic, at PTY fidelity."""
        s = self.state
        self._abort.clear()
        aborted = False
        while True:
            pulse_start = time.monotonic()
            s["phase"] = "T"
            self._send(proto.EVT_PULSE_START, struct.pack("<I", self._tick()))
            if self._abort.wait(PULSE_HOLD_S):
                aborted = True
                break
            self._send(proto.EVT_PULSE_END, struct.pack("<I", self._tick()))
            if not (s["mode"] & proto.MODE_REPEAT):   # latched per pulse
                break
            s["phase"] = "G"
            gap = pulse_start + REPEAT_PERIOD_S - time.monotonic()
            if self._abort.wait(max(gap, 0.0)):
                aborted = True
                break
        if aborted:
            if s["phase"] == "T":
                self._send(proto.EVT_PULSE_END, struct.pack("<I", self._tick()))
            s["phase"] = "W"
            self._send(proto.EVT_TRAIN_END, struct.pack("<I", self._tick()))
        s["phase"] = "W"

    def press_button(self, mask: int, edges: int) -> None:
        """Test hook: simulate a debounced button change."""
        self.state["button_mask"] = mask
        self._send(proto.EVT_BUTTON, bytes([mask, edges]))

    def handle(self, mtype: int, payload: bytes) -> None:
        s = self.state
        if mtype == proto.CMD_CONFIG and len(payload) == 10:
            i, r, h = proto.unpack_config(payload)
            if 1 <= i <= 320 and 1 <= r <= 10_000_000 and 1 <= h <= 10_000_000:
                s.update(intensity=i, ramp_ticks=r, hold_ticks=h)
            self._send_status()          # status-as-ack (echoes result)
        elif mtype == proto.CMD_TRIGGER:
            if s["phase"] == "W":
                threading.Thread(target=self._run_train, daemon=True).start()
            self._send_status()
        elif mtype == proto.CMD_QUERY:
            self._send_status()
        elif mtype == proto.CMD_SET_MODE and len(payload) == 1:
            if payload[0] <= proto.MODE_MASK and s["phase"] == "W":
                s["mode"] = payload[0]
            self._send_status()
        elif mtype == proto.CMD_ESTIM_CONFIG and len(payload) == 8:
            d, ipi = proto._ESTIM_CONFIG.unpack(payload)
            if (proto.ESTIM_TICKS_MIN <= d <= proto.ESTIM_TICKS_MAX
                    and proto.ESTIM_TICKS_MIN <= ipi <= proto.ESTIM_TICKS_MAX):
                s.update(estim_dur_ticks=d, estim_ipi_ticks=ipi)
            self._send_status()
        elif mtype == proto.CMD_ABORT:
            if s["phase"] != "W":
                self._abort.set()
            self._send_status()

    def run(self) -> None:
        while True:
            try:
                data = os.read(self._fd, 256)
            except OSError:
                return
            if not data:
                return
            for mtype, payload in self._cmd.feed(data):
                self.handle(mtype, payload)


def main() -> int:
    master_fd, slave_fd = pty.openpty()
    tty.setraw(master_fd)
    tty.setraw(slave_fd)
    slave_name = os.ttyname(slave_fd)
    print(slave_name, flush=True)
    print(f"fake_mcu: serving on {slave_name}", file=sys.stderr)

    mcu = FakeMCU(master_fd)
    try:
        mcu.run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

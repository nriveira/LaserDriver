"""Network trigger: a UDP datagram from the acquisition host fires GPIO 24.

This is the closed-loop path.  Open Ephys (the ripple detector) sends one
datagram per detection; the broker's listener fires the same PiTrigger the
`trigger_gpio` command uses, so the MCU sees an edge on PA19 within
~50-100 us of the datagram arriving.

Datagram (16 bytes, little-endian):

    offset  size  field
    0       4     magic   b"LTR1"
    4       4     seq     u32, +1 per datagram the sender emits
    8       8     sample  u64, sender's sample number of the detection

`seq` makes loss visible: a jump of more than one is counted as a gap, so a
dropped trigger is a number in the stats rather than a silent miss.
`sample` is not interpreted here; it is echoed in the broker event so a log
can line a stimulus up with the recording.

Only allow-listed source addresses are accepted -- this fires a laser, and
any host on the subnet can send a datagram.

The layout is mirrored by the ripple detector's sender (RippleDetector,
LaserTrigger); change both together.

CLI smoke tool, from the acquisition host:

    python3 udp_trigger.py send <pi-host> [--port N] [--count N] [--interval S]
"""

from __future__ import annotations

import socket
import struct
import sys
import threading
import time
from typing import Callable, Iterable, Optional

MAGIC = b"LTR1"
PACKET = struct.Struct("<4sIQ")
DEFAULT_PORT = 27136                    # 0x6A00


class UdpTrigger:
    """Listen for trigger datagrams and call `fire` for each accepted one.

    `on_event` receives a dict per accepted datagram (seq, sample, gap) so the
    broker can publish it.  Counters are read with `stats()`.
    """

    def __init__(self, fire: Callable[[], bool], port: int,
                 allow: Iterable[str], bind: str = "0.0.0.0",
                 on_event: Optional[Callable[[dict], None]] = None):
        self._fire = fire
        self._allow = frozenset(allow)
        self._on_event = on_event
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind((bind, port))
        self.port = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._stats = {"received": 0, "fired": 0, "fire_failed": 0,
                       "rejected_source": 0, "malformed": 0,
                       "seq_gaps": 0, "last_seq": None, "last_sample": None}

    def stats(self) -> dict:
        with self._lock:
            return dict(self._stats)

    def serve_forever(self) -> None:
        buf = bytearray(64)
        view = memoryview(buf)
        while not self._stop.is_set():
            try:
                n, (src, _) = self._sock.recvfrom_into(buf)
            except OSError:
                if self._stop.is_set():
                    return
                raise
            self._handle(view[:n], src)

    def _handle(self, data: memoryview, src: str) -> None:
        with self._lock:
            self._stats["received"] += 1
            if src not in self._allow:
                self._stats["rejected_source"] += 1
                return
            if len(data) != PACKET.size:
                self._stats["malformed"] += 1
                return
            magic, seq, sample = PACKET.unpack(data)
            if magic != MAGIC:
                self._stats["malformed"] += 1
                return
        # Fire before any bookkeeping: this call is the latency path.
        ok = self._fire()
        with self._lock:
            last = self._stats["last_seq"]
            # A seq at or below the last one is a restarted sender (a new
            # acquisition run), not billions of lost datagrams.
            gap = 0 if last is None or seq <= last else seq - last - 1
            self._stats["seq_gaps"] += gap
            self._stats["last_seq"] = seq
            self._stats["last_sample"] = sample
            self._stats["fired" if ok else "fire_failed"] += 1
        if self._on_event is not None:
            self._on_event({"type": "event", "event": "udp_trigger",
                            "seq": seq, "sample": sample, "gap": gap,
                            "src": src, "ok": ok})

    def close(self) -> None:
        self._stop.set()
        self._sock.close()


def send(host: str, port: int, count: int, interval: float) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    for seq in range(count):
        sock.sendto(PACKET.pack(MAGIC, seq, 0), (host, port))
        if seq + 1 < count:
            time.sleep(interval)
    sock.close()


def _main() -> int:
    import argparse

    p = argparse.ArgumentParser(description="Send LaserHAT UDP triggers")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("send", help="send trigger datagrams to a broker")
    s.add_argument("host")
    s.add_argument("--port", type=int, default=DEFAULT_PORT)
    s.add_argument("--count", type=int, default=1)
    s.add_argument("--interval", type=float, default=1.0,
                   help="seconds between datagrams")
    args = p.parse_args()
    send(args.host, args.port, args.count, args.interval)
    print(f"sent {args.count} trigger(s) to {args.host}:{args.port}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())

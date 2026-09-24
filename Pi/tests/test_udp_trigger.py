"""Tests for the UDP network trigger (Pi/udp_trigger.py) and its broker hookup.

Run:  python3 -m pytest Pi/tests/test_udp_trigger.py
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PI = os.path.join(HERE, "..")
sys.path.insert(0, PI)

import udp_trigger as ut  # noqa: E402


def _wait(predicate, timeout=5.0, interval=0.01):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(interval)
    return False


class _Listener:
    def __init__(self, allow=("127.0.0.1",)):
        self.fires = 0
        self.events = []
        self.trig = ut.UdpTrigger(self._fire, 0, allow, bind="127.0.0.1",
                                  on_event=self.events.append)
        self.thread = threading.Thread(target=self.trig.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def _fire(self):
        self.fires += 1
        return True

    def send(self, payload):
        self.tx.sendto(payload, ("127.0.0.1", self.trig.port))

    def trigger(self, seq, sample=0):
        self.send(ut.PACKET.pack(ut.MAGIC, seq, sample))

    def close(self):
        self.tx.close()
        self.trig.close()


def test_fires_and_reports_sample():
    L = _Listener()
    try:
        L.trigger(0, sample=123456789012)
        assert _wait(lambda: L.fires == 1)
        s = L.trig.stats()
        assert s["fired"] == 1 and s["seq_gaps"] == 0
        assert s["last_sample"] == 123456789012
        assert L.events[0]["event"] == "udp_trigger"
        assert L.events[0]["sample"] == 123456789012
    finally:
        L.close()


def test_counts_sequence_gaps():
    L = _Listener()
    try:
        for seq in (10, 11, 14, 15):        # 12 and 13 lost
            L.trigger(seq)
        assert _wait(lambda: L.fires == 4)
        assert L.trig.stats()["seq_gaps"] == 2
        assert [e["gap"] for e in L.events] == [0, 0, 2, 0]
    finally:
        L.close()


def test_sender_restart_is_not_a_gap():
    L = _Listener()
    try:
        for seq in (40, 41, 0, 1):
            L.trigger(seq)
        assert _wait(lambda: L.fires == 4)
        assert L.trig.stats()["seq_gaps"] == 0
    finally:
        L.close()


def test_rejects_malformed_without_firing():
    L = _Listener()
    try:
        L.send(b"LTR1" + b"\x00" * 4)                        # short
        L.send(ut.PACKET.pack(b"XXXX", 0, 0))                # bad magic
        L.send(ut.PACKET.pack(ut.MAGIC, 0, 0) + b"\x00")     # long
        L.trigger(1)
        assert _wait(lambda: L.fires == 1)
        s = L.trig.stats()
        assert s["malformed"] == 3 and s["received"] == 4
    finally:
        L.close()


def test_rejects_unlisted_source():
    L = _Listener(allow=("192.0.2.1",))
    try:
        L.trigger(0)
        assert _wait(lambda: L.trig.stats()["rejected_source"] == 1)
        assert L.fires == 0
    finally:
        L.close()


def test_broker_publishes_udp_trigger_event():
    """End to end: datagram -> broker -> event on the Unix socket."""
    py = sys.executable
    sock_path = f"/tmp/lh_udp_test_{os.getpid()}.sock"
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    fake = subprocess.Popen([py, os.path.join(PI, "fake_mcu.py")],
                            stdout=subprocess.PIPE, text=True)
    slave = fake.stdout.readline().strip()
    broker = subprocess.Popen(
        [py, os.path.join(PI, "broker.py"), "--device", slave, "--no-gpio",
         "--socket", sock_path, "--udp-trigger-port", str(port),
         "--udp-trigger-bind", "127.0.0.1",
         "--udp-trigger-allow", "127.0.0.1"])
    client = None
    try:
        assert _wait(lambda: os.path.exists(sock_path))
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(sock_path)
        rx = client.makefile("r")

        tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        tx.sendto(ut.PACKET.pack(ut.MAGIC, 7, 42), ("127.0.0.1", port))
        tx.close()

        client.settimeout(5.0)
        event = None
        for line in rx:
            msg = json.loads(line)
            if msg.get("event") == "udp_trigger":
                event = msg
                break
        assert event is not None
        assert event["seq"] == 7 and event["sample"] == 42
        assert event["ok"] is False          # --no-gpio: nothing to fire

        client.sendall(b'{"cmd": "udp_stats"}\n')
        for line in rx:
            msg = json.loads(line)
            if msg.get("cmd") == "udp_stats":
                assert msg["ok"] and msg["received"] == 1
                assert msg["fire_failed"] == 1
                break
    finally:
        if client is not None:
            client.close()
        broker.terminate()
        fake.terminate()
        broker.wait(timeout=5)
        fake.wait(timeout=5)

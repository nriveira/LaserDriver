"""End-to-end off-hardware test: fake_mcu <-> broker <-> hat_client.

Spawns the PTY simulator and the broker as subprocesses, then drives the
broker through a HatClient and checks that commands ACK, state mirrors
edits, and pulse/button events propagate.

Run:  python3 Pi/tests/test_integration.py
  or: python3 -m pytest Pi/tests/test_integration.py
"""

import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PI = os.path.join(HERE, "..")
sys.path.insert(0, PI)

import hat_client  # noqa: E402


def _wait(predicate, timeout=5.0, interval=0.02):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(interval)
    return False


def run() -> int:
    py = sys.executable
    sock = f"/tmp/lh_test_{os.getpid()}.sock"

    fake = subprocess.Popen([py, os.path.join(PI, "fake_mcu.py")],
                            stdout=subprocess.PIPE, text=True)
    slave = fake.stdout.readline().strip()
    assert slave, "fake_mcu did not report a device"

    broker = subprocess.Popen(
        [py, os.path.join(PI, "broker.py"),
         "--device", slave, "--no-gpio", "--socket", sock])

    failures = []
    try:
        assert _wait(lambda: os.path.exists(sock)), "broker socket never appeared"

        events = []
        client = hat_client.HatClient(sock, on_update=events.append)

        # MCU comes alive (broker polls QUERY, gets RSP_STATUS).
        if not _wait(lambda: client.get_state() is not None):
            failures.append("state never became alive")

        st = client.get_state()
        if st and st.intensity != 320:
            failures.append(f"unexpected default intensity {st.intensity}")

        # set intensity -> ACK ok, and the cached state mirrors it.
        if not client.set_intensity(200):
            failures.append("set_intensity not acked ok")
        if not _wait(lambda: getattr(client.get_state(), "intensity", None) == 200):
            failures.append("intensity edit did not reflect in state")

        # out-of-range set is rejected.
        if client.set_intensity(9999):
            failures.append("out-of-range intensity wrongly accepted")

        # trigger -> ACK ok, and pulse_start/pulse_end events propagate.
        del events[:]
        if not client.trigger():
            failures.append("trigger not acked ok")
        got_start = _wait(lambda: any(e.get("event") == "pulse_start" for e in events))
        got_end = _wait(lambda: any(e.get("event") == "pulse_end" for e in events))
        if not (got_start and got_end):
            failures.append(f"missing pulse events (start={got_start} end={got_end})")
        if not _wait(lambda: any(e.get("event") == "train_end" for e in events)):
            failures.append("single pulse did not end with train_end")

        def n_events(name):
            return sum(1 for e in events if e.get("event") == name)

        # --- pulse train: 3 pulses, 250 ms apart -> 3 start/end pairs, then
        # train_end; progress and phase mirror in state along the way.
        if not client.set_train_count(3):
            failures.append("set_train_count(3) not acked ok")
        if not client.set_train_period(250):
            failures.append("set_train_period(250) not acked ok")
        if not _wait(lambda: (getattr(client.get_state(), "train_count", None),
                              getattr(client.get_state(), "train_period_ms", None))
                     == (3, 250)):
            failures.append("train config did not reflect in state")
        if client.set_train_count(99_999):
            failures.append("out-of-range train count wrongly accepted")
        if client.set_train_period(1):
            failures.append("out-of-range train period wrongly accepted")

        del events[:]
        if not client.trigger():
            failures.append("train trigger not acked ok")
        saw_gap = _wait(lambda: getattr(client.get_state(), "phase", None) == "G",
                        timeout=1.0)
        if not _wait(lambda: n_events("train_end") == 1, timeout=3.0):
            failures.append("train_end never arrived")
        if (n_events("pulse_start"), n_events("pulse_end")) != (3, 3):
            failures.append(f"train fired {n_events('pulse_start')}/"
                            f"{n_events('pulse_end')} start/end, expected 3/3")
        if not saw_gap:
            failures.append("phase never showed 'G' during the train")
        if not _wait(lambda: getattr(client.get_state(), "phase", None) == "W"):
            failures.append("phase not back to W after train")
        st = client.get_state()
        if st and st.train_done != 3:
            failures.append(f"train_done {st.train_done}, expected 3")

        # --- unlimited train (count 0) runs until abort.
        if not client.set_train_count(0):
            failures.append("set_train_count(0) not acked ok")
        del events[:]
        if not client.trigger():
            failures.append("unlimited train trigger not acked ok")
        if not _wait(lambda: n_events("pulse_start") >= 2, timeout=2.0):
            failures.append("unlimited train did not repeat")
        if not client.abort():
            failures.append("abort not acked ok")
        if not _wait(lambda: n_events("train_end") == 1):
            failures.append("abort: no train_end")
        if not _wait(lambda: getattr(client.get_state(), "phase", None) == "W"):
            failures.append("abort: phase not back to W")
        if not client.set_train_count(1):
            failures.append("set_train_count(1) restore not acked ok")

        client.close()
    finally:
        broker.terminate()
        fake.terminate()
        broker.wait(timeout=5)
        fake.wait(timeout=5)
        try:
            os.unlink(sock)
        except OSError:
            pass

    if failures:
        for f in failures:
            print("FAIL:", f)
        return 1
    print("integration OK: alive, set+mirror, range-reject, pulse events, "
          "train (3x + gap phase), unlimited train + abort")
    return 0


def test_integration():
    assert run() == 0


if __name__ == "__main__":
    raise SystemExit(run())

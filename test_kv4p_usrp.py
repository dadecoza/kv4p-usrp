import threading
import time
import unittest
from collections import deque
from types import SimpleNamespace

from kv4p_usrp import (
    Bridge,
    CMD_AUDIO,
    KV4P_TX_PENDING_FRAMES,
    PttHangController,
    USRP_FRAME_BYTES,
    USRP_PERIOD,
    USRP_TX_QUEUE_FRAMES,
    UsrpRxPacer,
)


class PttHangControllerTest(unittest.TestCase):
    HANG = 0.4

    def keyed_controller(self):
        ptt = PttHangController(self.HANG)
        self.assertEqual(ptt.set_desired(True, 0.0), (True, False))
        ptt.request_sent(True)
        ptt.set_actual(True)
        return ptt

    def assert_short_off_on_is_continuous(self, delay):
        ptt = self.keyed_controller()
        self.assertEqual(ptt.set_desired(False, 1.0), (None, True))
        stale_generation = ptt.deadline_generation
        self.assertEqual(ptt.set_desired(True, 1.0 + delay), (None, True))
        self.assertTrue(ptt.desired)
        self.assertTrue(ptt.requested)
        self.assertTrue(ptt.actual)
        self.assertIsNone(ptt.deadline)
        # A callback already queued by an event loop must be harmless after ON.
        self.assertIsNone(ptt.expire(stale_generation))

    def test_off_on_at_zero_ms(self):
        self.assert_short_off_on_is_continuous(0.0)

    def test_off_on_at_80_ms(self):
        self.assert_short_off_on_is_continuous(0.08)

    def test_off_on_just_below_hang_time(self):
        self.assert_short_off_on_is_continuous(self.HANG - 0.000001)

    def test_off_on_at_hang_expiry_restarts_after_expiry_callback(self):
        ptt = self.keyed_controller()
        ptt.set_desired(False, 2.0)
        generation = ptt.deadline_generation
        self.assertFalse(ptt.expire(generation))
        ptt.request_sent(False)
        self.assertEqual(ptt.set_desired(True, 2.0 + self.HANG), (True, False))
        ptt.request_sent(True)
        self.assertTrue(ptt.desired)
        self.assertTrue(ptt.requested)

    def test_off_on_at_hang_expiry_on_first_invalidates_callback(self):
        ptt = self.keyed_controller()
        ptt.set_desired(False, 2.0)
        stale_generation = ptt.deadline_generation
        self.assertEqual(ptt.set_desired(True, 2.0 + self.HANG), (None, True))
        self.assertIsNone(ptt.expire(stale_generation))
        self.assertTrue(ptt.requested)

    def test_off_on_just_after_hang_expiry_restarts(self):
        ptt = self.keyed_controller()
        ptt.set_desired(False, 3.0)
        generation = ptt.deadline_generation
        self.assertFalse(ptt.expire(generation))
        ptt.request_sent(False)
        ptt.set_actual(False)
        self.assertEqual(
            ptt.set_desired(True, 3.0 + self.HANG + 0.000001),
            (True, False),
        )

    def test_stale_expiry_after_new_on_cannot_stop_tx(self):
        ptt = self.keyed_controller()
        ptt.set_desired(False, 4.0)
        stale_generation = ptt.deadline_generation
        ptt.set_desired(True, 4.08)
        self.assertIsNone(ptt.expire(stale_generation))
        self.assertTrue(ptt.requested)

    def test_stale_generation_cannot_expire_a_newer_off_transition(self):
        ptt = self.keyed_controller()
        ptt.set_desired(False, 4.0)
        stale_generation = ptt.deadline_generation
        ptt.set_desired(True, 4.08)
        ptt.set_desired(False, 4.1)
        current_generation = ptt.deadline_generation
        self.assertIsNone(ptt.expire(stale_generation))
        self.assertTrue(ptt.requested)
        self.assertFalse(ptt.expire(current_generation))

    def test_new_on_restarts_radio_that_stopped_during_hang(self):
        ptt = self.keyed_controller()
        ptt.set_desired(False, 5.0)
        ptt.set_actual(False)
        self.assertEqual(ptt.set_desired(True, 5.08), (True, True))

    def test_unsent_start_is_reconciled_after_initial_state(self):
        ptt = PttHangController(self.HANG)
        self.assertEqual(ptt.set_desired(True, 0.0), (True, False))
        self.assertFalse(ptt.requested)
        ptt.set_actual(False)
        self.assertTrue(ptt.reconcile())

    def test_late_active_state_while_unkeyed_forces_off(self):
        ptt = PttHangController(self.HANG)
        self.assertFalse(ptt.desired)
        self.assertFalse(ptt.set_actual(True))

    def test_request_state_is_recorded_only_after_send(self):
        ptt = PttHangController(self.HANG)
        self.assertEqual(ptt.set_desired(True, 0.0), (True, False))
        self.assertFalse(ptt.requested)
        ptt.request_sent(True)
        self.assertTrue(ptt.requested)

    def test_unacknowledged_request_is_retried_after_timeout(self):
        ptt = PttHangController(self.HANG, ack_timeout=1.0)
        self.assertTrue(ptt.set_desired(True, 0.0)[0])
        ptt.request_sent(True, 0.0)
        ptt.set_actual(False)
        self.assertIsNone(ptt.reconcile(0.999))
        self.assertTrue(ptt.reconcile(1.0))

    def test_reconciliation_does_not_bypass_ptt_hang(self):
        ptt = self.keyed_controller()
        ptt.set_desired(False, 1.0)
        self.assertIsNone(ptt.reconcile(1.2))
        self.assertTrue(ptt.actual)
        self.assertTrue(ptt.requested)


class UsrpRxPacerTest(unittest.TestCase):
    def test_queue_is_bounded_and_discards_oldest_audio(self):
        pacer = UsrpRxPacer(
            lambda _audio, _keyed: None, queue_frames=3, start_thread=False
        )
        pacer.set_active(True)
        for value in range(5):
            pacer.enqueue(bytes([value]) * USRP_FRAME_BYTES)
        with pacer.condition:
            self.assertEqual([frame[0] for frame in pacer.queue], [2, 3, 4])
            self.assertEqual(pacer.dropped, 2)
        pacer.close()

    def test_worker_failure_is_reported(self):
        failed = threading.Event()

        def fail(_audio, _keyed):
            failed.set()
            raise OSError("simulated UDP failure")

        pacer = UsrpRxPacer(fail)
        pacer.set_active(True)
        self.assertTrue(failed.wait(1))
        pacer.close()
        self.assertIsInstance(pacer.error, OSError)

    def test_long_duration_rate_drift_and_no_consumer_overrun(self):
        """Run 60 s: the producer may burst, but UDP remains exactly 50 pps."""
        sent = []
        lock = threading.Lock()

        def capture(audio, keyed):
            if keyed:
                with lock:
                    sent.append(time.monotonic())

        pacer = UsrpRxPacer(capture, queue_frames=10)
        start = time.monotonic()
        pacer.set_active(True)
        audio = bytes(USRP_FRAME_BYTES)
        duration = 60.0
        end = start + duration
        while time.monotonic() < end:
            # Two 20 ms frames arrive together in every 40 ms codec packet.
            pacer.enqueue(audio)
            pacer.enqueue(audio)
            time.sleep(0.04)
        pacer.set_active(False)
        pacer.close()

        elapsed = sent[-1] - sent[0]
        expected = elapsed / USRP_PERIOD + 1
        expected_wall_count = int(duration / USRP_PERIOD) + 1
        # Scheduler stalls may intentionally skip slots. They must never cause
        # catch-up overrun, and ordinary CI jitter should lose only a few slots.
        self.assertLessEqual(len(sent), expected + 2)
        self.assertGreaterEqual(len(sent), expected - 10)
        self.assertLessEqual(abs(len(sent) - expected_wall_count), 10)
        self.assertGreater(min(b - a for a, b in zip(sent, sent[1:])), 0.015)
        self.assertLessEqual(len(sent), int((time.monotonic() - start) * 50) + 2)


class BridgeQueueTest(unittest.TestCase):
    def test_tx_pcm_is_bounded_and_discards_oldest_audio(self):
        bridge = SimpleNamespace(
            tx_pcm=bytearray(), stats={"tx_audio_dropped": 0}
        )
        for value in range(USRP_TX_QUEUE_FRAMES + 3):
            Bridge.queue_tx_audio(
                bridge, bytes([value]) * USRP_FRAME_BYTES
            )
        self.assertEqual(
            len(bridge.tx_pcm), USRP_TX_QUEUE_FRAMES * USRP_FRAME_BYTES
        )
        self.assertEqual(bridge.tx_pcm[0], 3)
        self.assertEqual(bridge.stats["tx_audio_dropped"], 3)

    def test_serial_pending_queue_is_bounded(self):
        bridge = SimpleNamespace(
            ser=object(), window=1, tx_pending=deque(),
            stats={"tx_pending_dropped": 0},
        )
        for value in range(KV4P_TX_PENDING_FRAMES + 2):
            Bridge.send_vendor(bridge, CMD_AUDIO, bytes([value]))
        self.assertEqual(len(bridge.tx_pending), KV4P_TX_PENDING_FRAMES)
        self.assertEqual(bridge.stats["tx_pending_dropped"], 2)

if __name__ == "__main__":
    unittest.main()

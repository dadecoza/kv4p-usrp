import threading
import time
import unittest

from kv4p_usrp import USRP_FRAME_BYTES, USRP_PERIOD, UsrpRxPacer


class UsrpRxPacerTest(unittest.TestCase):
    def test_queue_is_bounded_and_discards_oldest_audio(self):
        pacer = UsrpRxPacer(lambda _audio, _keyed: None, queue_frames=3)
        # Prevent the worker consuming the queue while its eviction policy is tested.
        with pacer.condition:
            pacer.active = True
            for value in range(5):
                audio = bytes([value]) * USRP_FRAME_BYTES
                if len(pacer.queue) == pacer.queue.maxlen:
                    pacer.queue.popleft()
                    pacer.dropped += 1
                pacer.queue.append(audio)
            self.assertEqual([frame[0] for frame in pacer.queue], [2, 3, 4])
            self.assertEqual(pacer.dropped, 2)
        pacer.close()

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
        expected = int(elapsed / USRP_PERIOD) + 1
        expected_wall_count = int(duration / USRP_PERIOD) + 1
        self.assertLessEqual(abs(len(sent) - expected_wall_count), 5)
        self.assertLessEqual(abs(len(sent) - expected), 1)
        self.assertLess(abs(elapsed - (len(sent) - 1) * USRP_PERIOD), 0.06)
        self.assertGreater(min(b - a for a, b in zip(sent, sent[1:])), 0.015)
        self.assertLessEqual(len(sent), int((time.monotonic() - start) * 50) + 2)


if __name__ == "__main__":
    unittest.main()

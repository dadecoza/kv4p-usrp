#!/usr/bin/env python3
"""KV4P-HT to AllStar USRP bridge.

A bidirectional bridge for KV4P audio, control, and AllStar's USRP channel.
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import signal
import socket
import struct
import sys
import time
import wave
from dataclasses import dataclass

import numpy as np
import serial
from scipy.signal import firwin, lfilter

AUDIO_RATE = 48_000
OPUS_SAMPLES = 1_920  # 40 ms at 48 kHz
USRP_SAMPLES = 160  # 20 ms at 8 kHz
PROTO_MTU = 2_048

FEND, FESC, TFEND, TFESC = 0xC0, 0xDB, 0xDC, 0xDD
KISS_SETHARDWARE = 0x06
VENDOR = b"KV4P\x01"

CMD_HELLO = 0x06
CMD_AUDIO = 0x07  # RX_AUDIO device->host; HOST_TX_AUDIO host->device
CMD_WINDOW_UPDATE = 0x09
CMD_DEVICE_STATE = 0x0B
CMD_HOST_DESIRED_STATE = 0x0D

HOST_RADIO_CONFIG_VALID = 1 << 0
HOST_PTT_REQUESTED = 1 << 1
HOST_RX_AUDIO_OPEN = 1 << 2
HOST_RSSI_ENABLED = 1 << 4
HOST_TX_ALLOWED = 1 << 11
HOST_ENABLE_STATUS_REPORTS = 1 << 12
DEVICE_PHYS_PTT_DOWN = 1 << 8
DEVICE_TX_ACTIVE = 1 << 9
DEVICE_SQUELCHED = 1 << 10
HOST_HIGH_POWER = 1 << 3

VERSION = struct.Struct("<HcIBffB")
STATE = struct.Struct("<IiHBffBBBcBBB")
DESIRED = struct.Struct("<IiHBffBBB")
USRP_HEADER = struct.Struct("!4sIIIIIII")


@dataclass
class DeviceState:
    sequence: int
    memory_id: int
    flags: int
    bandwidth: int
    tx_frequency: float
    rx_frequency: float
    tx_ctcss: int
    squelch: int
    rx_ctcss: int
    radio_status: bytes
    mode: int
    last_error: int
    rssi: int

    @classmethod
    def decode(cls, payload: bytes) -> "DeviceState":
        if len(payload) < STATE.size:
            raise ValueError(f"short DeviceState: {len(payload)} < {STATE.size}")
        return cls(*STATE.unpack_from(payload))


class OpusCodec:
    OPUS_APPLICATION_AUDIO = 2049

    def __init__(self) -> None:
        name = ctypes.util.find_library("opus")
        if not name:
            raise RuntimeError("libopus not found (install libopus0)")
        self.lib = ctypes.CDLL(name)
        self._configure_api()
        error = ctypes.c_int()
        self.decoder = self.lib.opus_decoder_create(AUDIO_RATE, 1, ctypes.byref(error))
        if not self.decoder or error.value:
            raise RuntimeError(f"opus_decoder_create failed: {error.value}")
        self.encoder = self.lib.opus_encoder_create(
            AUDIO_RATE, 1, self.OPUS_APPLICATION_AUDIO, ctypes.byref(error)
        )
        if not self.encoder or error.value:
            self.lib.opus_decoder_destroy(self.decoder)
            raise RuntimeError(f"opus_encoder_create failed: {error.value}")

    def _configure_api(self) -> None:
        self.lib.opus_decoder_create.argtypes = [
            ctypes.c_int32, ctypes.c_int, ctypes.POINTER(ctypes.c_int)
        ]
        self.lib.opus_decoder_create.restype = ctypes.c_void_p
        self.lib.opus_decode.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_ubyte), ctypes.c_int32,
            ctypes.POINTER(ctypes.c_int16), ctypes.c_int, ctypes.c_int,
        ]
        self.lib.opus_decode.restype = ctypes.c_int
        self.lib.opus_decoder_destroy.argtypes = [ctypes.c_void_p]
        self.lib.opus_encoder_create.argtypes = [
            ctypes.c_int32, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_int)
        ]
        self.lib.opus_encoder_create.restype = ctypes.c_void_p
        self.lib.opus_encode.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_int16), ctypes.c_int,
            ctypes.POINTER(ctypes.c_ubyte), ctypes.c_int32,
        ]
        self.lib.opus_encode.restype = ctypes.c_int32
        self.lib.opus_encoder_destroy.argtypes = [ctypes.c_void_p]

    def decode(self, packet: bytes) -> bytes:
        encoded = (ctypes.c_ubyte * len(packet)).from_buffer_copy(packet)
        pcm = (ctypes.c_int16 * 5_760)()
        samples = self.lib.opus_decode(self.decoder, encoded, len(packet), pcm, 5_760, 0)
        if samples < 0:
            raise RuntimeError(f"opus_decode failed: {samples}")
        return ctypes.string_at(pcm, samples * 2)

    def encode(self, samples: np.ndarray) -> bytes:
        samples = np.asarray(samples, dtype=np.int16)
        if len(samples) != OPUS_SAMPLES:
            raise ValueError(f"expected {OPUS_SAMPLES} samples, got {len(samples)}")
        pcm = (ctypes.c_int16 * len(samples)).from_buffer_copy(samples)
        output = (ctypes.c_ubyte * PROTO_MTU)()
        length = self.lib.opus_encode(self.encoder, pcm, len(samples), output, len(output))
        if length < 0:
            raise RuntimeError(f"opus_encode failed: {length}")
        return bytes(output[:length])

    def close(self) -> None:
        if self.encoder:
            self.lib.opus_encoder_destroy(self.encoder)
            self.encoder = None
        if self.decoder:
            self.lib.opus_decoder_destroy(self.decoder)
            self.decoder = None


class KissParser:
    def __init__(self) -> None:
        self.frame = bytearray()
        self.in_frame = False
        self.escaped = False

    def feed(self, data: bytes):
        for byte in data:
            if byte == FEND:
                if self.in_frame and self.frame:
                    yield bytes(self.frame)
                self.frame.clear()
                self.in_frame = True
                self.escaped = False
            elif not self.in_frame:
                continue  # ESP32 boot banner
            elif self.escaped:
                if byte == TFEND:
                    self.frame.append(FEND)
                elif byte == TFESC:
                    self.frame.append(FESC)
                else:
                    self.frame.clear()
                    self.in_frame = False
                self.escaped = False
            elif byte == FESC:
                self.escaped = True
            elif len(self.frame) < PROTO_MTU + 7:
                self.frame.append(byte)
            else:
                self.frame.clear()
                self.in_frame = False


def kiss_escape(data: bytes) -> bytes:
    return data.replace(bytes([FESC]), bytes([FESC, TFESC])).replace(
        bytes([FEND]), bytes([FESC, TFEND])
    )


class Bridge:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.running = True
        self.ser: serial.Serial | None = None
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp.bind((args.usrp_bind, args.usrp_local_port))
        self.udp.setblocking(False)
        self.usrp_destination = (args.allstar_host, args.allstar_port)
        self.codec = OpusCodec()
        self.parser = KissParser()
        self.state: DeviceState | None = None
        self.sequence = 0
        self.device_tx_active = False
        self.ptt_requested = False
        self.ptt_release_deadline: float | None = None
        self.allstar_keyed = False
        self.last_usrp_packet: float | None = None
        self.cos_active = False
        self.usrp_sequence = 0
        self.rx_pcm = bytearray()
        self.tx_pcm = bytearray()
        self.window = 0
        self.tx_pending: list[bytes] = []
        self.rx_filter = firwin(127, 3_400, fs=AUDIO_RATE)
        self.rx_filter_state = np.zeros(len(self.rx_filter) - 1)
        self.tx_filter = firwin(127, 3_400, fs=AUDIO_RATE)
        self.tx_filter_state = np.zeros(len(self.tx_filter) - 1)
        self.wav: wave.Wave_write | None = None
        self.stats = dict(rx_opus=0, tx_opus=0, usrp_rx=0, usrp_tx=0, decode_errors=0)

    def open(self) -> None:
        if self.args.record:
            self.wav = wave.open(self.args.record, "wb")
            self.wav.setparams((1, 2, AUDIO_RATE, 0, "NONE", "not compressed"))
        self.ser = serial.Serial(
            self.args.device, self.args.baud, timeout=0.02, exclusive=True
        )
        self.ser.reset_input_buffer()
        if not self.args.no_reset:
            self.ser.dtr = False
            self.ser.rts = True
            time.sleep(0.15)
            self.ser.rts = False
        print(f"KV4P {self.args.device} @ {self.args.baud}")
        print(
            f"USRP {self.args.usrp_bind}:{self.args.usrp_local_port} <-> "
            f"{self.args.allstar_host}:{self.args.allstar_port}"
        )

    def send_vendor(self, command: int, payload: bytes = b"") -> None:
        if not self.ser:
            return
        body = bytes([KISS_SETHARDWARE]) + VENDOR + bytes([command]) + payload
        frame = bytes([FEND]) + kiss_escape(body) + bytes([FEND])
        if command == CMD_AUDIO and self.window and len(frame) > self.window:
            self.tx_pending.append(frame)
            return
        self.ser.write(frame)
        if self.window:
            self.window = max(0, self.window - len(frame))

    def flush_pending(self) -> None:
        if not self.ser:
            return
        while self.tx_pending and (not self.window or len(self.tx_pending[0]) <= self.window):
            frame = self.tx_pending.pop(0)
            self.ser.write(frame)
            if self.window:
                self.window -= len(frame)

    def desired_flags(self, ptt: bool) -> int:
        flags = HOST_RADIO_CONFIG_VALID | HOST_RX_AUDIO_OPEN | HOST_ENABLE_STATUS_REPORTS

        if self.args.power == "high":
            flags |= HOST_HIGH_POWER

        if not self.args.receive_only:
            flags |= HOST_TX_ALLOWED

        if ptt:
            flags |= HOST_PTT_REQUESTED

        return flags

    def request_state(self, ptt: bool) -> None:
        if not self.state or (ptt and self.args.receive_only):
            return
        self.sequence += 1
        payload = DESIRED.pack(
            self.sequence, self.state.memory_id, self.desired_flags(ptt),
            self.state.bandwidth, self.args.tx_frequency,
            self.args.rx_frequency,
            self.state.tx_ctcss, self.args.squelch, self.state.rx_ctcss,
        )
        self.send_vendor(CMD_HOST_DESIRED_STATE, payload)
        self.ptt_requested = ptt
        print(f"PTT {'ON' if ptt else 'OFF'} requested (sequence {self.sequence})")

    def handle_frame(self, frame: bytes) -> None:
        if len(frame) < 7 or frame[0] != KISS_SETHARDWARE or frame[1:6] != VENDOR:
            return
        command, payload = frame[6], frame[7:]
        if command == CMD_HELLO:
            self.handle_hello(payload)
        elif command == CMD_DEVICE_STATE:
            self.handle_state(DeviceState.decode(payload))
        elif command == CMD_AUDIO:
            self.handle_rx_audio(payload)
        elif command == CMD_WINDOW_UPDATE and len(payload) >= 4:
            self.window += struct.unpack_from("<I", payload)[0]
            self.flush_pending()

    def handle_hello(self, payload: bytes) -> None:
        if len(payload) < VERSION.size + STATE.size:
            raise RuntimeError("short KV4P HELLO")
        firmware, status, window, module, minimum, maximum, features = VERSION.unpack_from(payload)
        self.window = window
        self.state = DeviceState.decode(payload[VERSION.size:])
        self.sequence = self.state.sequence
        for label, frequency in (
            ("RX", self.args.rx_frequency),
            ("TX", self.args.tx_frequency),
        ):
            if not minimum <= frequency <= maximum:
                raise RuntimeError(
                    f"{label} frequency {frequency:.4f} MHz is outside the "
                    f"radio module range {minimum:.1f}-{maximum:.1f} MHz"
                )
        print(
            f"HELLO firmware={firmware} radio={status.decode(errors='replace')} "
            f"module={'VHF' if module == 0 else 'UHF'} range={minimum:.1f}-{maximum:.1f} "
            f"features=0x{features:02x} window={window}"
        )
        self.request_state(False)

    def handle_state(self, state: DeviceState) -> None:
        old_tx = self.device_tx_active
        old_cos = self.cos_active
        self.state = state
        self.sequence = max(self.sequence, state.sequence)
        self.device_tx_active = bool(state.flags & DEVICE_TX_ACTIVE)
        self.cos_active = not bool(state.flags & DEVICE_SQUELCHED)
        if self.device_tx_active != old_tx:
            print(f"KV4P transmitter {'active' if self.device_tx_active else 'stopped'}")
        if self.cos_active != old_cos and not self.device_tx_active:
            self.set_usrp_cos(self.cos_active)

    def handle_rx_audio(self, packet: bytes) -> None:
        try:
            pcm = self.codec.decode(packet)
        except RuntimeError as error:
            self.stats["decode_errors"] += 1
            print(error, file=sys.stderr)
            return
        self.stats["rx_opus"] += 1
        if self.wav:
            self.wav.writeframesraw(pcm)
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
        filtered, self.rx_filter_state = lfilter(
            self.rx_filter, [1.0], samples, zi=self.rx_filter_state
        )
        samples_8k = np.clip(np.rint(filtered[::6]), -32768, 32767).astype("<i2")
        if self.cos_active and not self.device_tx_active:
            self.rx_pcm.extend(samples_8k.tobytes())
            while len(self.rx_pcm) >= USRP_SAMPLES * 2:
                audio = bytes(self.rx_pcm[: USRP_SAMPLES * 2])
                del self.rx_pcm[: USRP_SAMPLES * 2]
                self.send_usrp(audio, True)

    def send_usrp(self, audio: bytes = b"", keyed: bool = False) -> None:
        header = USRP_HEADER.pack(
            b"USRP", self.usrp_sequence, 0, int(keyed), 0, 0, 0, 0
        )
        self.udp.sendto(header + audio, self.usrp_destination)
        self.usrp_sequence = (self.usrp_sequence + 1) & 0xFFFFFFFF
        self.stats["usrp_tx"] += 1

    def set_usrp_cos(self, active: bool) -> None:
        self.rx_pcm.clear()
        if active:
            print("RF COS open")
        else:
            self.send_usrp(keyed=False)
            print("RF COS closed")

    def receive_usrp(self) -> None:
        while True:
            try:
                packet, address = self.udp.recvfrom(2_048)
            except BlockingIOError:
                return

            if address[0] != self.args.allstar_host or len(packet) < USRP_HEADER.size:
                continue

            eye, _, _, keyup, _, kind, _, _ = USRP_HEADER.unpack_from(packet)

            if eye != b"USRP" or kind != 0:
                continue

            self.stats["usrp_rx"] += 1
            self.last_usrp_packet = time.monotonic()

            audio = packet[USRP_HEADER.size:]

            if keyup:
                # The radio is half duplex. RF has priority once COS opens.
                if self.cos_active and not self.device_tx_active:
                    continue

                if not self.allstar_keyed:
                    self.allstar_keyed = True
                    self.ptt_release_deadline = None
                    self.tx_pcm.clear()
                    print("AllStar requested TX")
                    self.request_state(True)

                # We have received a real TX audio packet, so keep PTT alive.
                self.ptt_release_deadline = None

                if len(audio) == USRP_SAMPLES * 2:
                    self.tx_pcm.extend(audio)

            elif self.allstar_keyed:
                # No TX audio/keying packet. Start the PTT hang timer,
                # but don't immediately release the KV4P.
                self.ptt_release_deadline = (
                    time.monotonic() + self.args.ptt_hang
                )

    def pump_tx(self) -> None:
        if not self.device_tx_active:
            return
        while len(self.tx_pcm) >= USRP_SAMPLES * 4:
            raw = bytes(self.tx_pcm[: USRP_SAMPLES * 4])
            del self.tx_pcm[: USRP_SAMPLES * 4]
            pcm_8k = np.frombuffer(raw, dtype="<i2").astype(np.float64)
            expanded = np.repeat(pcm_8k, 6)
            filtered, self.tx_filter_state = lfilter(
                self.tx_filter, [1.0], expanded, zi=self.tx_filter_state
            )
            pcm_48k = np.clip(np.rint(filtered), -32768, 32767).astype(np.int16)
            self.send_vendor(CMD_AUDIO, self.codec.encode(pcm_48k))
            self.stats["tx_opus"] += 1

    def unkey(self, reason: str) -> None:
        print(reason)
        self.allstar_keyed = False
        self.tx_pcm.clear()
        self.tx_pending.clear()
        self.request_state(False)

    def run(self) -> None:
        self.open()
        while self.running:
            assert self.ser is not None
            for frame in self.parser.feed(self.ser.read(512)):
                self.handle_frame(frame)
            self.receive_usrp()
            self.pump_tx()
            if (
                self.allstar_keyed and self.last_usrp_packet is not None
                and time.monotonic() - self.last_usrp_packet > self.args.tx_timeout
            ):
                self.unkey("USRP timeout; forcing PTT off")

            if (
                self.allstar_keyed
                and self.ptt_release_deadline is not None
                and time.monotonic() >= self.ptt_release_deadline
            ):
                self.unkey("AllStar released TX")

    def stop(self, *_args) -> None:
        self.running = False

    def close(self) -> None:
        try:
            if self.ser and (self.ptt_requested or self.device_tx_active):
                self.request_state(False)
                time.sleep(0.25)
        finally:
            if self.wav:
                self.wav.close()
            if self.ser:
                self.ser.close()
            self.udp.close()
            self.codec.close()
            print("Statistics: " + " ".join(f"{k}={v}" for k, v in self.stats.items()))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", default="/dev/ttyUSB1", help="KV4P serial device")
    parser.add_argument("--baud", type=int, default=115_200)
    parser.add_argument(
        "--allstar-host",
        default="127.0.0.1",
        help="AllStar address (default: 127.0.0.1)",
    )
    parser.add_argument("--allstar-port", type=int, default=32_001)
    parser.add_argument("--usrp-bind", default="0.0.0.0")
    parser.add_argument("--usrp-local-port", type=int, default=34_001)
    parser.add_argument("--squelch", type=int, choices=range(0, 9), default=4)
    parser.add_argument(
        "--rx-frequency", type=float, required=True,
        help="receive frequency in MHz (required)",
    )
    parser.add_argument(
        "--tx-frequency", type=float, required=True,
        help="transmit frequency in MHz (required)",
    )
    parser.add_argument("--tx-timeout", type=float, default=1)
    parser.add_argument("--receive-only", action="store_true", help="never key the transmitter")
    parser.add_argument("--record", metavar="WAV", help="optionally record decoded 48 kHz RX")
    parser.add_argument("--no-reset", action="store_true", help="do not pulse CP2102 RTS")
    parser.add_argument(
        "--power",
        choices=("high", "low"),
        default="high",
        help="KV4P transmit power: high or low (default: high)",
    )
    parser.add_argument(
        "--ptt-hang",
        type=float,
        default=0.40,
        help="delay before releasing KV4P PTT after AllStar key-up (seconds)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    bridge = Bridge(args)
    signal.signal(signal.SIGINT, bridge.stop)
    signal.signal(signal.SIGTERM, bridge.stop)
    try:
        bridge.run()
    except (OSError, RuntimeError, ValueError, serial.SerialException) as error:
        print(f"fatal: {error}", file=sys.stderr)
        return 1
    finally:
        bridge.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

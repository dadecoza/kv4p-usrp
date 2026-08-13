#!/usr/bin/env python3

from multiprocessing import context

import serial
import struct
import time
import ctypes
import ctypes.util
import wave
import socket
import numpy as np
from scipy.signal import firwin, lfilter

DEVICE = "/dev/ttyUSB1"
BAUD = 115200

FEND  = 0xC0
FESC  = 0xDB
TFEND = 0xDC
TFESC = 0xDD

CMD_HELLO              = 0x06
CMD_RX_AUDIO           = 0x07
CMD_WINDOW_UPDATE      = 0x09
CMD_DEVICE_STATE       = 0x0B
CMD_HOST_DESIRED_STATE = 0x0D

HOST_RADIO_CONFIG_VALID   = 1 << 0
HOST_PTT_REQUESTED        = 1 << 1
HOST_RX_AUDIO_OPEN        = 1 << 2
HOST_RSSI_ENABLED         = 1 << 4
HOST_TX_ALLOWED           = 1 << 11
HOST_ENABLE_STATUS_REPORT = 1 << 12

DEVICE_PHYS_PTT_DOWN = 1 << 8
DEVICE_TX_ACTIVE     = 1 << 9
DEVICE_SQUELCHED     = 1 << 10

VENDOR = b"KV4P" + bytes([1])

VERSION_FMT = "<HcIBffB"
STATE_FMT = "<IiHBffBBBcBBB"
DESIRED_FMT = "<IiHBffBBB"

VERSION_SIZE = struct.calcsize(VERSION_FMT)
STATE_SIZE = struct.calcsize(STATE_FMT)

CMD_HOST_TX_AUDIO = 0x07

class UsrpSender:
    def __init__(self, destination_host, destination_port, local_port):
        self.destination = (destination_host, destination_port)
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.bind(("0.0.0.0", local_port))
        self.socket.setblocking(False)

        self.sequence = 0
        self.keyed = False
        self.pcm_buffer = bytearray()

        # Low-pass before reducing 48 kHz to 8 kHz.
        self.fir = firwin(
            127,
            3400,
            fs=48000,
        )
        self.filter_state = np.zeros(len(self.fir) - 1)
        self.decimation_phase = 0

        print(
            f"USRP listening on UDP {local_port}; "
            f"sending to {destination_host}:{destination_port}"
        )

    def _header(self, keyup):
        # USRP header fields are 32-bit network byte order.
        return struct.pack(
            "!4sIIIIIII",
            b"USRP",
            self.sequence,
            0,                  # memory
            1 if keyup else 0,
            0,                  # talkgroup
            0,                  # voice
            0,                  # mpxid
            0,                  # reserved
        )

    def _send_packet(self, pcm=b"", keyup=False):
        packet = self._header(keyup) + pcm
        self.socket.sendto(packet, self.destination)
        self.sequence = (self.sequence + 1) & 0xFFFFFFFF

    def set_cos(self, active):
        if active and not self.keyed:
            self.keyed = True
            self.pcm_buffer.clear()
            print("*** USRP RX KEY")

        elif not active and self.keyed:
            self.keyed = False
            self.pcm_buffer.clear()
            self._send_packet(keyup=False)
            print("*** USRP RX UNKEY")

    def audio_48k(self, pcm):
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float64)

        filtered, self.filter_state = lfilter(
            self.fir,
            [1.0],
            samples,
            zi=self.filter_state,
        )

        # Every KV4P packet contains 1920 samples, exactly divisible by 6.
        samples_8k = filtered[self.decimation_phase::6]
        self.decimation_phase = (
            self.decimation_phase + len(samples)
        ) % 6

        samples_8k = np.clip(
            np.rint(samples_8k),
            -32768,
            32767,
        ).astype("<i2")

        # Continue maintaining the resampler state while squelched, but only
        # send audio to AllStar while COS is active.
        if not self.keyed:
            return

        self.pcm_buffer.extend(samples_8k.tobytes())

        # USRP requires 160 samples/320 bytes every 20 ms.
        while len(self.pcm_buffer) >= 320:
            frame = bytes(self.pcm_buffer[:320])
            del self.pcm_buffer[:320]
            self._send_packet(frame, keyup=True)

    def drain_allstar(self, context):
        try:
            while True:
                packet, address = self.socket.recvfrom(2048)

                if address[0] != "192.168.3.9":
                    print(f"Ignoring USRP packet from {address}")
                    continue

                if len(packet) < 32:
                    continue

                fields = struct.unpack("!4sIIIIIII", packet[:32])

                eye = fields[0]
                keyup = fields[3]
                packet_type = fields[5]
                audio = packet[32:]

                if eye != b"USRP" or packet_type != 0:
                    continue

                context["last_usrp_packet"] = time.monotonic()

                if keyup:
                    if not context["allstar_keyed"]:
                        context["allstar_keyed"] = True
                        context["tx_pcm_8k"].clear()
                        print("*** ALLSTAR REQUESTED TX")
                        request_ptt(context, True)

                    if len(audio) == 320:
                        context["tx_pcm_8k"].extend(audio)

                elif context["allstar_keyed"]:
                    print("*** ALLSTAR RELEASED TX")
                    context["allstar_keyed"] = False
                    context["tx_pcm_8k"].clear()
                    request_ptt(context, False)

        except BlockingIOError:
            pass

    def close(self):
        if self.keyed:
            self.set_cos(False)
        self.socket.close()

class OpusDecoder:
    def __init__(self, sample_rate=48000, channels=1):
        library = ctypes.util.find_library("opus")
        if not library:
            raise RuntimeError("libopus was not found; install libopus0")

        self.lib = ctypes.CDLL(library)
        self.channels = channels

        self.lib.opus_decoder_create.argtypes = [
            ctypes.c_int32,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_int),
        ]
        self.lib.opus_decoder_create.restype = ctypes.c_void_p

        self.lib.opus_decode.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_ubyte),
            ctypes.c_int32,
            ctypes.POINTER(ctypes.c_int16),
            ctypes.c_int,
            ctypes.c_int,
        ]
        self.lib.opus_decode.restype = ctypes.c_int

        self.lib.opus_decoder_destroy.argtypes = [ctypes.c_void_p]
        self.lib.opus_decoder_destroy.restype = None

        error = ctypes.c_int()
        self.decoder = self.lib.opus_decoder_create(
            sample_rate,
            channels,
            ctypes.byref(error),
        )

        if not self.decoder or error.value != 0:
            raise RuntimeError(
                f"opus_decoder_create failed: error {error.value}"
            )

    def decode(self, packet):
        # Allow up to 120 ms, although KV4P currently sends 40 ms frames.
        max_samples = 5760
        pcm = (ctypes.c_int16 * (max_samples * self.channels))()

        encoded = (ctypes.c_ubyte * len(packet)).from_buffer_copy(packet)

        samples = self.lib.opus_decode(
            self.decoder,
            encoded,
            len(packet),
            pcm,
            max_samples,
            0,
        )

        if samples < 0:
            raise RuntimeError(f"opus_decode failed: error {samples}")

        byte_count = samples * self.channels * ctypes.sizeof(ctypes.c_int16)
        return ctypes.string_at(pcm, byte_count), samples

    def close(self):
        if self.decoder:
            self.lib.opus_decoder_destroy(self.decoder)
            self.decoder = None

class OpusEncoder:
    OPUS_APPLICATION_AUDIO = 2049

    def __init__(self, sample_rate=48000, channels=1):
        library = ctypes.util.find_library("opus")
        if not library:
            raise RuntimeError("libopus was not found")

        self.lib = ctypes.CDLL(library)
        self.channels = channels

        self.lib.opus_encoder_create.argtypes = [
            ctypes.c_int32,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_int),
        ]
        self.lib.opus_encoder_create.restype = ctypes.c_void_p

        self.lib.opus_encode.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int16),
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_ubyte),
            ctypes.c_int32,
        ]
        self.lib.opus_encode.restype = ctypes.c_int32

        self.lib.opus_encoder_destroy.argtypes = [ctypes.c_void_p]

        error = ctypes.c_int()
        self.encoder = self.lib.opus_encoder_create(
            sample_rate,
            channels,
            self.OPUS_APPLICATION_AUDIO,
            ctypes.byref(error),
        )

        if not self.encoder or error.value != 0:
            raise RuntimeError(
                f"opus_encoder_create failed: {error.value}"
            )

    def encode(self, pcm):
        samples = np.asarray(pcm, dtype=np.int16)

        if len(samples) != 1920:
            raise ValueError(
                f"Opus requires 1920 samples; received {len(samples)}"
            )

        pcm_buffer = (
            ctypes.c_int16 * len(samples)
        ).from_buffer_copy(samples)

        encoded = (ctypes.c_ubyte * 2048)()

        length = self.lib.opus_encode(
            self.encoder,
            pcm_buffer,
            1920,
            encoded,
            len(encoded),
        )

        if length < 0:
            raise RuntimeError(f"opus_encode failed: {length}")

        return bytes(encoded[:length])

    def close(self):
        if self.encoder:
            self.lib.opus_encoder_destroy(self.encoder)
            self.encoder = None        

def kiss_escape(data):
    result = bytearray()

    for byte in data:
        if byte == FEND:
            result.extend((FESC, TFEND))
        elif byte == FESC:
            result.extend((FESC, TFESC))
        else:
            result.append(byte)

    return bytes(result)


def send_vendor(ser, command, payload=b""):
    body = bytes([0x06]) + VENDOR + bytes([command]) + payload
    frame = bytes([FEND]) + kiss_escape(body) + bytes([FEND])
    ser.write(frame)
    ser.flush()
    print(f"TX command 0x{command:02x}: {len(frame)} wire bytes")


def decode_state(data):
    if len(data) < STATE_SIZE:
        raise ValueError(
            f"short DeviceState: expected {STATE_SIZE}, received {len(data)}"
        )

    values = struct.unpack_from(STATE_FMT, data)

    return {
        "sequence": values[0],
        "memory_id": values[1],
        "flags": values[2],
        "bandwidth": values[3],
        "tx_frequency": values[4],
        "rx_frequency": values[5],
        "tx_ctcss": values[6],
        "squelch": values[7],
        "rx_ctcss": values[8],
        "radio_status": values[9].decode("ascii", errors="replace"),
        "mode": values[10],
        "last_error": values[11],
        "rssi": values[12],
    }


def show_state(prefix, state):
    mode_name = {
        0: "TX",
        1: "RX",
        2: "STOPPED",
    }.get(state["mode"], f"unknown-{state['mode']}")

    flags = state["flags"]

    print(
        f"{prefix}: seq={state['sequence']} "
        f"mode={mode_name} flags=0x{flags:04x} "
        f"RX={state['rx_frequency']:.4f} "
        f"TX={state['tx_frequency']:.4f} "
        f"squelched={'yes' if flags & DEVICE_SQUELCHED else 'no'} "
        f"TX-active={'yes' if flags & DEVICE_TX_ACTIVE else 'no'} "
        f"physical-PTT={'down' if flags & DEVICE_PHYS_PTT_DOWN else 'up'} "
        f"RSSI={state['rssi']} error={state['last_error']}"
    )


def send_rx_state(ser, state):
    # Preserve persistent host-controlled settings, but never inherit
    # device-only status bits or an old PTT request.
    flags = state["flags"]

    flags &= ~(
        HOST_PTT_REQUESTED
        | DEVICE_PHYS_PTT_DOWN
        | DEVICE_TX_ACTIVE
        | DEVICE_SQUELCHED
    )

    flags |= (
        HOST_RADIO_CONFIG_VALID
        | HOST_RX_AUDIO_OPEN
        | HOST_ENABLE_STATUS_REPORT
    )

    flags &= ~HOST_RSSI_ENABLED

    # Preserve TX_ALLOWED exactly as reported by the device.
    sequence = state["sequence"] + 1

    squelch_level = 4

    desired = struct.pack(
        DESIRED_FMT,
        sequence,
        state["memory_id"],
        flags,
        state["bandwidth"],
        state["tx_frequency"],
        state["rx_frequency"],
        state["tx_ctcss"],
        squelch_level,
        state["rx_ctcss"],
    )

    print(
        f"Requesting RX mode: seq={sequence}, "
        f"flags=0x{flags:04x}, "
        f"RX={state['rx_frequency']:.4f}, "
        f"TX={state['tx_frequency']:.4f}, "
        f"squelch={squelch_level}"
    )

    send_vendor(ser, CMD_HOST_DESIRED_STATE, desired)
    return sequence

def request_ptt(context, requested):
    state = context.get("latest_device_state")
    ser = context.get("serial")

    if state is None or ser is None:
        return

    flags = state["flags"]

    # Remove device-only report bits and disruptive RSSI polling.
    flags &= ~(
        DEVICE_PHYS_PTT_DOWN
        | DEVICE_TX_ACTIVE
        | DEVICE_SQUELCHED
        | HOST_RSSI_ENABLED
        | HOST_PTT_REQUESTED
    )

    flags |= (
        HOST_RADIO_CONFIG_VALID
        | HOST_RX_AUDIO_OPEN
        | HOST_TX_ALLOWED
        | HOST_ENABLE_STATUS_REPORT
    )

    if requested:
        flags |= HOST_PTT_REQUESTED

    context["next_sequence"] += 1
    sequence = context["next_sequence"]

    desired = struct.pack(
        DESIRED_FMT,
        sequence,
        state["memory_id"],
        flags,
        state["bandwidth"],
        state["tx_frequency"],
        state["rx_frequency"],
        state["tx_ctcss"],
        state["squelch"],
        state["rx_ctcss"],
    )

    send_vendor(
        ser,
        CMD_HOST_DESIRED_STATE,
        desired,
    )

    context["requested_ptt"] = requested

    print(
        f"PTT {'ON' if requested else 'OFF'} requested: "
        f"sequence={sequence}"
    )

def pump_tx_audio(context):
    if not context["tx_active"]:
        return

    # Two 20 ms USRP frames make one 40 ms Opus frame:
    # 320 samples at 8 kHz.
    while len(context["tx_pcm_8k"]) >= 640:
        raw = bytes(context["tx_pcm_8k"][:640])
        del context["tx_pcm_8k"][:640]

        pcm_8k = np.frombuffer(
            raw,
            dtype="<i2",
        ).astype(np.float64)

        # Expand 8 kHz to 48 kHz. The continuous low-pass filter
        # removes the images produced by sample repetition.
        expanded = np.repeat(pcm_8k, 6)

        filtered, context["tx_filter_state"] = lfilter(
            context["tx_filter"],
            [1.0],
            expanded,
            zi=context["tx_filter_state"],
        )

        pcm_48k = np.clip(
            np.rint(filtered),
            -32768,
            32767,
        ).astype(np.int16)

        encoded = context["opus_encoder"].encode(pcm_48k)

        send_vendor(
            context["serial"],
            CMD_HOST_TX_AUDIO,
            encoded,
        )

        context["tx_opus_packets"] += 1

        if context["tx_opus_packets"] == 1:
            print(
                f"First TX Opus packet: {len(encoded)} bytes"
            )

def process_vendor(ser, command, payload, context):
    if command == CMD_HELLO:
        if len(payload) < VERSION_SIZE + STATE_SIZE:
            print(f"Short HELLO payload: {len(payload)} bytes")
            return

        version = struct.unpack_from(VERSION_FMT, payload)
        state = decode_state(payload[VERSION_SIZE:])

        print("\nHELLO received")
        print(f"  Firmware:       {version[0]}")
        print(f"  Radio status:   {version[1].decode(errors='replace')}")
        print(f"  Window:         {version[2]} bytes")
        print(f"  RF module:      {'VHF' if version[3] == 0 else 'UHF'}")
        print(f"  Frequency:      {version[4]:.1f}–{version[5]:.1f} MHz")
        print(f"  Features:       0x{version[6]:02x}")
        show_state("  Initial state", state)

        if context["requested_sequence"] is None:
            context["requested_sequence"] = send_rx_state(ser, state)

        context["latest_device_state"] = state
        context["next_sequence"] = state["sequence"]

    elif command == CMD_DEVICE_STATE:
        state = decode_state(payload)
        show_state("DEVICE_STATE", state)

        """context["squelched"] = bool(
            state["flags"] & DEVICE_SQUELCHED
        )"""

        if state["sequence"] == context["requested_sequence"]:
            context["acknowledged"] = True


        new_squelched = bool(state["flags"] & DEVICE_SQUELCHED)

        if context["last_squelched"] is not None:
            if new_squelched != context["last_squelched"]:
                context["squelch_transitions"] += 1
                print(
                    f"*** SQUELCH TRANSITION: "
                    f"{'CLOSED' if new_squelched else 'OPEN'}"
                )


        context["latest_device_state"] = state
        context["next_sequence"] = max(
            context["next_sequence"],
            state["sequence"],
        )

        was_tx_active = context["tx_active"]
        context["tx_active"] = bool(
            state["flags"] & DEVICE_TX_ACTIVE
        )

        if context["tx_active"] and not was_tx_active:
            print("*** KV4P TRANSMITTER ACTIVE")

        if was_tx_active and not context["tx_active"]:
            print("*** KV4P TRANSMITTER STOPPED")



        

        context["last_squelched"] = new_squelched
        context["squelched"] = new_squelched
        context["usrp"].set_cos(not new_squelched)

    elif command == CMD_RX_AUDIO:
        context["audio_packets"] += 1
        context["audio_bytes"] += len(payload)

        try:
            pcm, samples = context["opus_decoder"].decode(payload)
            context["usrp"].audio_48k(pcm)
        except RuntimeError as error:
            context["decode_errors"] += 1
            print(f"Opus decode error: {error}")
            return

        context["decoded_samples"] += samples

        # Continue decoding every packet to preserve Opus decoder state,
        # but only record audio while COS/squelch indicates a signal.
        context["wav"].writeframesraw(pcm)
        context["recorded_samples"] += samples

        if context["audio_packets"] == 1:
            duration_ms = samples * 1000 / 48000
            print(
                f"First Opus packet decoded: {len(payload)} encoded bytes, "
                f"{samples} samples, {duration_ms:.1f} ms"
            )
        elif context["audio_packets"] % 25 == 0:
            print(
                f"Decoded {context['audio_packets']} packets; "
                f"recorded {context['recorded_samples'] / 48000:.1f} seconds "
                f"of open-squelch audio"
            )

    elif command == CMD_WINDOW_UPDATE:
        if len(payload) >= 4:
            amount = struct.unpack_from("<I", payload)[0]
            print(f"WINDOW_UPDATE: +{amount} bytes")


def process_frame(ser, frame, context):
    if not frame:
        return

    kiss_command = frame[0] & 0x0F
    port = frame[0] >> 4

    if port != 0 or kiss_command != 0x06:
        return

    payload = frame[1:]

    if len(payload) < 6 or payload[:5] != VENDOR:
        return

    command = payload[5]
    process_vendor(ser, command, payload[6:], context)


def main():

    opus_encoder = OpusEncoder(48000, 1)

    tx_filter = firwin(
        127,
        3400,
        fs=48000,
    )

    usrp = UsrpSender(
        destination_host="192.168.3.9",
        destination_port=32001,
        local_port=34001,
    )

    output_name = "kv4p-receive.wav"

    opus_decoder = OpusDecoder(48000, 1)

    wav_file = wave.open(output_name, "wb")
    wav_file.setnchannels(1)
    wav_file.setsampwidth(2)
    wav_file.setframerate(48000)

    context = {
        "requested_sequence": None,
        "acknowledged": False,
        "audio_packets": 0,
        "audio_bytes": 0,
        "decoded_samples": 0,
        "recorded_samples": 0,
        "decode_errors": 0,
        "squelched": True,
        "opus_decoder": opus_decoder,
        "wav": wav_file,
        "last_squelched": None,
        "squelch_transitions": 0,
        "usrp": usrp,
        "serial": None,
        "latest_device_state": None,
        "next_sequence": 0,

        "allstar_keyed": False,
        "requested_ptt": False,
        "tx_active": False,
        "last_usrp_packet": None,

        "tx_pcm_8k": bytearray(),
        "tx_filter": tx_filter,
        "tx_filter_state": np.zeros(len(tx_filter) - 1),
        "opus_encoder": opus_encoder,
        "tx_opus_packets": 0,
    }

    print(f"Opening {DEVICE} at {BAUD} baud")

    with serial.Serial(
        DEVICE,
        BAUD,
        timeout=0.1,
        exclusive=True,
    ) as ser:
        context["serial"] = ser
        ser.reset_input_buffer()

        # GPIO0 inactive; pulse EN using the CP2102 RTS line.
        ser.dtr = False
        ser.rts = True
        time.sleep(0.15)
        ser.rts = False

        print("Reset released; waiting for HELLO...")

        deadline = time.monotonic() + 60
        frame = bytearray()
        in_frame = False
        escaped = False
        try:
            while time.monotonic() < deadline:
                data = ser.read(512)

                for byte in data:
                    if byte == FEND:
                        if in_frame and frame:
                            process_frame(ser, bytes(frame), context)

                        frame.clear()
                        in_frame = True
                        escaped = False
                        continue

                    if not in_frame:
                        # Ignore the human-readable ESP32 boot banner.
                        continue

                    if escaped:
                        if byte == TFEND:
                            frame.append(FEND)
                        elif byte == TFESC:
                            frame.append(FESC)
                        else:
                            print(f"Invalid KISS escape: 0x{byte:02x}")
                            frame.clear()
                            in_frame = False

                        escaped = False
                    elif byte == FESC:
                        escaped = True
                    else:
                        frame.append(byte)
                context["usrp"].drain_allstar(context)
                pump_tx_audio(context)
                if (
                    context["allstar_keyed"]
                    and context["last_usrp_packet"] is not None
                    and time.monotonic() - context["last_usrp_packet"] > 0.5
                ):
                    print("*** USRP TX TIMEOUT — UNKEYING")
                    context["allstar_keyed"] = False
                    context["tx_pcm_8k"].clear()
                    request_ptt(context, False)
        finally:
            if context["requested_ptt"] or context["tx_active"]:
                print("Forcing PTT off before exit")
                request_ptt(context, False)
                time.sleep(0.25)

            context["usrp"].close()
            context["opus_encoder"].close()
            context["opus_decoder"].close()

        wav_file.close()
        opus_decoder.close()
        context["usrp"].close()

        print("\nTest complete")
        print(f"State acknowledged: {context['acknowledged']}")
        print(f"Opus packets:       {context['audio_packets']}")
        print(f"Opus bytes:         {context['audio_bytes']}")
        print(f"Decoded samples:    {context['decoded_samples']}")
        print(f"Decode errors:      {context['decode_errors']}")
        print(f"Squelch transitions:{context['squelch_transitions']}")
        print(
            f"Recorded audio:     "
            f"{context['recorded_samples'] / 48000:.2f} seconds"
        )
        
        print(f"Output file:        {output_name}")

        if not context["acknowledged"]:
            raise SystemExit("No matching DEVICE_STATE acknowledgement")

        if context["audio_packets"] == 0:
            raise SystemExit("State worked, but no RX audio packets arrived")

        
        


if __name__ == "__main__":
    main()

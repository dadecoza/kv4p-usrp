# kv4p-usrp

Bridge a [KV4P-HT](https://github.com/VanceVagell/kv4p-ht) radio to an
[AllStarLink](https://www.allstarlink.org/) node using AllStar's USRP UDP
channel driver.

The Python bridge is the production implementation. It supports bidirectional
audio, PTT, COS/squelch, Opus conversion, and the fixed 48 kHz ↔ 8 kHz
resampling required between KV4P and USRP.

## Signal path

```text
RF ↔ SA818 ↔ ESP32/KV4P ↔ USB serial ↔ kv4p_usrp.py
   ↔ USRP/UDP ↔ chan_usrp ↔ AllStarLink
```

## Requirements

- KV4P-HT ESP32 WROOM-32 running firmware v2.0.0.1 (protocol 2.2)
- Python 3.11 or newer
- `libopus`
- Python packages in `requirements.txt`
- AllStarLink with `chan_usrp.so`

On Debian/Ubuntu:

```bash
sudo apt install libopus0 python3-venv
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

## AllStar configuration

If the bridge host is `192.168.3.67`, configure the node in
`/etc/asterisk/rpt.conf`:

```ini
rxchannel = USRP/192.168.3.67:34001:32001
```

Ensure `chan_usrp.so` is loaded. The AllStar host must accept UDP 32001 from
the bridge host; the bridge host must accept UDP 34001 from AllStar.

## Run

The operator must explicitly provide both RX and TX frequencies in MHz. The
bridge never trusts frequencies persisted by a previous KV4P session. Confirm
that both frequencies are authorized for your station and appropriate for the
intended simplex or repeater channel.

When AllStar runs on the same host, `--allstar-host` may be omitted and
defaults to `127.0.0.1`. Start safely in receive-only mode first:

```bash
./kv4p_usrp.py \
  --device /dev/ttyUSB1 \
  --allstar-host 192.168.3.9 \
  --rx-frequency <RX_MHZ> \
  --tx-frequency <TX_MHZ> \
  --squelch 4 \
  --receive-only
```

After verifying RF → AllStar audio, omit `--receive-only` to permit AllStar →
RF transmission:

```bash
./kv4p_usrp.py \
  --device /dev/ttyUSB1 \
  --allstar-host 192.168.3.9 \
  --rx-frequency <RX_MHZ> \
  --tx-frequency <TX_MHZ> \
  --squelch 4
```

The bridge rejects frequencies outside the RF module range reported in its
KV4P HELLO message. This hardware-range check does not determine whether a
frequency is legal for the operator, country, licence class, or local band
plan. Use a persistent `/dev/serial/by-id/...` or udev-created device name in
production.

## Safety

- Transmit is disabled when `--receive-only` is used.
- Audio is sent only after KV4P reports `DEVICE_STATE_TX_ACTIVE`.
- A 500 ms USRP watchdog unkeys if packets stop arriving.
- SIGINT/SIGTERM and normal shutdown explicitly request PTT off.
- The ESP32 firmware retains its independent 200-second runaway-TX timeout.
- Only USRP packets from `--allstar-host` are accepted.

Test with a dummy load or appropriate monitoring receiver, verify the configured
frequency and licensing conditions, and keep initial transmissions short.

## Important hardware note: keep RSSI disabled

The bridge intentionally clears `HOST_STATE_RSSI_ENABLED`. KV4P firmware polls
the SA818 with `RSSI?` every 100 ms when RSSI is enabled. On affected v1.x
boards, crosstalk between the serial and audio traces produces a clearly audible
tick during received speech. Hardware squelch state remains available and is
used as USRP COS, so RSSI is unnecessary for normal operation.

## Protocol summary

- Serial: 115200 baud, KISS framing
- Vendor frame: `C0 06 "KV4P" 01 <command> <payload> C0`
- KV4P audio: mono Opus, 48 kHz, 40 ms / 1920-sample frames
- USRP audio: signed 16-bit mono PCM, 8 kHz, 20 ms / 160-sample frames
- KV4P multi-byte fields: little-endian
- USRP header fields: network byte order

The bridge decodes every KV4P Opus packet to keep decoder state continuous,
uses `DEVICE_STATE_SQUELCHED` for COS, and maps acknowledged KV4P PTT state to
USRP keying.

## Project status

The Python bridge has been tested bidirectionally against AllStarLink node
69449 with clean audio and normal AllStar courtesy-tone behavior. Potential
future work includes reconnect hardening, configuration files, systemd
packaging, automated protocol/resampler tests, and—only if resource usage
warrants it—an optional lightweight C implementation.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).

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

## Quick install on an AllStarLink node

On an ASL3/Debian node, connect the KV4P and run:

```bash
curl -fsSL https://raw.githubusercontent.com/dadecoza/kv4p-usrp/main/install.sh | sudo bash
```

The installer detects likely serial devices, asks for the RX and TX
frequencies, installs the Python dependencies in a dedicated virtual
environment, and creates a hardened `kv4p-usrp.service`. It defaults the first
start to receive-only so that RF receive can be verified before transmission is
enabled.

Then ensure the appropriate node section in `/etc/asterisk/rpt.conf` contains:

```ini
rxchannel = USRP/127.0.0.1:34001:32001
```

Follow the service log while testing:

```bash
sudo journalctl -u kv4p-usrp -f
```

To enable transmit or change radio settings, rerun the installer with
`--reconfigure`. A fully non-interactive install is also supported:

```bash
curl -fsSL https://raw.githubusercontent.com/dadecoza/kv4p-usrp/main/install.sh \
  | sudo bash -s -- \
      --device /dev/serial/by-id/<YOUR_KV4P_DEVICE> \
      --rx-frequency <RX_MHZ> \
      --tx-frequency <TX_MHZ> \
      --allow-transmit
```

Running the one-line installer again updates an existing installation while
preserving `/etc/default/kv4p-usrp`. Other useful installer modes are
`--no-start`, `--reconfigure`, and `--uninstall` (which preserves the
configuration file).

On Debian/Ubuntu:

```bash
sudo apt install libopus0 python3-venv
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

Grant the user running the bridge access to USB serial devices:

```bash
sudo usermod -aG dialout "$USER"
```

Log out completely and log back in for the new group membership to take
effect. Then verify both membership and access:

```bash
id
ls -l /dev/serial/by-id/
test -r /dev/ttyUSB1 && test -w /dev/ttyUSB1 && echo "Serial access OK"
```

Use the stable `/dev/serial/by-id/...` path when the adapter has a unique
serial number. If multiple CP2102 adapters expose the same identity, use a
udev rule based on their physical `ID_PATH` or specify the correct
`/dev/ttyUSB*` device explicitly.

## AllStar configuration

For the usual setup where the bridge runs directly on the AllStar Pi,
configure the node in `/etc/asterisk/rpt.conf`:

```ini
rxchannel = USRP/127.0.0.1:34001:32001
```

Ensure `chan_usrp.so` is loaded. If the bridge runs on a separate host,
replace `127.0.0.1` with that host's address, pass the AllStar host address
using `--allstar-host`, and permit UDP 32001/34001 between the two systems.

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
  --rx-frequency <RX_MHZ> \
  --tx-frequency <TX_MHZ> \
  --squelch 4
```

The bridge rejects frequencies outside the RF module range reported in its
KV4P HELLO message. This hardware-range check does not determine whether a
frequency is legal for the operator, country, licence class, or local band
plan. Use a persistent `/dev/serial/by-id/...` or udev-created device name in
production.

## Receive gain and bandwidth

Use `--rx-gain 2.0 --bandwidth wide` to boost RF-to-AllStar audio by
approximately 6 dB and explicitly select wide FM. Gain defaults to `1.0`
(unchanged); `4.0` is approximately +12 dB. Use normal speech with an audio
test node when adjusting. Excess gain raises noise and clips loud speech at
the signed 16-bit limits. AllStar-to-RF audio is unaffected.
The optional `--record` WAV remains the decoded RX audio before bridge gain.

Bandwidth accepts `wide` (25 kHz), `narrow` (12.5 kHz), or `keep` (default:
preserve the device-reported setting). The selected bandwidth applies to both
radio receive and transmit. Match the other radio and channel requirements.

For an installed service, append these options inside the existing
`KV4P_ARGS="..."` line in `/etc/default/kv4p-usrp`, then restart:

```bash
sudo systemctl restart kv4p-usrp
```

The installer also accepts `--rx-gain` and `--bandwidth` when creating or
replacing configuration. Supplying installer configuration options replaces
the configuration, so provide the other station settings too. A plain update
preserves the existing configuration, including any manually added options.

## Safety

- Transmit is disabled when `--receive-only` is used.
- Audio is sent only after KV4P reports `DEVICE_STATE_TX_ACTIVE`.
- A 1-second USRP watchdog unkeys if packets stop arriving by default; configure
  it with `--tx-timeout`.
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

MIT. See [LICENSE](LICENSE).

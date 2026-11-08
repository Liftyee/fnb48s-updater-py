This is the result of a oneshot prompt where I wanted to update my FNB48S USB tester, reverse-engineered from the Windows updater by GPT6.1 Sol. It is tested working on my FNB48S unit on Ubuntu 24.04. Hopefully someone, or some AI, might find this useful in the future for flashing or developing custom firmware.

# FNB48S updater for Linux

A small Python script that flashes official FNIRSI FNB48S `.ufn` firmware over USB HID. No Wine, Windows, pip packages, or build step required.

Tested with **one FNB48S and an Ubuntu 24.04 host**, reinstalling **V1.10**. The original hardware run transferred 198,608 bytes in 3,425 blocks, received the bootloader's completion response, and verified that the tester restarted in normal USB mode and reported V1.10. The cleaned-up version reproduces all 3,427 request/response exchanges from that transfer in an offline replay and has tests for simulated failures; it has not been flashed again on hardware.

## Usage

Requires Python 3.8+ and HIDAPI's Linux hidraw backend:

```sh
sudo apt install python3 libhidapi-hidraw0
```

1. Download the **FNB48S** firmware from [FNIRSI's downloads page](https://www.fnirsi.com/pages/manuals-firmwares). Keep its original filename, such as `Fnb48sV1.10.ufn`. Firmware is not bundled here.
2. Disconnect the tester. Hold its middle button while connecting its PC USB port to enter DFU mode.
3. Connect only one FNIRSI tester in DFU mode, then run:

```sh
python3 fnb48s_update.py /path/to/Fnb48sV1.10.ufn
```

If access is denied, run the same command with `sudo`:

```sh
sudo python3 fnb48s_update.py /path/to/Fnb48sV1.10.ufn
```

Keep power and USB connected throughout the update. Success requires the tester's `0x31` completion response; the tester should then restart automatically. Installing the same version again is supported by the tested bootloader. If a transfer fails after erasing, keep or re-enter DFU mode and rerun the command to start over.

**Flashing can leave a device unusable if interrupted or given incorrect firmware.** Use trusted, official FNB48S files. The script checks the device model, filename, size bounds, and USB response checksums; it cannot authenticate or decrypt the firmware, and a filename does not prove compatibility. Other FNIRSI models and custom firmware are untested.

The tested V1.10 file has SHA-256:

```text
b03b47bfbe956a81437260e7d3cdfe412d3fb1a210334c1467d2e76646822dcf
```

## Protocol notes

DFU mode enumerates as `0483:0038`. The script queries identification (`0x26` → `0x27`) and requires FNB48S model code `0x31` and DFU mode `1` before erasing.

Each packet is 64 bytes: command, payload length, sequence, two-byte big-endian block index, up to 58 payload bytes, zero padding, and CRC at byte 63. CRC uses polynomial `0x39`, initial value zero, MSB first, over bytes 0–62. HIDAPI adds a separate zero report ID before the packet.

- Start: `0x28`, sequence zero, six-byte payload containing little-endian `uint16` version and `uint32` file length. Wait for `0x29`.
- Data: `0x2b`, block index starting at one, sequence `index % 50`. Wait for `0x2c`; the last block must lead to `0x31` completion. The final short block uses its actual payload length, with CRC still at byte 63.
- Version: inferred from the original firmware filename; `V1.10` becomes `110`, `V1.0` becomes `100`.

Data response timeouts trigger up to three retries of the identical packet. CRC, status, sequence, and block-index errors stop the transfer. Erase commands are not automatically retried. There is no saved-state resume, firmware download, device selection, or firmware modification.

The archived older Windows updater used a 58-byte start payload, which this FNB48S bootloader rejected. The working six-byte start payload was established by comparing existing Linux implementations and testing the device.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

Tests require no USB hardware. `tests/packets.json` contains four request/response pairs from the successful hardware transfer: identification, start, first data block, and final data block.

## Sources and licence

The Windows .NET updater was inspected using Mono's disassembler, and its packet builder was invoked under Mono to compare packet layouts. Existing native implementations helped resolve differences in the newer bootloader protocol:

- [kr4fty/fnb48s-archive](https://github.com/kr4fty/fnb48s-archive): Windows updater and archived firmware used in the experiment.
- [dossalab/fnirsi-fnb58-firmware-update-tool](https://github.com/dossalab/fnirsi-fnb58-firmware-update-tool): native transfer sequence and six-byte start payload; published under WTFPL v2.
- [Randomblock1/fnirsi-usb-tool](https://github.com/Randomblock1/fnirsi-usb-tool): corroborating packet format, HID report handling, and USB identifiers.

This Python implementation is AI-generated and provided under the [MIT licence](LICENSE). The Windows executable and FNIRSI firmware are not distributed or covered by this licence. This is an unofficial project with no FNIRSI affiliation.

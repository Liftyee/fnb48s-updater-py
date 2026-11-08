#!/usr/bin/env python3
"""Minimal Linux firmware updater for the FNIRSI FNB48S."""

import argparse
import ctypes
import re
import struct
import sys
import time
from pathlib import Path


class UpdateError(Exception):
    """A device, firmware, or protocol error."""


class ResponseTimeout(UpdateError):
    """No HID response arrived within the requested time."""


def crc8(data):
    """FNIRSI CRC: polynomial 0x39, initial value zero, MSB first."""
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ (0x39 if crc & 0x80 else 0)) & 0xFF
    return crc


def packet(command, sequence, index=0, payload=b""):
    if len(payload) > 58 or not 0 <= index <= 0xFFFF:
        raise ValueError("Packet payload or block index is too large")
    body = (bytes((command, len(payload), sequence))
            + index.to_bytes(2, "big") + payload).ljust(63, b"\0")
    return body + bytes((crc8(body),))


def validate_response(response):
    if len(response) != 64:
        raise UpdateError(f"Expected a 64-byte response, received {len(response)} bytes")
    if crc8(response[:63]) != response[63]:
        raise UpdateError("Device response has an invalid CRC")
    return response


def load_firmware(path):
    # The version is sent to the bootloader; the encrypted file has no header
    # that this tool can inspect. Restrict names to the FNB48S naming scheme.
    match = re.fullmatch(r"Fnb48s_?V(\d+)\.(\d{1,2})\.ufn", path.name, re.IGNORECASE)
    if not match:
        raise UpdateError("Expected an FNB48S filename such as Fnb48sV1.10.ufn")
    version = int(match[1]) * 100 + int(match[2].ljust(2, "0"))
    if not 0 < version <= 0xFFFF:
        raise UpdateError("Firmware version is outside the protocol's range")
    data = path.read_bytes()
    if not data or len(data) > 58 * 0xFFFF:
        raise UpdateError("Firmware is empty or exceeds the protocol's block limit")
    return version, data


class HidDevice:
    """Small ctypes binding to Ubuntu's hidraw-backed HIDAPI library."""

    def __init__(self):
        try:
            self.lib = ctypes.CDLL("libhidapi-hidraw.so.0")
        except OSError as exc:
            raise UpdateError("Install HIDAPI: sudo apt install libhidapi-hidraw0") from exc
        lib = self.lib
        lib.hid_open.argtypes = [ctypes.c_ushort, ctypes.c_ushort, ctypes.c_wchar_p]
        lib.hid_open.restype = ctypes.c_void_p
        lib.hid_write.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
        lib.hid_write.restype = ctypes.c_int
        lib.hid_read_timeout.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                         ctypes.c_size_t, ctypes.c_int]
        lib.hid_read_timeout.restype = ctypes.c_int
        lib.hid_error.argtypes = [ctypes.c_void_p]
        lib.hid_error.restype = ctypes.c_wchar_p
        lib.hid_close.argtypes = [ctypes.c_void_p]
        lib.hid_close.restype = None
        self.handle = lib.hid_open(0x0483, 0x0038, None)
        if not self.handle:
            raise UpdateError("Cannot open the tester. Connect it in DFU mode and check "
                              "USB permissions (see README). "
                              + (lib.hid_error(None) or ""))

    def close(self):
        if self.handle:
            self.lib.hid_close(self.handle)
            self.handle = None

    def write(self, data):
        # HIDAPI expects an initial zero report ID, outside the 64-byte packet.
        wire = b"\0" + data
        if self.lib.hid_write(self.handle, wire, len(wire)) != len(wire):
            raise UpdateError("USB write failed: " + (self.lib.hid_error(self.handle) or ""))

    def read(self, timeout_ms):
        buffer = ctypes.create_string_buffer(64)
        size = self.lib.hid_read_timeout(self.handle, buffer, 64, timeout_ms)
        if size == 0:
            raise ResponseTimeout("Timed out waiting for the tester")
        if size < 0:
            raise UpdateError("USB read failed: " + (self.lib.hid_error(self.handle) or ""))
        return buffer.raw[:size]


def exchange(device, request, expected, timeout_ms=5000, retry=False):
    # Only data packets are retried. Never silently repeat an erase command.
    for attempt in range(4 if retry else 1):
        time.sleep(0.005)
        device.write(request)
        try:
            response = validate_response(device.read(timeout_ms))
        except ResponseTimeout:
            if not retry or attempt == 3:
                raise
            print("Missing acknowledgement; retrying the same block.", flush=True)
            continue
        if response[2] != request[2]:
            raise UpdateError("Device response sequence does not match the request")
        if response[0] not in expected:
            raise UpdateError(f"Unexpected device response 0x{response[0]:02x}: {response.hex()}")
        if request[0] == 0x2B and response[3:5] != request[3:5]:
            raise UpdateError("Device acknowledged a different firmware block")
        return response


def flash(device, version, firmware, progress=print):
    info = exchange(device, packet(0x26, 0x45), (0x27,))
    if info[5] != 0x31:
        raise UpdateError(f"Expected an FNB48S (model 0x31), got model 0x{info[5]:02x}")
    if info[8] != 1:
        raise UpdateError("Tester is not in DFU mode")
    current = int.from_bytes(info[6:8], "little")
    progress(f"FNB48S V{current // 100}.{current % 100:02d} -> "
             f"V{version // 100}.{version % 100:02d}; {len(firmware):,} bytes")
    progress("Erasing flash. Keep the tester connected until completion.")
    exchange(device, packet(0x28, 0, payload=struct.pack("<HI", version, len(firmware))),
             (0x29,), timeout_ms=15000)
    total = (len(firmware) + 57) // 58
    for index in range(1, total + 1):
        chunk = firmware[(index - 1) * 58:index * 58]
        expected = (0x2C, 0x31) if index == total else (0x2C,)
        response = exchange(device, packet(0x2B, index % 50, index, chunk),
                            expected, retry=True)
        if index % 250 == 0 or index == total:
            progress(f"{index}/{total} blocks acknowledged")
    if response[0] != 0x31:
        response = validate_response(device.read(10000))
        if response[0] != 0x31 or response[2:5] != packet(0x2B, total % 50, total)[2:5]:
            raise UpdateError("Tester did not confirm update completion")
    progress("Update complete: tester confirmed success (0x31) and should restart.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("firmware", type=Path, help="official Fnb48sVx.xx.ufn file")
    args = parser.parse_args(argv)
    device = None
    started = False
    try:
        version, firmware = load_firmware(args.firmware)
        device = HidDevice()
        started = True
        flash(device, version, firmware, progress=lambda message: print(message, flush=True))
    except (UpdateError, OSError, KeyboardInterrupt) as exc:
        print(f"Error: {exc or 'interrupted'}", file=sys.stderr)
        if started:
            print("If flashing began, keep the tester in DFU mode and rerun the command. "
                  "This tool restarts the transfer; it does not resume.", file=sys.stderr)
        return 1
    finally:
        if device is not None:
            device.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

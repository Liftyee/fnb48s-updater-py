import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import fnb48s_update as updater


class FakeDevice:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.writes = []

    def write(self, request):
        self.writes.append(request)

    def read(self, timeout_ms):
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.sleep = patch.object(updater.time, "sleep")
        self.sleep.start()
        self.addCleanup(self.sleep.stop)
        self.captures = json.loads(Path(__file__).with_name("packets.json").read_text())

    def test_matches_packets_from_successful_hardware_update(self):
        for capture in self.captures:
            request = bytes.fromhex(capture["tx"])
            self.assertEqual(updater.packet(request[0], request[2],
                             int.from_bytes(request[3:5], "big"), request[5:5+request[1]]), request)
            updater.validate_response(bytes.fromhex(capture["rx"]))
        last = bytes.fromhex(self.captures[-1]["tx"])
        self.assertEqual(last[1], 16)  # Actual short final block, CRC still at byte 63.

    def test_two_block_transfer(self):
        # Small two-block transfer exercises sequence, short tail, and final status.
        device = FakeDevice([
            bytes.fromhex(self.captures[0]["rx"]),
            updater.packet(0x29, 0, payload=struct.pack("<HI", 110, 59)),
            updater.packet(0x2C, 1, 1),
            updater.packet(0x31, 2, 2),
        ])
        updater.flash(device, 110, bytes(range(59)), progress=lambda _: None)
        self.assertEqual(len(device.writes), 4)
        self.assertEqual(device.writes[-1][1], 1)

    def test_identical_data_packet_is_retried_after_timeout(self):
        request = updater.packet(0x2B, 0, 50, b"data")
        device = FakeDevice([updater.ResponseTimeout(), updater.packet(0x2C, 0, 50)])
        updater.exchange(device, request, (0x2C,), retry=True)
        self.assertEqual(device.writes, [request, request])

    def test_erase_is_not_retried(self):
        device = FakeDevice([updater.ResponseTimeout()])
        with self.assertRaises(updater.ResponseTimeout):
            updater.exchange(device, updater.packet(0x28, 0), (0x29,))
        self.assertEqual(len(device.writes), 1)

    def test_wrong_model_is_rejected_before_erase(self):
        info = bytearray.fromhex(self.captures[0]["rx"])
        info[5] = 0x30
        info[63] = updater.crc8(info[:63])
        device = FakeDevice([bytes(info)])
        with self.assertRaisesRegex(updater.UpdateError, "Expected an FNB48S"):
            updater.flash(device, 110, b"firmware")
        self.assertEqual(len(device.writes), 1)

    def test_bad_crc_sequence_block_and_error_status_are_rejected(self):
        request = updater.packet(0x2B, 1, 1)
        corrupt = bytearray(updater.packet(0x2C, 1, 1))
        corrupt[8] ^= 1
        for response in (bytes(corrupt), updater.packet(0x2C, 2, 1),
                         updater.packet(0x2C, 1, 51), updater.packet(0x32, 1, 1), b"short"):
            with self.subTest(response=response.hex()):
                with self.assertRaises(updater.UpdateError):
                    updater.exchange(FakeDevice([response]), request, (0x2C,))

    def test_missing_completion_is_not_success(self):
        device = FakeDevice([bytes.fromhex(self.captures[0]["rx"]),
                             updater.packet(0x29, 0), updater.packet(0x2C, 1, 1),
                             updater.ResponseTimeout()])
        with self.assertRaises(updater.ResponseTimeout):
            updater.flash(device, 110, b"x", progress=lambda _: None)

    def test_firmware_filename_version_and_empty_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Fnb48sV1.10.ufn"
            path.write_bytes(b"firmware")
            self.assertEqual(updater.load_firmware(path), (110, b"firmware"))
            renamed = path.with_name("Fnb48s_V1.0.ufn")
            path.rename(renamed)
            self.assertEqual(updater.load_firmware(renamed)[0], 100)
            renamed.write_bytes(b"")
            with self.assertRaises(updater.UpdateError):
                updater.load_firmware(renamed)
            with self.assertRaises(updater.UpdateError):
                updater.load_firmware(path.with_name("Fnb58V1.10.ufn"))


if __name__ == "__main__":
    unittest.main()

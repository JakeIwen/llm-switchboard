"""The Mac's `machine` for the link hello (desk/mac_machine.py): a fixed
derivation of the hardware UUID that passes the hub's validator, and on
macOS the UUID as `ioreg` shows `IOPlatformUUID`."""

import re
import subprocess
import sys
import unittest

from llm_switchboard.desk.link_protocol import valid_machine
from llm_switchboard.desk.mac_machine import hardware_uuid, machine_for, machine_id

UUID_TEXT = re.compile(r"[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}")
IOREG = "/usr/sbin/ioreg"


class MachineForTest(unittest.TestCase):
    def test_fixed_32_hex_and_accepted_by_the_hub(self):
        # A fixed vector: changing the derivation would make the hub see a new machine.
        value = machine_for("00000000-1111-2222-3333-444455556666")
        self.assertEqual(value, "88df02c0f12bebd63d4fac7a62f4b77a")
        self.assertTrue(valid_machine(value))

    def test_uuid_text_taken_exactly(self):
        # Why hardware_uuid() pins ioreg's upper-case form: another spelling is another machine.
        uuid_text = "ABCDEF01-2345-6789-ABCD-EF0123456789"
        self.assertNotEqual(machine_for(uuid_text), machine_for(uuid_text.lower()))


@unittest.skipUnless(sys.platform == "darwin", "gethostuuid and IOPlatformUUID exist only on macOS")
class MachineIdOnMacTest(unittest.TestCase):
    def test_machine_id_stable_and_from_the_hardware_uuid(self):
        uuid_text = hardware_uuid()
        self.assertRegex(uuid_text, UUID_TEXT)
        self.assertEqual(machine_id(), machine_for(uuid_text))
        self.assertEqual(machine_id(), machine_id())
        self.assertTrue(valid_machine(machine_id()))

    def test_hardware_uuid_as_ioreg_shows_it(self):
        result = subprocess.run([IOREG, "-rd1", "-c", "IOPlatformExpertDevice"], capture_output=True, text=True,
                                timeout=30, check=True)
        match = re.search(r'"IOPlatformUUID" = "([^"]+)"', result.stdout)
        self.assertIsNotNone(match, "ioreg shows no IOPlatformUUID")
        self.assertEqual(hardware_uuid(), match.group(1))


if __name__ == "__main__":
    unittest.main()

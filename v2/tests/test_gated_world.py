from __future__ import annotations

import ctypes
import subprocess
import sys
import unittest
from pathlib import Path


EXPERIMENT = Path(__file__).parents[1] / "experiments" / "gated-world"


def supports_structural_world() -> bool:
    if sys.platform != "linux":
        return False
    libc = ctypes.CDLL(None, use_errno=True)
    return libc.syscall(444, 0, 0, 1) >= 6


class GatedWorldExperimentTests(unittest.TestCase):
    @unittest.skipUnless(supports_structural_world(), "structural process World requires Landlock ABI 6")
    def test_three_process_worlds_attenuate_one_private_link(self):
        completed = subprocess.run(
            [sys.executable, str(EXPERIMENT / "run.py")],
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("PASS: command and native-socket affordances were projected independently", completed.stdout)
        self.assertIn("PASS: denied and unknown Slack operations failed closed before upstream", completed.stdout)
        self.assertIn("PASS: Landlock and seccomp blocked filesystem, network, exec, and process escape", completed.stdout)
        self.assertIn("PASS: revoking the socket Link preserved the command projection", completed.stdout)
        self.assertIn("PASS: revoking the command Link preserved the socket projection", completed.stdout)


if __name__ == "__main__":
    unittest.main()

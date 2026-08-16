from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
EXPERIMENT = ROOT / "experiments" / "world-backends"


class BackendWorldProofTests(unittest.TestCase):
    def test_running_proof(self):
        completed = subprocess.run(
            [sys.executable, str(EXPERIMENT / "proof.py")],
            text=True,
            capture_output=True,
            timeout=30,
            check=True,
        )
        self.assertEqual(completed.stderr, "")
        self.assertEqual(completed.stdout.splitlines(), [
            "direct: same logical graph + same result + clean lifecycle",
            "packaged-relayed: same logical graph + same result + clean lifecycle",
            "proof: backend interface = launch() -> stop(); interaction = Links",
        ])

if __name__ == "__main__":
    unittest.main()

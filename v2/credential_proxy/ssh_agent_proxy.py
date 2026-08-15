#!/usr/bin/env python3
"""Legacy release marker retained for one updater compatibility window."""
import sys

print("devc2: the legacy SSH relay was removed; grant --span ssh-agent", file=sys.stderr)
raise SystemExit(2)

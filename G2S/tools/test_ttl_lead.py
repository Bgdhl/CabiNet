#!/usr/bin/env python3
"""
Regression test for the clock-lead timeToLive stretch (_ttl_for).

A machine whose clock runs ahead of the hub reads every request as already
expired (G2S_APX011), so the hub stretches each outbound timeToLive by the
measured lead plus CLOCK_LEAD_TTL_MARGIN_SEC. timeToLive is an xs:int of
milliseconds: a lead past about 24.8 days (a clock set a month or a year
ahead) would stretch it out of the schema's range, and a validating machine
would refuse every request as malformed. Past the range the hub sends 0
(unexpirable) instead.

In-process (imports g2s_host directly); no network, no host process.

Run from G2S/:
    python3 tools/test_ttl_lead.py
Exits 0 if all checks pass.
"""

import logging
import re
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import g2s_host as gh  # noqa: E402

EGM_ID = "BAL_001122334455"
MARGIN = gh.CLOCK_LEAD_TTL_MARGIN_SEC
TTL_MAX = gh.G2S_TTL_MAX_MS

PASS = 0
FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}  {detail!r}")


class Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def count(self, needle):
        return sum(1 for r in self.records if needle in r.getMessage())


def assoc_with(lead):
    return SimpleNamespace(egm_id=EGM_ID, egm_clock_skew_sec=lead)


def ttl_for(assoc, ttl):
    return gh.G2SHost._ttl_for(None, assoc, ttl)


def main():
    cap = Capture()
    gh.log.addHandler(cap)
    gh.log.setLevel(logging.DEBUG)

    print("— pass-through cases")
    check("no measured skew: untouched", ttl_for(assoc_with(None), 30000) == 30000)
    check("EGM behind the hub: untouched", ttl_for(assoc_with(-90.0), 30000) == 30000)
    check("lead within the margin: untouched",
          ttl_for(assoc_with(float(MARGIN)), 30000) == 30000)
    check("timeToLive 0 stays unexpirable", ttl_for(assoc_with(70.0), 0) == 0)
    check("non-numeric timeToLive: untouched", ttl_for(assoc_with(70.0), "x") == "x")

    print("— ordinary lead (the Bally Alpha 2 bench case, 70 s fast)")
    a = assoc_with(70.0)
    check("stretched by lead + margin",
          ttl_for(a, 30000) == 30000 + (70 + MARGIN) * 1000, ttl_for(a, 30000))
    ttl_for(a, 30000)
    check("logged once per association", cap.count("stretching every timeToLive") == 1)

    print("— boundary of the int range")
    ttl = 30000
    edge = (TTL_MAX - ttl) / 1000 - MARGIN      # stretched value == TTL_MAX
    check("exactly at the maximum: still stretched",
          ttl_for(assoc_with(edge), ttl) == TTL_MAX, ttl_for(assoc_with(edge), ttl))
    check("one second past: unexpirable (0)",
          ttl_for(assoc_with(edge + 1), ttl) == 0, ttl_for(assoc_with(edge + 1), ttl))

    print("— huge lead (clock set a year ahead)")
    year = assoc_with(365 * 86400.0)
    before = cap.count("go out unexpirable")
    got = ttl_for(year, 30000)
    check("sends 0, never an out-of-range value", got == 0, got)
    ttl_for(year, 30000)
    check("logged once per association",
          cap.count("go out unexpirable") - before == 1)

    print("— on the wire")
    engine = SimpleNamespace(host_id="1", _ttl_for=lambda a, t: ttl_for(a, t))
    wire = SimpleNamespace(egm_id=EGM_ID, device_id="G2S_host", egm_clock_skew_sec=365 * 86400.0,
                           next_command_id=lambda: 1)
    xml, _ = gh.G2SHost.build_inner_command(
        engine, wire, "<g2s:keepAlive/>", "G2S_request", 1, 30000)
    m = re.search(r'g2s:timeToLive="(-?\d+)"', xml)
    check("build_inner_command carries timeToLive 0 for a year-ahead EGM",
          m is not None and m.group(1) == "0", m and m.group(1))
    for lead in (None, -90.0, 70.0, 20 * 86400.0, 30 * 86400.0, 365 * 86400.0):
        v = ttl_for(assoc_with(lead), 30000)
        check(f"lead {lead}: value within the xs:int range", 0 <= v <= TTL_MAX, v)

    print("=" * 50)
    print(f"RESULT: {PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

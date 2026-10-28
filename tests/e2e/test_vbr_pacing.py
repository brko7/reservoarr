"""VBR e2e: a stream whose bitrate drops from 3 to 1 Mbps for 40s and comes
back, from an edge that sends it in realtime (cdn_sim --pcr). Paced at the
content rate, a 90s average, the release ran the 1 Mbps stretch out faster
than realtime and then fed the player under realtime for a minute after the
bitrate came back: on 6.5.0 the player's lead here swings by several seconds,
and on an 8.7 -> 3.1 -> 8.7 Mbps bench it fell 29s behind (discussion #46).
Paced on content time, the lead holds: 0.3s of swing on macOS against 13.9s
on 6.5.0; the 1.5s bound leaves room for a loaded runner."""
from __future__ import annotations

import pytest

from .harness import player_lead, run_pipeline


@pytest.mark.e2e
def test_the_player_lead_holds_through_a_bitrate_drop(tmp_path, vbr_ts):
    # 12s front-load: 5s headstart to the player, 7s banked, far under the target.
    # The drop spans content 45-85s, i.e. ~33-73s in; the bitrate is back from ~73s.
    run = run_pipeline(tmp_path, vbr_ts, rate_bps=350_000, duration_s=125, front_s=12, pcr_paced=True)
    lead = [(t, x) for t, x in player_lead(run) if t >= 15]
    assert lead and lead[-1][0] >= 100, f"output ended early at {lead[-1][0] if lead else 0:.0f}s"
    leads = [x for _, x in lead]
    stats = "\n  ".join(s["raw"] for s in run.stats_lines())
    assert max(leads) - min(leads) < 1.5, (
        f"player lead swung {min(leads):.2f}..{max(leads):.2f}s; it should hold:\n  {stats}"
    )

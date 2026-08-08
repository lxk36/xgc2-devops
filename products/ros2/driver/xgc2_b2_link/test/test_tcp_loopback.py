"""G3 dry-run forwarder ↔ G4 ground peer over TCP framed transport."""

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def test_tcp_g3_g4_loop():
    py = sys.executable
    env = {**dict(**__import__("os").environ), "PYTHONPATH": str(ROOT)}
    ground = subprocess.Popen(
        [
            py,
            "-m",
            "xgc2_b2_link.ground_peer",
            "--robot-id",
            "b2-test",
            "--transport",
            "tcp",
            "--tcp-role",
            "server",
            "--tcp-port",
            "17448",
            "--print-hz",
            "2",
            "--send-echo",
        ],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    time.sleep(0.4)
    fwd = subprocess.Popen(
        [
            py,
            "-m",
            "xgc2_b2_link.forwarder_node",
            "--robot-id",
            "b2-test",
            "--transport",
            "tcp",
            "--tcp-role",
            "client",
            "--tcp-port",
            "17448",
            "--dry-run-no-ros",
        ],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        # wait for forwarder dry-run to finish publishing
        fwd_out, _ = fwd.communicate(timeout=10)
        time.sleep(0.8)
        ground.terminate()
        g_out, _ = ground.communicate(timeout=5)
    finally:
        if fwd.poll() is None:
            fwd.kill()
        if ground.poll() is None:
            ground.kill()

    assert "dry-run done" in (fwd_out or "")
    # ground should have printed channel summaries
    assert "n_channels" in (g_out or "")
    assert "power_summary" in (g_out or "") or "odom" in (g_out or "")

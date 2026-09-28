import os
import shutil
import subprocess

import pytest

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "..", "deploy", "setup_vps.sh")


@pytest.mark.skipif(os.name == "nt" or shutil.which("bash") is None, reason="needs a Linux bash")
def test_vps_setup_script_is_valid_bash():
    subprocess.run(["bash", "-n", SCRIPT], check=True)


def test_vps_service_stops_the_bot_gracefully():
    with open(SCRIPT, encoding="utf-8") as f:
        text = f.read()
    assert "-m scalper run --yes" in text
    assert "KillSignal=SIGINT" in text  # same as Ctrl+C: state is saved, exchange stops stay
    assert "RestartPreventExitStatus=2" in text  # no restart loop on a config error

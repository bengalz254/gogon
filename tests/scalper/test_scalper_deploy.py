import os
import shutil
import subprocess

import pytest

from scalper.dashboard import DEFAULT_PORT

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
SCRIPT = os.path.join(ROOT, "deploy", "setup_vps.sh")
SHORTCUT = os.path.join(ROOT, "deploy", "scalper_dashboard.bat")


@pytest.mark.skipif(os.name == "nt" or shutil.which("bash") is None, reason="needs a Linux bash")
@pytest.mark.parametrize("name", ["setup_vps.sh", "health.sh"])
def test_vps_scripts_are_valid_bash(name):
    subprocess.run(["bash", "-n", os.path.join(ROOT, "deploy", name)], check=True)


def test_vps_service_stops_the_bot_gracefully():
    with open(SCRIPT, encoding="utf-8") as f:
        text = f.read()
    assert "-m scalper run --yes" in text
    assert "KillSignal=SIGINT" in text  # same as Ctrl+C: state is saved, exchange stops stay
    assert "RestartPreventExitStatus=2" in text  # no restart loop on a config error


def test_windows_shortcut_opens_the_dashboard_port():
    with open(SHORTCUT, "rb") as f:
        text = f.read().decode("ascii")  # cmd.exe reads batch files in the OEM code page
    assert f'set "PORT={DEFAULT_PORT}"' in text
    assert "-L %PORT%:127.0.0.1:%PORT%" in text  # only the VPS's own localhost is forwarded
    with open(os.path.join(ROOT, ".gitattributes"), encoding="utf-8") as f:
        assert "*.bat text eol=crlf" in f.read()  # cmd.exe misreads labels in LF-only files

"""The launchd supervisor template (#28): drift here recreates the
hand-started posture where a crash or reboot silently ends the service and
the fleet's deploy watcher has nothing to restart through.

Pure file checks - no launchctl, no network - so they run in CI unchanged.
"""

import os
import re
import stat
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

OPS = Path(__file__).resolve().parent.parent / "ops"
TEMPLATE = OPS / "dev.spendglass.server.plist.template"
INSTALLER = OPS / "install-supervisor.sh"
ROTATE = OPS / "rotate-log.sh"
START = OPS.parent / "start.sh"


def test_template_is_wellformed_and_supervises():
    assert TEMPLATE.exists(), "plist template missing"
    body = TEMPLATE.read_text()
    for token in ("{{REPO_DIR}}", "{{HOME}}", "{{PATH}}"):
        assert token in body, f"template lost its {token} placeholder"
    root = ET.fromstring(body)
    keys = [el.text for el in root.iter("key")]
    for required in ("Label", "RunAtLoad", "KeepAlive", "ThrottleInterval",
                     "StandardOutPath", "EnvironmentVariables"):
        assert required in keys, f"template missing <key>{required}</key>"
    kids = list(root.find("dict"))
    label_val = kids[kids.index(next(k for k in kids if k.text == "Label")) + 1].text
    assert label_val == "dev.spendglass.server"


def test_template_starts_the_real_launcher():
    """The agent must run start.sh (venv creation, dependency stamp, port
    guard, .env permission repair) - not python directly, which would skip
    every one of those protections."""
    assert "start.sh" in TEMPLATE.read_text()


def test_installer_targets_the_same_label():
    body = INSTALLER.read_text()
    assert 'LABEL="dev.spendglass.server"' in body
    assert "plutil -lint" in body, "installer must validate the rendered plist"


def _rotate(log: Path, max_bytes: int) -> str:
    return subprocess.run(["bash", str(ROTATE), str(log), str(max_bytes)],
                          check=True, capture_output=True, text=True).stdout


def test_service_log_rolls_over_past_its_cap(tmp_path):
    """Issue #95: launchd appends to data/service.log and never trims it.
    Past the cap the log moves to service.log.1, owner-only like the log,
    and starts again empty in place, so launchd's handle stays valid."""
    log = tmp_path / "service.log"
    log.write_text("x" * 200)
    os.chmod(log, 0o600)
    (tmp_path / "service.log.1").write_text("the generation before")
    inode = log.stat().st_ino
    assert "rotated" in _rotate(log, 100)
    assert log.read_text() == "" and log.stat().st_ino == inode
    old = tmp_path / "service.log.1"
    assert old.read_text() == "x" * 200
    assert stat.S_IMODE(old.stat().st_mode) == 0o600


def test_service_log_under_its_cap_is_left_alone(tmp_path):
    log = tmp_path / "service.log"
    log.write_text("x" * 50)
    assert _rotate(log, 100) == ""
    assert log.read_text() == "x" * 50
    assert not (tmp_path / "service.log.1").exists()
    assert _rotate(tmp_path / "missing.log", 100) == ""


def test_start_rotates_the_log_the_agent_writes():
    """start.sh rotates before anything else prints, and the file it rotates
    is the one the plist sends output to."""
    start = START.read_text()
    assert "bash ops/rotate-log.sh data/service.log" in start
    assert start.index("rotate-log.sh") < start.index("exec ")
    assert "{{REPO_DIR}}/data/service.log" in TEMPLATE.read_text()


def test_installer_stops_only_the_port_the_agent_binds():
    """Issue #96: the installer read SPENDGLASS_UI_PORT from .env, which the
    server never reads, so it could stop a process on a port Spendglass
    doesn't use. The agent passes the server only HOME and PATH, so the
    supervised server binds ui.py's default, and so must the installer."""
    ui_src = (OPS.parent / "spendglass" / "ui.py").read_text()
    default = re.search(
        r'os\.environ\.get\("SPENDGLASS_UI_PORT", "(\d+)"\)', ui_src).group(1)

    installer = INSTALLER.read_text()
    assert re.findall(r"^PORT=(\S+)$", installer, re.M) == [default]
    assert ".env" not in "".join(
        ln for ln in installer.splitlines()
        if "PORT" in ln and not ln.lstrip().startswith("#"))

    root = ET.fromstring(TEMPLATE.read_text())
    kids = list(root.find("dict"))
    env = kids[kids.index(next(k for k in kids
                               if k.text == "EnvironmentVariables")) + 1]
    assert [k.text for k in env.iter("key")] == ["HOME", "PATH"]

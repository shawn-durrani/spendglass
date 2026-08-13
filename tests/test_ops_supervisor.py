"""The launchd supervisor template (#28): drift here recreates the
hand-started posture where a crash or reboot silently ends the service and
the fleet's deploy watcher has nothing to restart through.

Pure file checks - no launchctl, no network - so they run in CI unchanged.
"""

import xml.etree.ElementTree as ET
from pathlib import Path

OPS = Path(__file__).resolve().parent.parent / "ops"
TEMPLATE = OPS / "dev.spendglass.server.plist.template"
INSTALLER = OPS / "install-supervisor.sh"


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

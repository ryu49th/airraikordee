import os
import re

GATEWAY = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SYSTEMD = os.path.join(GATEWAY, "systemd")


def modules():
    out = []
    for line in open(os.path.join(SYSTEMD, "modules.txt"), encoding="utf-8"):
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(line)
    return out


def test_every_listed_module_exists_and_has_main():
    listed = modules()
    assert len(listed) == len(set(listed)) and "bridge.uart_bridge" in listed and "brain.decision" in listed
    for m in listed:
        path = os.path.join(GATEWAY, *m.split(".")) + ".py"
        src = open(path, encoding="utf-8").read()
        assert re.search(r"^def main\(", src, re.M), m
        assert 'if __name__ == "__main__":' in src, m


def test_every_service_module_is_listed():
    """A new module with main() must be added to modules.txt (or it never starts on the Jetson)."""
    found = set()
    for folder in os.listdir(GATEWAY):
        d = os.path.join(GATEWAY, folder)
        if folder in ("tests", "tools", "common") or not os.path.isfile(os.path.join(d, "__init__.py")):
            continue
        for name in os.listdir(d):
            if name.endswith(".py") and re.search(r"^def main\(", open(os.path.join(d, name), encoding="utf-8").read(), re.M):
                found.add(folder + "." + name[:-3])
    assert found == set(modules())


def test_unit_template_runs_the_module_from_the_gateway_folder():
    unit = open(os.path.join(SYSTEMD, "airraikordee@.service"), encoding="utf-8").read()
    assert "ExecStart=/usr/bin/python3 -m %i" in unit and "WorkingDirectory=@GATEWAY_DIR@" in unit
    assert "User=@USER@" in unit and "Restart=always" in unit and "WantedBy=airraikordee.target" in unit
    target = open(os.path.join(SYSTEMD, "airraikordee.target"), encoding="utf-8").read()
    assert "WantedBy=multi-user.target" in target
    assert os.access(os.path.join(SYSTEMD, "install.sh"), os.X_OK)

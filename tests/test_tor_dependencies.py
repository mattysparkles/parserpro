import install_tools


def test_get_missing_tor_dependencies_reports_only_missing_modules():
    available = {"stem", "requests"}

    def fake_finder(name):
        return object() if name in available else None

    assert install_tools.get_missing_tor_dependencies(module_finder=fake_finder) == ["pysocks"]


def test_install_tor_dependencies_skips_pip_when_nothing_missing():
    result = install_tools.install_tor_dependencies(missing_packages=[])

    assert result == {
        "ok": True,
        "message": "Tor Python dependencies already installed",
        "installed": False,
        "missing": [],
    }


def test_ensure_tor_dependencies_attempts_install_for_missing(monkeypatch):
    monkeypatch.setattr(install_tools, "get_missing_tor_dependencies", lambda module_finder=None: ["stem", "pysocks"])

    calls = []

    def fake_install(log_func=None, missing_packages=None):
        calls.append(list(missing_packages or []))
        return {"ok": True, "message": "installed", "installed": True, "missing": list(missing_packages or [])}

    monkeypatch.setattr(install_tools, "install_tor_dependencies", fake_install)

    result = install_tools.ensure_tor_dependencies()

    assert result["ok"] is True
    assert calls == [["stem", "pysocks"]]

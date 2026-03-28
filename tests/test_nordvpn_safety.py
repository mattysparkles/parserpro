import config
import install_tools


def test_ensure_nordvpn_cli_does_not_open_browser_by_default(monkeypatch):
    monkeypatch.setattr(config.shutil, "which", lambda _name: None)
    opened = []
    monkeypatch.setattr(config.webbrowser, "open", lambda url: opened.append(url))

    result = config.ensure_nordvpn_cli()

    assert result == {"available": False, "path": None}
    assert opened == []


def test_ensure_nordvpn_cli_optionally_opens_download_page(monkeypatch):
    monkeypatch.setattr(config.shutil, "which", lambda _name: None)
    opened = []
    monkeypatch.setattr(config.webbrowser, "open", lambda url: opened.append(url))

    result = config.ensure_nordvpn_cli(auto_open_download_page=True)

    assert result == {"available": False, "path": None}
    assert opened == ["https://nordvpn.com/download/windows/"]


def test_check_nordvpn_onion_support_skips_groups_probe_by_default(monkeypatch):
    monkeypatch.setattr(install_tools.shutil, "which", lambda _name: "nordvpn")

    def _unexpected_run(*_args, **_kwargs):
        raise AssertionError("subprocess.run should not execute when probe_groups is disabled")

    monkeypatch.setattr(install_tools.subprocess, "run", _unexpected_run)

    result = install_tools.check_nordvpn_onion_support()

    assert result["ok"] is True
    assert "detected" in result["message"].lower()

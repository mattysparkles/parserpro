from config import _is_apt_lock_error, build_wsl_command, build_wsl_sudo_command


def test_build_wsl_command_with_distro_and_user():
    cmd = build_wsl_command("hydra --version", distro="kali-linux", username="alice")
    assert cmd == ["wsl", "-d", "kali-linux", "-u", "alice", "bash", "-lc", "hydra --version"]


def test_build_wsl_sudo_command_quotes_password():
    cmd = build_wsl_sudo_command("apt update", password="pa ss$word")
    assert cmd.startswith("echo '")
    assert "sudo -S apt update" in cmd


def test_build_wsl_sudo_command_without_password():
    assert build_wsl_sudo_command("apt update", password="") == "sudo apt update"


def test_build_wsl_sudo_command_without_password_non_interactive():
    assert build_wsl_sudo_command("apt update", password="", non_interactive=True) == "sudo -n apt update"


def test_is_apt_lock_error_detection():
    assert _is_apt_lock_error("Could not get lock /var/lib/apt/lists/lock")
    assert _is_apt_lock_error("Unable to lock directory")
    assert not _is_apt_lock_error("sudo: a password is required")

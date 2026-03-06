from config import build_wsl_command, build_wsl_sudo_command


def test_build_wsl_command_with_distro_and_user():
    cmd = build_wsl_command("hydra --version", distro="kali-linux", username="alice")
    assert cmd == ["wsl", "-d", "kali-linux", "-u", "alice", "bash", "-lc", "hydra --version"]


def test_build_wsl_sudo_command_quotes_password():
    cmd = build_wsl_sudo_command("apt update", password="pa ss$word")
    assert cmd.startswith("echo '")
    assert "sudo -S apt update" in cmd


def test_build_wsl_sudo_command_without_password():
    assert build_wsl_sudo_command("apt update", password="") == "sudo apt update"

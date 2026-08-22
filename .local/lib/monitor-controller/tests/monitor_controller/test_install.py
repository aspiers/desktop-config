"""Locked fixed-venv installer and deployment-source tests."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT = Path(__file__).parents[2]
REPOSITORY = Path(__file__).parents[5]
INSTALLER = PROJECT / "install.sh"


def _install_environment(tmp_path: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "HOME": str(tmp_path / "home"),
            "XDG_DATA_HOME": str(tmp_path / "home" / ".local" / "share"),
        }
    )
    environment.pop("VIRTUAL_ENV", None)
    return environment


def _venv(tmp_path: Path) -> Path:
    return tmp_path / "home" / ".local" / "share" / "monitor-controller" / "venv"


def _copy_project(target: Path) -> None:
    target.mkdir()
    shutil.copy2(INSTALLER, target / "install.sh")
    shutil.copy2(PROJECT / "uv.lock", target / "uv.lock")
    shutil.copy2(PROJECT / "pyproject.toml", target / "pyproject.toml")
    shutil.copytree(PROJECT / "monitor_controller", target / "monitor_controller")


def _run_installer(
    installer: Path,
    tmp_path: Path,
    *,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ("/bin/sh", str(installer)),
        check=check,
        capture_output=True,
        env=_install_environment(tmp_path),
        text=True,
        timeout=180,
    )


def test_installer_uses_locked_noneditable_runtime_sync() -> None:
    text = INSTALLER.read_text(encoding="utf-8")

    assert "venv=$install_root/venv" in text
    assert "--locked" in text
    assert "--no-dev" in text
    assert "--no-editable" in text
    assert "--reinstall-package monitor-controller" in text
    assert "--no-python-downloads" in text
    assert 'UV_PROJECT_ENVIRONMENT="$venv"' in text
    assert "uv run" not in text
    assert "pip install" not in text
    assert "--offline" not in text
    assert "This installation step is not offline" in text
    assert "import monitor_controller.shadow" in text


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is required by installer")
def test_locked_install_is_idempotent_in_temporary_xdg_data_home(
    tmp_path: Path,
) -> None:
    first = _run_installer(INSTALLER, tmp_path)
    venv = _venv(tmp_path)
    python = venv / "bin" / "python"
    first_inode = python.stat().st_ino

    second = _run_installer(INSTALLER, tmp_path)

    assert first.returncode == second.returncode == 0
    assert first_inode == python.stat().st_ino
    assert (venv / "bin" / "monitor-controller").is_file()
    assert not (venv / "bin" / "pytest").exists()
    assert not (venv / "bin" / "ruff").exists()
    import_command = (
        "from pathlib import Path; import monitor_controller; "
        "import monitor_controller.shadow; "
        "print(Path(monitor_controller.__file__).resolve()); "
        "print(Path(monitor_controller.shadow.__file__).resolve())"
    )
    imported = subprocess.run(  # noqa: S603
        (str(python), "-I", "-c", import_command),
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.splitlines()
    assert len(imported) == 2
    assert all(Path(path).is_relative_to(venv.resolve()) for path in imported)
    assert all(not Path(path).is_relative_to(PROJECT.resolve()) for path in imported)
    assert "Installed locked monitor-controller runtime" in second.stdout


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is required by installer")
def test_reinstall_updates_changed_source_without_a_version_bump(
    tmp_path: Path,
) -> None:
    project = tmp_path / "updated-project"
    _copy_project(project)
    _run_installer(project / "install.sh", tmp_path)
    package = project / "monitor_controller" / "__init__.py"
    package.write_text(
        package.read_text(encoding="utf-8").replace(
            '__version__ = "0.1.0"',
            '__version__ = "0.1.0-shadow-update"',
        ),
        encoding="utf-8",
    )

    _run_installer(project / "install.sh", tmp_path)

    python = _venv(tmp_path) / "bin" / "python"
    installed_version = subprocess.run(  # noqa: S603
        (
            str(python),
            "-I",
            "-c",
            "import monitor_controller; print(monitor_controller.__version__)",
        ),
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()
    assert installed_version == "0.1.0-shadow-update"


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is required by installer")
def test_locked_install_rejects_metadata_drift_without_rewriting_lock(
    tmp_path: Path,
) -> None:
    project = tmp_path / "drifted-project"
    _copy_project(project)
    pyproject = project / "pyproject.toml"
    pyproject.write_text(
        pyproject.read_text(encoding="utf-8").replace(
            "dependencies = []",
            'dependencies = ["definitely-unlocked-package==0.0.1"]',
        ),
        encoding="utf-8",
    )
    before = (project / "uv.lock").read_bytes()

    completed = _run_installer(project / "install.sh", tmp_path, check=False)

    assert completed.returncode != 0
    assert (project / "uv.lock").read_bytes() == before
    assert "lock" in completed.stderr.casefold()


def test_installer_and_unit_agree_on_the_canonical_xdg_data_path() -> None:
    unit = (
        REPOSITORY / ".config/systemd/user/monitor-controller-shadow.service"
    ).read_text(encoding="utf-8")
    installer = INSTALLER.read_text(encoding="utf-8")

    assert "service_data_home=$home/.local/share" in installer
    assert 'if [ "$data_home" != "$service_data_home" ]; then' in installer
    assert (
        "ExecStart=%h/.local/share/monitor-controller/venv/bin/python "
        "-I -m monitor_controller.shadow"
    ) in unit
    assert "Environment=XDG_DATA_HOME=%h/.local/share" in unit


def test_installer_rejects_noncanonical_absolute_xdg_data_home(
    tmp_path: Path,
) -> None:
    environment = _install_environment(tmp_path)
    environment["XDG_DATA_HOME"] = str(tmp_path / "other-data")

    completed = subprocess.run(  # noqa: S603
        ("/bin/sh", str(INSTALLER)),
        check=False,
        capture_output=True,
        env=environment,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 2
    assert "Unsupported XDG_DATA_HOME" in completed.stderr
    assert "requires" in completed.stderr


def test_installer_rejects_relative_xdg_data_home_before_install(
    tmp_path: Path,
) -> None:
    environment = _install_environment(tmp_path)
    environment["XDG_DATA_HOME"] = "relative/data"

    completed = subprocess.run(  # noqa: S603
        ("/bin/sh", str(INSTALLER)),
        check=False,
        capture_output=True,
        env=environment,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 2
    assert "absolute paths" in completed.stderr


def test_stow_deploys_source_but_excludes_local_build_artifacts() -> None:
    ignore = (REPOSITORY / ".stow-local-ignore").read_text(encoding="utf-8")

    assert "^/\\.local/lib/monitor-controller$" not in ignore
    assert (
        "^/\\.local/lib/monitor-controller/"
        "(\\.venv|\\.hypothesis|\\.pytest_cache|dist|.*/__pycache__)$"
    ) in ignore


def test_real_stow_dry_run_accepts_the_deployment_tree(tmp_path: Path) -> None:
    stow = shutil.which("stow")
    if stow is None:
        pytest.skip("GNU Stow is unavailable")
    target = tmp_path / "home"
    target.mkdir()

    completed = subprocess.run(  # noqa: S603
        (stow, "-n", "-d", str(REPOSITORY), "-t", str(target), "."),
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert completed.returncode == 0, completed.stderr

"""Tests for the guarded configuration-file reader.

These exist because the reader had no tests at all, in either composition
root, which is how the authoritative controller came to read its theme file
with none of these checks while the non-authoritative one kept them
(`dc-t53`). Each test below names the specific way a plain
`Path.read_text()` would behave differently.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from monitor_controller.active import ActivePaths, ActiveStartupError, active_theme
from monitor_controller.safeio import (
    DEFAULT_REFERENCE_DPI,
    MAX_CONFIGURATION_BYTES,
    read_bounded_text,
    read_reference_dpi,
)
from monitor_controller.shadow import ShadowPaths, ShadowStartupError, shadow_theme


class _ProbeError(RuntimeError):
    """Stand-in for a composition root's startup error."""


def _paths(root: Path) -> ActivePaths:
    """Build active paths rooted entirely inside a temporary directory."""
    return ActivePaths(
        data_home=root / "data",
        state_home=root / "state",
        runtime_dir=root / "runtime",
        config_home=root / "config",
        desktop_configuration_root=root / "desktop-config",
    )


class TestReadBoundedText:
    """What the reader accepts, and what it refuses."""

    def test_it_reads_an_ordinary_file(self, tmp_path: Path) -> None:
        """The common case must still work."""
        path = tmp_path / "theme"
        path.write_text("dark\n", encoding="utf-8")
        assert read_bounded_text(path, "desktop:theme", _ProbeError) == "dark\n"

    def test_it_refuses_a_symlink(self, tmp_path: Path) -> None:
        """The file read must be the file named.

        Without `O_NOFOLLOW` the reader silently follows the link and returns
        content from somewhere else entirely.
        """
        (tmp_path / "elsewhere").write_text("light", encoding="utf-8")
        link = tmp_path / "theme"
        link.symlink_to(tmp_path / "elsewhere")

        with pytest.raises(_ProbeError, match="cannot read desktop:theme"):
            read_bounded_text(link, "desktop:theme", _ProbeError)

    def test_it_refuses_a_directory(self, tmp_path: Path) -> None:
        """A directory is not a configuration file."""
        directory = tmp_path / "theme"
        directory.mkdir()
        with pytest.raises(_ProbeError):
            read_bounded_text(directory, "desktop:theme", _ProbeError)

    def test_it_refuses_a_fifo_rather_than_blocking(self, tmp_path: Path) -> None:
        """A FIFO is the case that would hang startup indefinitely.

        `Path.read_text()` on a FIFO with no writer blocks forever, so the
        controller would never finish starting and would report nothing. The
        regular-file check turns that into an immediate, explained refusal.

        The read runs on a worker thread with a timeout, so this test fails
        rather than hangs if the check is ever removed.
        """
        fifo = tmp_path / "theme"
        os.mkfifo(fifo)
        outcome: list[object] = []

        def _attempt() -> None:
            try:
                outcome.append(read_bounded_text(fifo, "desktop:theme", _ProbeError))
            except BaseException as error:  # noqa: BLE001 - recorded for assertion
                outcome.append(error)

        worker = threading.Thread(target=_attempt, daemon=True)
        worker.start()
        worker.join(timeout=5.0)

        assert not worker.is_alive(), "reader blocked on a FIFO instead of refusing"
        assert isinstance(outcome[0], _ProbeError)

    def test_it_refuses_an_oversized_file(self, tmp_path: Path) -> None:
        """A file holding one word must not be able to hold a gigabyte."""
        path = tmp_path / "theme"
        path.write_bytes(b"d" * (MAX_CONFIGURATION_BYTES + 1))
        with pytest.raises(_ProbeError, match="larger than"):
            read_bounded_text(path, "desktop:theme", _ProbeError)

    def test_a_file_at_exactly_the_limit_is_accepted(self, tmp_path: Path) -> None:
        """The bound is inclusive; off-by-one here would reject valid input."""
        path = tmp_path / "big"
        path.write_bytes(b"d" * MAX_CONFIGURATION_BYTES)
        assert len(read_bounded_text(path, "probe", _ProbeError)) == (
            MAX_CONFIGURATION_BYTES
        )

    def test_it_refuses_invalid_utf8(self, tmp_path: Path) -> None:
        """Binary content must be reported, not silently mangled."""
        path = tmp_path / "theme"
        path.write_bytes(b"\xff\xfe")
        with pytest.raises(_ProbeError, match="not valid UTF-8"):
            read_bounded_text(path, "desktop:theme", _ProbeError)

    def test_it_refuses_a_missing_file(self, tmp_path: Path) -> None:
        """Absence is the caller's decision to default, not the reader's."""
        with pytest.raises(_ProbeError, match="cannot read"):
            read_bounded_text(tmp_path / "absent", "desktop:theme", _ProbeError)

    def test_the_caller_chooses_the_error_type(self, tmp_path: Path) -> None:
        """Each composition root catches its own startup error."""
        with pytest.raises(ActiveStartupError):
            read_bounded_text(tmp_path / "absent", "probe", ActiveStartupError)

    def test_the_reference_appears_in_every_message(self, tmp_path: Path) -> None:
        """A path alone rarely says what the file was for."""
        path = tmp_path / "f"
        path.write_bytes(b"\xff")
        with pytest.raises(_ProbeError, match=r"autorandr:settings\.ini"):
            read_bounded_text(path, "autorandr:settings.ini", _ProbeError)

    def test_it_leaks_no_descriptor_on_refusal(self, tmp_path: Path) -> None:
        """Startup may try several files; a leak per refusal would accumulate."""
        directory = tmp_path / "theme"
        directory.mkdir()
        open_descriptors = Path("/proc/self/fd")
        before = len(list(open_descriptors.iterdir()))
        for _ in range(20):
            with pytest.raises(_ProbeError):
                read_bounded_text(directory, "probe", _ProbeError)
        assert len(list(open_descriptors.iterdir())) <= before


class TestActiveTheme:
    """The authoritative controller's own use of the reader."""

    def test_it_reads_the_configured_theme(self, tmp_path: Path) -> None:
        """The ordinary case."""
        paths = _paths(tmp_path)
        paths.config_home.mkdir(parents=True)
        (paths.config_home / "theme").write_text("light\n", encoding="utf-8")
        assert active_theme(paths) == "light"

    def test_absent_theme_defaults_to_dark(self, tmp_path: Path) -> None:
        """A missing file is normal, not an error."""
        paths = _paths(tmp_path)
        paths.config_home.mkdir(parents=True)
        assert active_theme(paths) == "dark"

    def test_it_refuses_a_symlinked_theme(self, tmp_path: Path) -> None:
        """The regression: the authoritative controller followed symlinks.

        This assertion fails against the version shipped in `a6599ff`.
        """
        paths = _paths(tmp_path)
        paths.config_home.mkdir(parents=True)
        (tmp_path / "elsewhere").write_text("light", encoding="utf-8")
        (paths.config_home / "theme").symlink_to(tmp_path / "elsewhere")

        with pytest.raises(ActiveStartupError, match="cannot read desktop:theme"):
            active_theme(paths)

    def test_it_refuses_an_unrecognised_theme(self, tmp_path: Path) -> None:
        """Only two values are meaningful downstream."""
        paths = _paths(tmp_path)
        paths.config_home.mkdir(parents=True)
        (paths.config_home / "theme").write_text("mauve", encoding="utf-8")
        with pytest.raises(ActiveStartupError, match="dark or light"):
            active_theme(paths)


def test_both_composition_roots_read_the_theme_identically(tmp_path: Path) -> None:
    """The two must not drift apart again.

    `dc-t53` was exactly this: the same function written twice, one copy
    guarded and one not, with nothing comparing them. Asserting agreement is
    what stops a future edit to one going unnoticed.
    """
    config_home = tmp_path / "config"
    config_home.mkdir(parents=True)
    active = _paths(tmp_path)
    shadow = ShadowPaths(
        data_home=tmp_path / "data",
        state_home=tmp_path / "state",
        runtime_dir=tmp_path / "runtime",
        config_home=config_home,
        desktop_configuration_root=tmp_path / "desktop-config",
    )

    for value in ("dark", "light"):
        (config_home / "theme").write_text(value, encoding="utf-8")
        assert active_theme(active) == shadow_theme(shadow) == value

    (config_home / "theme").unlink()
    assert active_theme(active) == shadow_theme(shadow) == "dark"

    # A symlink must be refused by both, not just by shadow.
    (tmp_path / "elsewhere").write_text("light", encoding="utf-8")
    (config_home / "theme").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(ActiveStartupError):
        active_theme(active)
    with pytest.raises(ShadowStartupError):
        shadow_theme(shadow)


class TestReadReferenceDpi:
    """Reading the desktop's current dots-per-inch, and failing softly."""

    @staticmethod
    def _fake_query(tmp_path: Path, body: str) -> None:
        """Put a fake xfconf-query first on PATH."""
        fake = tmp_path / "bin"
        fake.mkdir(exist_ok=True)
        script = fake / "xfconf-query"
        script.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
        script.chmod(0o755)
        os.environ["PATH"] = f"{fake}:{os.environ['PATH']}"

    def test_it_reads_the_configured_value(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The ordinary case."""
        monkeypatch.setenv("PATH", os.environ["PATH"])
        self._fake_query(tmp_path, "echo 139")
        assert read_reference_dpi() == 139

    def test_absent_command_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A host without xfconf must still start."""
        monkeypatch.setenv("PATH", "/nonexistent")
        assert read_reference_dpi() == DEFAULT_REFERENCE_DPI

    def test_failing_command_falls_back(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An unset property exits non-zero; that is not an error here."""
        monkeypatch.setenv("PATH", os.environ["PATH"])
        self._fake_query(tmp_path, "exit 1")
        assert read_reference_dpi() == DEFAULT_REFERENCE_DPI

    def test_unparseable_output_falls_back(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Whatever xfconf prints, it must not crash startup."""
        monkeypatch.setenv("PATH", os.environ["PATH"])
        self._fake_query(tmp_path, "echo 'Property does not exist'")
        assert read_reference_dpi() == DEFAULT_REFERENCE_DPI

    def test_nonpositive_value_falls_back(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Zero would make every computed scale zero."""
        monkeypatch.setenv("PATH", os.environ["PATH"])
        self._fake_query(tmp_path, "echo 0")
        assert read_reference_dpi() == DEFAULT_REFERENCE_DPI

    def test_the_default_is_overridable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Callers may supply their own fallback."""
        monkeypatch.setenv("PATH", "/nonexistent")
        assert read_reference_dpi(default=120) == 120

    def test_it_matches_the_legacy_fallback(self) -> None:
        """The shell falls back to 96 too.

        If these diverge, the two paths compute different font and panel sizes
        on any host without xfconf (`dc-qu6`).
        """
        legacy = Path("/home/adam/.GIT/adamspiers.org/desktop-config/lib/libdpy.py")
        if not legacy.is_file():
            pytest.skip("legacy libdpy.py not present")
        assert "reference_dpi = 96" in legacy.read_text(encoding="utf-8")
        assert DEFAULT_REFERENCE_DPI == 96


def test_shell_and_python_agree_on_the_reference_dpi() -> None:
    """Both must read the same value from the same setting.

    This is the divergence `dc-qu6` records: the shell read the live
    dots-per-inch while the planner assumed 96, so on a 139 dpi display one
    computed a scale of 1.0 and the other 1.46. Since the scale is
    `physical_dpi / reference_dpi` on both sides, agreeing on the reference is
    what makes the scales agree.

    Runs against the real desktop, so it skips where there is none.
    """
    if shutil.which("xfconf-query") is None:
        pytest.skip("xfconf-query not installed")
    xfconf = shutil.which("xfconf-query")
    if xfconf is None:  # pragma: no cover - guarded by the skip above
        pytest.skip("xfconf-query not installed")
    probe = subprocess.run(  # noqa: S603 - fixed argv, resolved path, no shell
        [xfconf, "-c", "xsettings", "-p", "/Xft/DPI"],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0:
        pytest.skip("Xft.DPI is not set on this host")

    assert read_reference_dpi() == int(probe.stdout.strip())


def test_the_scale_changes_with_the_reference() -> None:
    """Guards the assertion above against being vacuous.

    If `read_reference_dpi` silently returned the fallback, the test above
    would still pass on a host where Xft.DPI happens to be 96. This proves the
    reference actually moves the answer, using libdpy as the oracle since both
    sides use the same formula.
    """
    legacy_root = Path("/home/adam/.GIT/adamspiers.org/desktop-config/lib")
    if not (legacy_root / "libdpy.py").is_file():
        pytest.skip("legacy libdpy.py not present")
    if not os.environ.get("DISPLAY"):
        pytest.skip("no X display")

    def _scale(reference: int) -> float:
        program = (
            f"import sys; sys.path.insert(0, {str(legacy_root)!r}); import libdpy; "
            f"print(libdpy.calculate_ui_scale_factor(reference_dpi={reference}))"
        )
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [sys.executable, "-c", program],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            pytest.skip(f"legacy libdpy unavailable: {result.stderr.strip()[:120]}")
        return float(result.stdout.strip())

    live = read_reference_dpi()
    if live == DEFAULT_REFERENCE_DPI:
        pytest.skip("live reference equals the fallback; nothing to distinguish")

    assert _scale(live) != _scale(DEFAULT_REFERENCE_DPI), (
        "the reference dpi must change the computed scale, "
        "or this comparison proves nothing"
    )

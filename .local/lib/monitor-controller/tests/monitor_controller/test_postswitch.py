"""Tests for reading manual-autorandr notifications.

The notification file is written by a shell hook that runs whenever the user
types an autorandr command, so it is untrusted input: it can be truncated,
empty, stale, or absent. None of those may crash the controller or be
mistaken for a real topology change.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from monitor_controller.postswitch import (
    MAX_NOTIFICATION_BYTES,
    PostswitchNotification,
    PostswitchNotificationError,
    parse_notification,
    read_notification,
)

REPOSITORY = Path(__file__).parents[5]
HOOK = REPOSITORY / ".config" / "autorandr" / "postswitch"


class TestParseNotification:
    """Parsing the hook's key=value record."""

    def test_profile_and_monitors_are_parsed(self) -> None:
        """The ordinary record the hook writes."""
        parsed = parse_notification("profile=celtic+ultrawide\nmonitors=DP-9:eDP\n")
        assert parsed.profile == "celtic+ultrawide"
        assert parsed.monitors == ("DP-9", "eDP")

    def test_missing_monitors_is_allowed(self) -> None:
        """Autorandr does not always export AUTORANDR_MONITORS."""
        parsed = parse_notification("profile=celtic\nmonitors=\n")
        assert parsed.profile == "celtic"
        assert parsed.monitors == ()

    def test_unknown_keys_are_ignored(self) -> None:
        """A new hook field must not require a synchronised deployment."""
        parsed = parse_notification("profile=celtic\nfuture_field=whatever\n")
        assert parsed.profile == "celtic"

    def test_missing_profile_is_refused(self) -> None:
        """Profile is the only field carrying meaning."""
        with pytest.raises(PostswitchNotificationError, match="no profile"):
            parse_notification("monitors=DP-9:eDP\n")

    def test_empty_profile_is_refused(self) -> None:
        """An empty value is as useless as a missing key."""
        with pytest.raises(PostswitchNotificationError, match="no profile"):
            parse_notification("profile=\nmonitors=eDP\n")

    def test_empty_file_is_refused(self) -> None:
        """A truncated write must not read as a valid notification."""
        with pytest.raises(PostswitchNotificationError):
            parse_notification("")

    def test_garbage_is_refused(self) -> None:
        """Unstructured text must not parse as a notification."""
        with pytest.raises(PostswitchNotificationError):
            parse_notification("this is not a key value record\n")

    def test_overlong_profile_is_refused(self) -> None:
        """Bounded input keeps paths and logs sane."""
        with pytest.raises(PostswitchNotificationError, match="too long"):
            PostswitchNotification(profile="x" * 1000)

    def test_control_characters_are_refused(self) -> None:
        """A profile name is used in paths and logs; keep it boring."""
        with pytest.raises(PostswitchNotificationError, match="control character"):
            PostswitchNotification(profile="celtic\x00evil")


class TestReadNotification:
    """Reading and consuming the file."""

    def test_absent_file_is_not_an_error(self, tmp_path: Path) -> None:
        """No manual change is the overwhelmingly common case."""
        assert read_notification(tmp_path / "missing") is None

    def test_valid_notification_is_read_and_consumed(self, tmp_path: Path) -> None:
        """Consuming means a single manual change is acted on exactly once."""
        path = tmp_path / "autorandr-postswitch"
        path.write_text("profile=celtic+ultrawide\nmonitors=DP-9:eDP\n")

        notification = read_notification(path)
        assert notification is not None
        assert notification.profile == "celtic+ultrawide"
        assert not path.exists()
        assert read_notification(path) is None

    def test_malformed_notification_is_left_for_diagnosis(self, tmp_path: Path) -> None:
        """Silently deleting bad input destroys the evidence of the bug."""
        path = tmp_path / "autorandr-postswitch"
        path.write_text("profile=\n")
        with pytest.raises(PostswitchNotificationError):
            read_notification(path)
        assert path.exists()

    def test_oversized_file_is_refused(self, tmp_path: Path) -> None:
        """Something that large was not written by the hook."""
        path = tmp_path / "autorandr-postswitch"
        path.write_text("profile=celtic\n" + "x" * MAX_NOTIFICATION_BYTES)
        with pytest.raises(PostswitchNotificationError, match="implausibly large"):
            read_notification(path)

    def test_non_utf8_is_refused(self, tmp_path: Path) -> None:
        """Binary content is not a notification."""
        path = tmp_path / "autorandr-postswitch"
        path.write_bytes(b"profile=\xff\xfe\n")
        with pytest.raises(PostswitchNotificationError, match="UTF-8"):
            read_notification(path)

    def test_symlink_is_refused(self, tmp_path: Path) -> None:
        """The runtime dir is private, but O_NOFOLLOW costs nothing."""
        target = tmp_path / "target"
        target.write_text("profile=celtic\n")
        link = tmp_path / "autorandr-postswitch"
        link.symlink_to(target)
        with pytest.raises(PostswitchNotificationError):
            read_notification(link)


class TestHookContract:
    """The reader must match what the shell hook actually writes.

    These two are deployed independently, so a format drift would silently
    mean manual autorandr changes are never delivered.
    """

    def test_hook_writes_the_expected_keys(self) -> None:
        """The reader's format assumption, checked against the hook."""
        text = HOOK.read_text(encoding="utf-8")
        assert "printf 'profile=%s\\nmonitors=%s\\n'" in text

    def test_reader_parses_what_the_hook_produces(self, tmp_path: Path) -> None:
        """Run the hook's own printf and parse its real output.

        Asserting against a hand-written sample would pass even if the hook
        changed; this executes the format string from the hook itself.
        """
        text = HOOK.read_text(encoding="utf-8")
        assert "printf 'profile=%s\\nmonitors=%s\\n'" in text

        produced = subprocess.run(
            [  # noqa: S607
                "sh",
                "-c",
                'printf \'profile=%s\\nmonitors=%s\\n\' "$1" "$2"',
                "sh",
                "celtic+Samsung-Odyssey-G75F",
                "DP-9:eDP",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout

        path = tmp_path / "autorandr-postswitch"
        path.write_text(produced)
        notification = read_notification(path)
        assert notification is not None
        assert notification.profile == "celtic+Samsung-Odyssey-G75F"
        assert notification.monitors == ("DP-9", "eDP")

    def test_hook_refuses_to_write_multiline_evidence(self) -> None:
        """The hook guards against newlines in the profile name.

        Without that guard a crafted profile could inject extra keys into the
        record, so the reader's control-character check has a counterpart on
        the writing side.
        """
        text = HOOK.read_text(encoding="utf-8")
        assert "evidence is malformed" in text

---
name: kinesis-advantage2
description: Configures Adam's Kinesis Advantage2 keyboard (USB 29ea:0102, SmartSet firmware with a 2MB v-drive), covering its firmware remaps in active/qwerty.txt, thumb key modes, hotkey layouts, Progm shortcuts, and the keyd config that must match whatever the firmware sends. Use when remapping, diagnosing or backing up keys on the Advantage2; when editing its qwerty.txt or state.txt; when opening its v-drive with Progm+F1 or the "KINESIS KB" drive appears or gives I/O errors; or when its keys come out doubled, reversed or wrong after a firmware or keyd change. Not for the Advantage 360 or Kinesis Essential, which have their own keyd configs.
---

# Kinesis Advantage2

## Where things live

Paths below are in the desktop-config repository
(`~/.GIT/adamspiers.org/desktop-config`), which stows this skill into
`~/.agents/skills/`.

- **Firmware remaps**: `active/qwerty.txt` on the keyboard's v-drive.
  The repo copy, [`.keymaps/kinesis-advantage2/active/`][backup], is the
  record of what the keyboard should hold: read it rather than assuming.
- **Linux-only remaps**: [`.config/keyd/daemon/kinesis-advantage.conf`][keyd].
- **Strategy**: a remap that is the same on every OS goes in firmware; keyd
  carries only Linux-specific behaviour. See "Strategy: remap in firmware
  first" in [`specs/mac-keyboard-window-parity.md`][spec]. When a remap
  moves into firmware, drop it from keyd in the same commit, or the key is
  remapped twice (Up/Down then come out reversed, Caps Lock types `£`).

[backup]: ../../../.keymaps/kinesis-advantage2/active/
[keyd]: ../../../.config/keyd/daemon/kinesis-advantage.conf
[spec]: ../../../specs/mac-keyboard-window-parity.md

## How the firmware is organised

- A **layout** is a text file in `active/` listing only differences from
  the factory layout: `qwerty.txt`, `dvorak.txt`, and hotkey layouts named
  `<key>_qwerty.txt` (created with Progm+F2 then a key, loaded with
  Progm+that key). Each has a top layer and a keypad layer.
- A remap line is `[location]>[action]`, CRLF line endings, one per line.
  The left token is a physical key, the right one what it sends; lines do
  not chain. Tokens are in the manual's appendix (section 13.1).
- **Thumb key modes** (Windows, the default; PC; Mac) are remap lines the
  keyboard writes into the active layout. Progm+F5/F6/F7 rewrites the ten
  mode keys (left Ctrl, left Alt, right Win, right Ctrl, Scroll Lock, and
  their keypad twins) and **erases custom remaps on them**, saved to the
  file. `qwerty.txt` has `[lalt]>[delete]`, so never switch mode on it.
  The keyboard does not detect the OS.
- `state.txt` holds global settings (`startup_file=`, tones, clicks,
  `power_user=`). `version.txt` is informational; `do_not_edit.txt` is the
  firmware's scratch file.

Shortcuts: [references/progm-shortcuts.md](references/progm-shortcuts.md).

## Changing firmware remaps

The user drives the keyboard; the agent edits files.

1. The user turns on Power User Mode if needed (Progm+Shift+Esc) and opens
   the v-drive with Progm+F1. It appears as a 2MB disk labelled
   `KINESIS KB`, and the keyboard re-enumerates as `29ea:0103`, which keyd
   also matches.
2. With the user's approval, mount it the normal way,
   `udisksctl mount -b /dev/<dev>`. Check the label first with `lsblk`.
3. `cmp` each file with the repo backup before changing anything. A
   mismatch means someone edited the keyboard since; show it and ask.
4. Edit, `sync`, `udisksctl unmount`. The user closes the drive with
   Progm+F1, which makes the keyboard reload its layout.
5. Update the repo backup and keyd together, commit, and ask the user to
   test each changed key.

**Raw device access needs explicit approval per command.** `dd`, `fsck`
or any read or write of the block device itself is never implied by
"fix it" or by approval to mount. For a write, give the user a guarded
command where the check is a test that blocks the write, not a printout
before it. If `active/` gives I/O errors, follow
[references/recovery.md](references/recovery.md).

## Applying keyd changes

- Use `sudo systemctl restart keyd`, not `keyd reload`: keyd 2.6.0
  segfaulted on reload and left every keyboard unremapped (bead `dc-5ixn`).
- Then `systemctl --user restart fievel-keyd keyd-application-mapper`:
  fievel does not reattach to keyd's new virtual keyboard (bead `dc-rll9`),
  and the mapper's bindings live in the daemon.

## Finding out what the firmware sends

- keyd grabs the keyboard, so `keyd monitor` shows only keyd's output
  (re-emitted by the `fievel keyboard` device). Work out the raw codes by
  inverting the current keyd config. A capture records everything the user
  types on every keyboard: say so, and delete the log afterwards.
- **Status report**: Progm+Esc types firmware version, active layout,
  thumb mode and the number of remapped keys. The user must run it in an
  empty editor buffer: in an agent's prompt box its first Enter submits.
- Two physical keys sending one code cannot be told apart by keyd; that
  needs a firmware remap.

## v-drive quirks

- It is an Atmel AT45DB DataFlash read through the keyboard's
  microcontroller: about 40KB/s, so reading all 2MB takes 25-50 s.
- A Linux read-write mount sets the FAT dirty bit; on a FAT error the
  kernel remounts it read-only. Kinesis recommends Windows scan-and-repair
  for 2MB drives.
- The root also holds `Advantage2 SmartSet App for Windows (v1.2.3d).exe`,
  which the user keeps (a copy is in `~/software/kinesis/`). Reformatting
  deletes it.

Manual: [Advantage2 User's Manual][manual].

[manual]: https://kinesis-ergo.com/wp-content/uploads/Adv2-Users-Manual-01-07-19_us.pdf

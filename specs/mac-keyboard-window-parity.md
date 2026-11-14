# Keyboard and window management parity: Linux, macOS, Windows

Status: **draft for review**. Tracked by bead `dc-ql65`.

This spec states what Adam's Linux desktop does with the keyboard and
windows, independent of any tool, so the same behaviour can be built on a
MacBook (Karabiner-Elements plus AeroSpace) and on Windows (AutoHotkey).
Everything in sections 1 to 4 is extracted from the Linux config in this
repository; the source of each part is named. Section 5 holds
implementation proposals, which are unchecked. Section 6 lists open
questions.

## Terms

- **Thumb**: the window-management modifier, pressed with the right thumb.
  Linux: `Super` (`Mod4`). Windows: the Windows key. macOS: right
  `command`, turned into a Karabiner layer that neither apps nor macOS see
  as Command (section 5.1).
- **Thumb+x**: hold Thumb, press `x`.
- **Physical key**: the key as labelled on a standard PC laptop keyboard,
  before any remapping.

## Priorities

1. **Thumb+letter jumps straight to an app, or to a part of an app**, on
   every platform. Must have. Adam's decision (2026-11-14): exact window
   positions and sizes may be given up for a tiling window manager, but
   only if this holds.
2. The **same physical keys type the same letters** on each keyboard as
   under Linux, including the left-hand column shift (section 2.1).
3. **PC-style editing shortcuts** on macOS: `Ctrl+C` / `Ctrl+V` copy and
   paste, `Ctrl+Left` / `Ctrl+Right` move by word.
4. The window management operations in section 3.2, mapped to the tiling
   window manager's nearest equivalents.

## Strategy: remap in firmware first

Adam's rule (2026-11-14): **a remapping that is the same on every
operating system belongs in the keyboard's firmware**, wherever the
keyboard can be programmed. It then works on Linux, macOS and Windows with
no software, and each OS's tool (keyd, Karabiner-Elements, AutoHotkey) only
has to carry what really differs between OSes, such as app jumps, PC-style
editing on macOS, or the left-hand alpha shift on a laptop's built-in
keyboard.

Examples on the Kinesis Advantage2: swapping Up and Down, and Caps Lock
typing `-`. These must be done in firmware, not in keyd.

So for each requirement in section 2, the implementation order is:

1. Keyboard firmware, if the keyboard is programmable and the behaviour is
   the same on every OS.
2. Otherwise the per-OS tool, matched to the device where the tool allows
   it.

When a firmware change moves a remap out of software, the software config
must drop it in the same step, or the key is remapped twice.

## 1. Keyboards

Remapping lives in three layers, and only the first travels with the
keyboard:

- **Firmware**: the keyboard sends already-remapped keys. Works on any OS.
- **keyd** (Linux, per device): `.config/keyd/daemon/*.conf` and
  `partials/`, matched by USB ID.
- **keyd per-app rules**: `.config/keyd/app.conf`.

The XKB symbols in `.xkb/symbols/adam*` describe the same remaps and were
the previous mechanism; `.config/keyd/daemon/default.conf` says keyd is
"equivalent to Adam's XKB setup".

| Keyboard | Remapping lives in | Right-thumb key (Thumb) | OS-level extras on Linux |
| --- | --- | --- | --- |
| Laptop built-ins (Framework, ThinkPad, Dell, Lenovo, Samsung) | keyd | Right Alt (Dell: Right Ctrl; Lenovo, Samsung: Menu) | Everything in 2.1 to 2.3 |
| Keyboardio Atreus, Model 01, Preonic | Firmware (Chrysalis) | Firmware | UK shifted symbols, `£` / `€` (2.3) |
| Kinesis Advantage 360 | Mostly firmware | Firmware | Home = AltGr, physical Win = Compose, Caps Lock = Insert, `£` / `€` |
| Kinesis Advantage2 (USB) | Firmware (v-drive `qwerty.txt`) for OS-independent remaps, keyd for the rest | Right Ctrl on the thumb pad | Firmware: Caps Lock = `-`, Up/Down swapped, End = Esc, thumb Delete/Alt swapped, top-right key swap, International key = Insert; backed up in `.keymaps/kinesis-advantage2/active/`. keyd: `£` / `€` on the top-right key, thumb pad layers |
| Kinesis Essential (PS/2) | keyd (not programmable) | Right Ctrl on the thumb pad | Thumb pads (2.4), Caps Lock = `-`, Up/Down swapped, `-` key types `£` / `€` |
| Gyration | keyd | Not remapped | Own variant of 2.1 |

With the Mac (Adam, 2026-11-14): the built-in keyboard, the Kinesis
Advantage2 and Advantage 360, and the Keyboardio boards. The external ones
do their OS-independent remaps in firmware, so on macOS most device rules
are for the built-in keyboard.

## 2. Key remapping

### 2.1 Left-hand alpha shift (laptop and standard keyboards)

The left-hand letters move one column left, so the left hand rests one key
further left than usual. Source: keyd partials `left-alpha-common`,
`standard-qaz`, `xcv2`.

| Physical key | Types |
| --- | --- |
| `` ` `` (left of `1`) | `q` |
| `Q` `W` `E` `R` `T` | `w` `e` `r` `t` `` ` `` / `~` |
| Caps Lock | `a` |
| `A` `S` `D` `F` `G` | `s` `d` `f` `g` `\` / `\|` |
| Left Ctrl (MacBook: `fn`, see 2.2) | `z` |
| ISO key left of `Z` (`102nd`) | `x` |
| `Z` `X` | `c` `v` |
| `C` `V` `B` | unchanged: `c` `v` `b` |

Tab, Left Shift and every right-hand key are unchanged. One "hacked" US
keyboard uses a variant: `x` on Left Shift (`xcv3`). (The HHKB Lite has its
own variant in keyd but is no longer used.)

A keyboard with no ISO key left of `Z` (US/ANSI layout) has no home for
`x` under this scheme. The MacBook has one: a `` ` `` / `~` key left of
`Z`, before Left Shift (Adam, 2026-11-14), which becomes `x`.

### 2.2 Modifiers (laptop-style keyboards)

Source: keyd partials `laptop-left-mods`, `right-alt-super-right`,
`right-ctrl-menu`, `right-ctrl-compose-control-meta`.

| Physical key | Role |
| --- | --- |
| Left Alt (left of Space) | Control |
| Left Win | Alt (Emacs Meta) |
| Right Alt (right of Space) | **Thumb** (`Super`) |
| Right Ctrl | Compose. Framework: Compose on tap, Control+Super while held |
| Left Ctrl, Caps Lock | Letters (2.1) |

So under Linux, **Control is the key immediately left of Space**, pressed
with the left thumb, and Thumb is the key immediately right of Space.

On a MacBook the bottom row is `fn` `control` `option` `command` Space
`command` `option`; on the Framework it is Ctrl, Fn, Win, Alt, Space,
Alt, Ctrl. By position (Adam, 2026-11-14):

| MacBook key | Linux key in that position | Role |
| --- | --- | --- |
| `fn` (far left) | Left Ctrl | `z` (2.1) |
| Left `control` | Fn | Fn |
| Left `option` | Left Win | Option, macOS's Alt key: Meta in Emacs and terminals. Option+Tab is left alone; window cycling is Thumb+`/` as on Linux (3.2) |
| Left `command` | Left Alt | Control's position: Command in ordinary apps, so `Cmd+C` copies; Control in the exception apps (2.5, 5.2) |
| Right `command` | Right Alt | **Thumb** (5.1) |
| Right `option` | Right Ctrl | The opposite of left `command` (Adam, 2026-11-14, approach A2 in 5.2): Control in ordinary apps, Command in the exception apps. macOS has no Compose; left `option` combinations type special characters instead |

So in every app exactly one key is Command and one is Control. Adam's
rules (2026-11-14): "There should never be any combination where there
are two command keys", and Control and Command must always both be
available.

Option, not Command, is the counterpart of Alt: a Mac keyboard's Option
key sends the same USB code as Alt, the Advantage2 manual calls them the
same action, and macOS terminals and Emacs use Option as Meta. Command
sends the Windows key's code, but in apps it plays Ctrl's part (`Cmd+C`)
and `Cmd+Tab` is the app switcher.

### 2.3 Characters

Source: keyd partials `gb`, `gb-3`, `backslash-hash-tilde`,
`minus-sterling-euro`, `backslash-currency`; `.config/keyd/app.conf`.

The base layout is US, with UK changes:

- `Shift+2` types `"` and `Shift+'` types `@`.
- `Shift+3` types `£`; `Ctrl+Shift+3` types `€` (Framework). Keyboards
  without a spare key get `£` / `€` on another key (Kinesis: the `-` key or
  top-right key).
- The `\` key types `#`, and `~` with Shift (laptops).
- Compose (2.2) types other characters.
- In Emacs, which ignores Compose, the `£` / `€` keys send Emacs's own
  `C-x 8 L` / `C-x 8 E` instead.

### 2.4 Kinesis thumb pads

Source: keyd partials `kinesis-left-thumb-pad`, `kinesis-right-thumb-pad`.

- Left pad: Delete key = Alt, Alt key = Delete, Home key = AltGr (with
  Thumb, Home), End key = Esc (with Thumb, End).
- Right pad: Right Ctrl = **Thumb**, Right Alt = Compose.

### 2.5 PC-style editing on macOS

Required (Adam, 2026-11-14):

- `Ctrl+C` / `Ctrl+V`: copy, paste.
- `Ctrl+Left` / `Ctrl+Right`: move back / forward one word.

"Ctrl" here is the key in Linux's Control position (2.2). These need
explicit work on macOS, where copy is `Cmd+C` and word movement is
`Option+Left`.

Also required (Adam, 2026-11-14):

- the other common `Ctrl+`letter shortcuts: `X`, `Z`, `A`, `S`, `F`, `W`,
  `T`, `N` and similar, as their `Cmd` equivalents;
- `Ctrl+Shift+Left` / `Ctrl+Shift+Right`: select by word;
- `Home` / `End`: line start / end; `Ctrl+Home` / `Ctrl+End`: document
  start / end.

Not wanted: `Ctrl+Backspace` / `Ctrl+Delete` word delete.

macOS has no Control-based copy and paste at all: in most apps `Ctrl+C`
does nothing and `Ctrl+V` pages down, so these are translations, not
existing behaviour.

**Exceptions**, where Control stays Control because `Ctrl+C` means
interrupt and `Ctrl+Left` is the app's own binding (Adam, 2026-11-14):
terminal emulators, Emacs, VM and remote desktop windows, and Orca's
terminals.

### 2.6 Per-app key bindings

Source: `.config/keyd/app.conf`.

- Emacs: currency keys as in 2.3.
- Orca: `Alt+Shift+Up` / `Alt+Shift+Down` cycle between agents waiting for
  attention; `Ctrl+Alt+o` opens a token picker. Needed on macOS too.

Emacs and Orca are both used on the work Mac (Adam, 2026-11-14).

## 3. Window management

Source: `.fluxbox/keys.erb`, as rendered on `celtic` and listed by
`.config/bindings-help.d/fluxbox`.

### 3.1 App jumps (must have)

Behaviour of a jump binding:

- If the app is running, focus its window, switching workspace or monitor
  as needed. If not, launch it.
- **Toggle** jumps (marked T) hide or minimise the window when it already
  has focus.
- **Cycle** jumps (marked C) go to the next matching window each press.
- A jump can target **part of an app**: a browser tab by URL, an Emacs
  command, a Teams chat.

Thumb+letter, Linux and the agreed Mac / Windows targets:

| Key | Linux | macOS / Windows |
| --- | --- | --- |
| `a` | ChatGPT / Claude desktop window (C) | same |
| `b` | Browser windows on the main monitor (C) | same |
| `c` | Orca (T) | same |
| `d` | Emacs: today's org agenda | same |
| `e` | Emacs (T) | same |
| `f` | Firefox | same |
| `g` | Telegram | same |
| `h` | Picker of GitHub tabs in Chrome | same |
| `j` | Jump menu | unbound for now |
| `k` | Emacs: `org-capture` | same |
| `m` | Mail menu (`rofi-mail-menu`) | **Outlook mail** |
| `n` | Emacs: personal note | same |
| `o` | Org menu (`rofi-org-menu`) | **Teams Planner** (focus or launch) |
| `p` | Volume control (`pavucontrol`) (T) | Sound settings |
| `s` | **Slack** | **Slack** |
| `t` | Terminal menu (`rofi-terminal-menu`) | **Teams chat** (Linux has no Teams); no terminal menu on the Mac |
| `u` | Bluetooth TUI (T) | Bluetooth settings |
| `v` | Emacs: daily review agenda | same |
| `w` | Wi-Fi menu | Wi-Fi menu |
| `x` | Terminal windows (C) | same |
| `y` | Picker of YouTube tabs | same |
| `z` | Zulip | same |
| `` ` `` | `btop` (T) | Activity Monitor |
| `'` | Beeper | same |

Thumb+modifier+letter jumps:

| Keys | Linux | macOS / Windows |
| --- | --- | --- |
| Thumb+Shift+`a` | Anytype | same |
| Thumb+Ctrl+`a` | Spotify web app (T) | same |
| Thumb+Ctrl+`b` | "Bead Me Up, Scotty" web app (T) | same |
| Thumb+Shift+`c` | Google Calendar tab, account 0 | same (personal Google Calendar) |
| Thumb+Ctrl+`c` | Google Calendar tab, account 1 | **Outlook calendar** |
| Thumb+Ctrl+`d` | Discord | same |
| Thumb+Shift+`e` | Picker of Linear tabs | same |
| Thumb+Ctrl+`f` | Picker of Messenger tabs | same |
| Thumb+Shift+`g` / Thumb+Ctrl+`g` | Google Meet tab / Meet window | same |
| Thumb+Ctrl+`h` | Octobox web app (T) | same |
| Thumb+Ctrl+`k` | Capture a todo from Slack | same |
| Thumb+Alt+`n` / Thumb+Shift+`n` | Notion app / picker of Notion tabs | same |
| Thumb+Shift+`r` | Reclaim tab | same |
| Thumb+Ctrl+`t` | (none) | **Teams meeting window** |
| Thumb+Alt+`w` | WhatsApp tab | same |
| Thumb+Ctrl+`y` | Syncthing web UI | unbound |
| Thumb+Shift+`z` / Thumb+Ctrl+`z` | Picker of Zoom tabs / Zoom meeting window | same |
| Thumb+Shift+`` ` `` | `ccmonitor` (T) | unbound |
| Thumb+Shift+`p` | EasyEffects (T) | unbound (Linux-only tool) |
| Thumb+Shift+`u` | Bluetooth manager (T) | unbound (Linux-only tool) |
| Thumb+Ctrl+`2` / `5` / `7` | Emacs: personal todo / Toucan todo / weekly review | same |

Mac / Windows targets were agreed with Adam on 2026-11-14. "same" means
the Linux target, bound where that app is installed.

### 3.2 Window operations

| Keys | Action |
| --- | --- |
| Thumb+Left / Right / Up / Down | Focus the window in that direction |
| Thumb+Shift+arrows or `h` `j` `k` `l` | Move window 10 px (with Alt: 50 px) |
| Thumb+`/` | Cycle windows on this monitor and workspace (Shift: reverse; Ctrl: include minimised) |
| Thumb+Alt+Return | Window switcher (search by title) |
| Thumb+`1` .. `6` | Go to workspace 1 to 6 |
| Thumb+`,` / `.` | Previous / next workspace |
| Thumb+Alt+`1` .. `6` | Take window to workspace 1 to 6 |
| Thumb+Alt+`,` / `.` | Take window to previous / next workspace |
| Thumb+Shift+`,` / `.` | Move focus to the monitor on the left / right |
| Thumb+Alt+Shift+`,` / `.` | Move window to the monitor on the left / right |
| Thumb+Alt+Shift+`1` / `2` / `3` | Move window to monitor 1 / 2 / 3 |
| Thumb+Space | Raise |
| Thumb+`l` | Lower |
| Thumb+`i` / Thumb+Ctrl+`i` | Minimise / restore |
| Thumb+Ctrl+Shift+`m` | Maximise (Shift+`m`: vertically; Ctrl+`m`: horizontally) |
| Thumb+Shift+`s` | Make window sticky (on all workspaces) |
| Thumb+`q` / Thumb+Ctrl+`q` | Close / kill window |
| Thumb+Ctrl+`p` | Pass-through mode: window-manager keys go to the app until `q` |
| Thumb+`r` | Reorganise mode (below) |

**Reorganise mode** (Thumb+`r`, leave with `q`): `h` `j` `k` `l` focus,
arrows move, Ctrl+arrows resize, `m` maximise, and number keys snap the
window to a preset: `1` left half, `2` right half, `3` top, `4` bottom,
`5` .. `8` quarters, `0` two columns left, `9` right third, Shift+`2` two
columns, Shift+`;` two rows. On a tiling window manager this maps to its
own layout commands.

Mouse: Alt+drag moves a window, Alt+right-drag resizes it, the scroll
wheel on the desktop switches workspace.

### 3.3 Layout intent

Source: `.fluxbox/layouts/` (one file per monitor setup, rules in
`common/`).

- **Workspace 1**: main work. Terminals in the left column of the main
  monitor, Emacs in the right column, Orca, browser.
- **Workspace 2**: browser windows for side projects and Toucan.
- **Workspace 3**: Firefox, `gitk`, Notion, LM Studio.
- **Workspace 4**: communication: Slack, Telegram, Discord, Beeper, Zulip.
- **Workspace 5**: personal browser window.
- **Workspace 6**: monitoring (`btop` and friends), Kraken, Chrysalis.
- **Secondary monitor**: windows there are sticky, shown on every
  workspace: secondary browser windows, volume and Bluetooth tools,
  monitoring.
- **Pop-ups** (pickers, Bluetooth TUI, mail sync): floating, centred, 90%
  of the screen.
- With an external monitor attached, it is the main monitor and the laptop
  screen is secondary.

Exact positions and sizes are negotiable (Priorities). Which monitor and
workspace an app lands on is the part to keep.

### 3.4 Pointer control from the keyboard

Source: `.config/bindings-help.d/fievel.tsv`, `neru.tsv`.

- **fievel**: hold `d`+`f` (or F3) for free mouse mode: `j` `k` `i` `l`
  move, Space left-click, `;` right-click, `n` `m` `,` `.` scroll.
- **neru**: Thumb+`-` grid to move the pointer, Thumb+`;` hints for click
  targets in the focused window, hold `e`+`r` for a recursive grid.

Both need IT approval on the work Mac; Karabiner is assumed allowed.

### 3.5 Other global bindings

| Keys | Action |
| --- | --- |
| Thumb+`[` / `]` / `0` | Volume down / up / mute |
| Thumb+Shift+`[` / `]` | Brightness down / up |
| Thumb+Ctrl+`s` / `w` | Screenshot of a region / window |
| Thumb+Ctrl+Space | App launcher |
| Thumb+Ctrl+Alt+`l` | Lock screen |
| Thumb+`=` | Clear notifications |
| Thumb+Alt+Shift+`/` | Search every key binding |
| Thumb+Ctrl+`8` | Push-to-talk recording (dictation) |

## 4. Windows

The AutoHotkey script covers part of this spec, with the Windows key as
Thumb. It exists only on the Windows PC (Adam, 2026-11-14), so it is not
compared here until a copy is brought over.

## 5. macOS implementation

Built on branch `mac-karabiner-aerospace-configs`, not yet run on macOS.
Section 7 says how to install and test it.

### 5.1 Thumb

Right `command` must become a modifier no app sees as Command, otherwise
Thumb+`c` would also copy. Options:

- **Chosen**: Karabiner turns right `command` into a layer whose bindings
  run commands directly (`open -a`, the `aerospace` CLI), so neither apps
  nor macOS see a key combo.
- Rejected: Karabiner turns it into Hyper (Cmd+Option+Ctrl+Shift) and
  AeroSpace binds Hyper combos. macOS reserves Hyper+`.` for a system
  diagnostic, reportedly with no way to override it ([Keyboard Maestro
  forum][km-sysdiagnose]), and an old remapping recipe also blocks
  Hyper+`,` and Hyper+`/` ([gist][hyper-gist]). Thumb+`.`, Thumb+`,` and
  Thumb+`/` are core bindings (3.2), and Adam needs Thumb+`.` (2026-11-14).

[km-sysdiagnose]: https://forum.keyboardmaestro.com/t/dont-try-to-use-for-a-macro-hot-key/14101
[hyper-gist]: https://gist.github.com/mutewinter/6847308

### 5.2 Editing (2.5)

Two ways, given that left `command` sits in Linux's Control position:

- **A**: keep left `command` as Command. `Cmd+C` already copies, with the
  same finger movement as Linux `Ctrl+C`. Remap word movement to
  `Option+arrows`, and turn left `command` into Control in the exception
  apps.
- **B**: make left `command` Control everywhere, then translate
  `Ctrl+C` / `Ctrl+V` etc. to Command combos outside the exception apps.

A needs fewer rules in ordinary apps. Both use the exception list in 2.5.

**Chosen: A2** (Adam, 2026-11-14: "OK A2 with Orca as an exception").
Approach A as first built left ordinary apps with two Command keys (left
`command` and right `option`) and no Control, which breaks the rule in
2.2. A2 is A with right `option` swapped to match:

| MacBook key | Ordinary apps | Exception apps (2.5, Orca included) |
| --- | --- | --- |
| `fn` | `z` | `z` |
| Left `control` | Fn | Fn |
| Left `option` | Option | Option |
| Left `command` | Command | Control |
| Right `command` | Thumb | Thumb |
| Right `option` | Control | Command |

Rejected (Adam, 2026-11-14):

- **B**: translating `Ctrl+` shortcuts needs too many rules for Teams,
  Outlook, browsers and AI clients.
- **A'**: keeping a second Command key duplicates Command, which the rule
  in 2.2 forbids.
- **A chord to swap the two keys' roles**: Fn hurts the left little finger
  (RSI), and Option clashes with Emacs `C-M-` bindings and Orca's
  `Ctrl+Alt+o`.

In ordinary apps every `Cmd`
shortcut then works from the Control position without a rule, including
ones the list in 2.5 leaves out (`Cmd+L`, `Cmd+R`, `Cmd+click`). B would
need a rule per letter and would still miss mouse and scroll combinations.
A needs rules only where the Mac differs from Linux:

- `Ctrl+Left` / `Ctrl+Right`, with or without Shift → `Option+arrows`;
- `Home` / `End` → `Cmd+Left` / `Cmd+Right`; `Ctrl+Home` / `Ctrl+End` →
  `Cmd+Up` / `Cmd+Down`;
- `Ctrl+Tab` → `Control+Tab`, so it switches tabs as on Linux rather than
  apps;
- right `option`+Tab, which A2 makes `Control+Tab` in ordinary apps, →
  `Cmd+Tab` (Adam, 2026-11-14), so the two are swapped and the app
  switcher stays on the Compose-position key, as in the exception apps.
  Unchecked: whether the switcher stays open while right `option` is
  held and Tab pressed again, or Karabiner releases Command after each
  Tab.

The implementation is `bin/karabiner-gen-config`, which writes
`.config/karabiner/karabiner.json`. Terminals and Orca also get
`Ctrl+Shift+C` / `Ctrl+Shift+V` as copy and paste, as on Linux.

**Orca**: Karabiner's conditions see only the frontmost app, not the
pane inside it ([conditions][ke-conditions]). Karabiner 16.1.94 and later
expose the focused element's role, subrole, title and window title as
variables, but Orca's editor and terminal panes are both Chromium text
areas, so whether those values differ is unknown until checked on the
Mac. Until then all of Orca keeps Control as Control: an interrupt sent
as `Cmd+C` would silently fail to stop an agent, which is worse than
copying with Command in Orca's editor panes. Follow-up: bead `dc-ql65.6` (Check whether Karabiner can tell Orca's terminal panes from its editor panes).

[ke-conditions]: https://karabiner-elements.pqrs.org/docs/json/complex-modifications-manipulator-definition/conditions/

### 5.5 Bottom row

Karabiner treats `fn` as an ordinary key code that it can remap and send
(maintainer, [pqrs-org/Karabiner-Elements#3940][ke-3940]), so the built-in
`fn` types `z` and left `control` sends `fn`. Unchecked on the Mac:

- `fn`+arrows giving Home/End/Page Up/Page Down from the new Fn key.
  Karabiner's docs say macOS does this itself "if the virtual device has
  Apple's Vendor ID and Product ID" ([breaking changes][ke-breaking]).
- The Globe action: [#2866][ke-2866] reports a stray `fn` key-up that
  opens the emoji picker. If it happens, set System Settings > Keyboard >
  "Press Globe key to" to "Do Nothing".

[ke-3940]: https://github.com/pqrs-org/Karabiner-Elements/issues/3940
[ke-breaking]: https://karabiner-elements.pqrs.org/docs/help/troubleshooting/breaking-changes/
[ke-2866]: https://github.com/pqrs-org/Karabiner-Elements/issues/2866

### 5.6 Keyboards in Karabiner

Profile `devices[]` entries cannot match the built-in keyboard without
its USB IDs, which differ by model, but the `device_if` condition can,
with `is_built_in_keyboard` ([device conditions][ke-device]). So every
remap is a complex rule, and the Thumb bindings for letters list the
built-in keyboard's physical key separately: Karabiner applies only the
first matching complex rule and does not feed its output to later ones
([evaluation priority][ke-priority]).

Thumb on each keyboard, worked out from this repository:

| Keyboard | Thumb key sends | Evidence |
| --- | --- | --- |
| MacBook built-in | right `command` | Spec 2.2 |
| Kinesis Advantage2 | right Control | `kinesis-right-thumb-pad`: `rightcontrol = layer(meta)`; firmware leaves it alone |
| Kinesis Advantage 360 | a GUI key, side unknown | keyd leaves meta alone, so any GUI key is Super on Linux |
| Keyboardio Atreus | left GUI, from Space held | `.config/chrysalis/atreus.json`: the right-thumb Space is a dual-use key, code 49981 (`0xC311` + Space; Kaleidoscope dual-use index 3 is left GUI, unchecked) |
| Keyboardio Model 01 | right GUI, on the right thumb arc | Adam's `Model01-Firmware` fork, `aspiers` branch: the 2020 Chrysalis backup (`layer-0.json`, commit `8b4b6cd`) and the older sketch agree |
| Keyboardio Preonic | a GUI key, side unknown | keyd leaves meta alone; no firmware export found |

So right `command` is Thumb on every keyboard, and left `command` is
Thumb too on the 360 and the Atreus (matched by the USB IDs in their keyd
configs). The Preonic's keyd ID is not a USB ID, so only its right GUI
key works. On the Kinesis boards, the Compose key (right Alt)
acts as right `option` does on the MacBook: Control in ordinary apps,
Command in the exception apps (5.2). So the Kinesis boards also have
exactly one Command and one Control in every app. Each is a single line
in `thumb_trigger_rule` or `modifier_rules`.

**The Atreus and the Model 01 are different keyboards** (Adam,
2026-11-14), and each needs its own rules:

- **Atreus**: 44 keys. Each half's bottom row has five keys, plus one
  centre key per side. On the base layer the modifiers are all holds or
  left-side codes: Esc, Tab and Enter are Shift when held, Backspace is
  Alt when held, Space is left GUI (Thumb) when held, and one key is
  Ctrl.
- **Model 01**: separate halves, each with a four-key thumb arc and a
  palm key. Per the 2020 backup, the left arc sends Backspace, left
  Control, left Alt and Esc, the key beside it a second left Alt, and the
  left palm key left Shift. The right arc sends right GUI (Thumb), Space,
  Page Up and Page Down, and the right palm key holds layer 2.

Neither board sent right Control, right Alt or Compose from any key, so
neither had a Compose-position key for A2's second role: under A2 each
had no Control in ordinary apps and no Command in the exception apps.
Each needs a firmware change so that one key sends a right-side code
Karabiner can give that role:

- **Atreus**: the Ctrl key on layer 4 (held `-` or `[`) sends right
  Control (Adam, 2026-11-14: "We can put it on layer 4 Ctrl for now").
  Karabiner leaves it Control in ordinary apps and makes it Command in
  the exception apps. The change is in `.config/chrysalis/atreus.json`
  and takes effect once that layout is loaded into the keyboard with
  Chrysalis.
- **Model 01**: undecided.

The key codes Karabiner reports for the MacBook's ISO keys (§ left of
`1`, `` ` `` left of `Z`) are unchecked: its ISO handling has swapped
them before ([#3974][ke-3974]). The generator assumes
`grave_accent_and_tilde` and `non_us_backslash`, as on Linux.

[ke-device]: https://karabiner-elements.pqrs.org/docs/json/complex-modifications-manipulator-definition/conditions/device/
[ke-priority]: https://karabiner-elements.pqrs.org/docs/json/complex-modifications-manipulator-evaluation-priority/
[ke-3974]: https://github.com/pqrs-org/Karabiner-Elements/issues/3974

### 5.3 App jumps

Built as Karabiner rules (`bin/karabiner-gen-config`, `jumps()`) and
`bin/mac-jump`:

- **Focus or launch**: `open -b <bundle ID>`. AeroSpace switches to the
  workspace of whichever window macOS focuses ([guide][as-guide],
  "Emulation of virtual workspaces"); that `open` takes this path is
  suspected, unchecked.
- **Toggle** (T): when the app is frontmost, Karabiner sends `Cmd+H`
  instead, decided by its frontmost-app condition. Hide stands in for
  minimise because AeroSpace cannot focus a minimised window again
  ([#18][as-18]).
- **Cycle** (C): `mac-jump cycle` focuses the next window of the app
  with `aerospace focus --window-id`. Thumb+`b` cycles every Chrome
  window, not only those on the main monitor.
- **Chrome tabs**: `mac-jump tab` finds tabs by URL through Chrome's
  AppleScript dictionary and picks among several with AppleScript's
  `choose from list`, which has no typed filter like rofi's. Web apps
  (Spotify, Octobox, "Bead Me Up, Scotty") open as `--app` windows.
- **Emacs**: `emacsclient -e`, as `switch-emacs-eval` does on Linux.
- **Outlook** mail and calendar: activate Outlook, then `Cmd+1` /
  `Cmd+2` through System Events, which needs the Accessibility permission.
  **Teams chat** does the same with `Cmd+2`, assuming Chat is second in
  Teams' app bar.

Stubs and gaps, all unchecked on the Mac:

- Thumb+`o` **Teams Planner** only focuses Teams: no reliable way found to
  open Planner inside it.
- Thumb+Ctrl+`t` **Teams meeting window** cycles all Teams windows.
- Unbound: Thumb+Ctrl+`k` (Slack todo capture, a Linux script), Thumb+`=`
  (clear notifications), Thumb+Alt+Shift+`/` (binding search),
  Thumb+Ctrl+`8` (dictation), Orca's `Ctrl+Alt+o` token picker (uses
  rofi), and pointer control (3.4, needs IT approval).
- Raise, Thumb+`l` (lower), Thumb+Ctrl+`i` (restore), Thumb+Shift+`s`
  (sticky) and most of the reorganise mode's number presets have no
  AeroSpace equivalent. The tiling operations that replace them are in
  5.7.

[as-guide]: https://nikitabobko.github.io/AeroSpace/guide
[as-18]: https://github.com/nikitabobko/AeroSpace/issues/18

### 5.4 Layouts

`.config/aerospace/aerospace.toml`, with no key bindings of its own:

- Workspaces 1 to 6 are force-assigned to the main monitor, and a
  workspace `S` to the secondary one ([guide][as-guide], "Assign
  workspaces to monitors"). Undocked, `S` matches no monitor, so AeroSpace
  shows it on the laptop screen like the others (read from AeroSpace's
  source, not tested).
- New windows go to the Linux workspace by app (`on-window-detected`).
  Chrome cannot be split between workspaces 1, 2 and 5 by app, so all its
  windows land on 1.
- **Sticky windows**: not supported ([#2][as-2]). What Linux keeps sticky
  on the secondary monitor lives on workspace `S` instead.
- **Pop-ups**: System Settings floats. AeroSpace cannot centre or size a
  floating window ([#494][as-494]), so pop-ups are not centred at 90%.

[as-2]: https://github.com/nikitabobko/AeroSpace/issues/2
[as-494]: https://github.com/nikitabobko/AeroSpace/issues/494

### 5.7 Thumb bindings for AeroSpace tiling (agreed, bead `dc-ql65.5`)

AeroSpace arranges each workspace as a tree of containers. Each container
is **tiles** (side by side) or **accordion** (stacked, one visible), and
horizontal or vertical. It has no pixel moves, raise/lower or sticky
windows (5.4). The proposal keeps every Linux key that has a
tiling meaning, reuses the keys whose Fluxbox meaning has none, and puts
the less common tiling operations in the reorganise mode, as on Linux.
Commands are from [AeroSpace's command reference][aerospace-commands].
Agreed with Adam on 2026-11-14; Thumb+Space was his choice ("maybe
toggle floating?"), so it is the first candidate to revisit.

**Direct Thumb bindings** (changes from 3.2 in bold):

| Keys | Linux (3.2) | AeroSpace |
| --- | --- | --- |
| Thumb+arrows | Focus in that direction | `focus <dir>` |
| Thumb+Shift+arrows or `h` `j` `k` `l` | Move window 10 px | `move <dir>`: move the window in the tree |
| Thumb+Alt+Shift+arrows or `h` `j` `k` `l` | Move window 50 px | **`join-with <dir>`**: group with the neighbour in a new container |
| Thumb+`/` (Shift: reverse) | Cycle windows on this monitor and workspace | `focus dfs-next` / `dfs-prev`, wrapping round the workspace |
| Thumb+Space | Raise | **`layout floating tiling`**: float or tile the window |
| Thumb+Ctrl+Shift+`m` | Maximise | `fullscreen` toggle |
| Thumb+`i` | Minimise | `macos-native-minimize` |
| Thumb+`q` / Thumb+Ctrl+`q` | Close / kill | `close` / `close --quit-if-last-window` |
| Thumb+`1` .. `6`, Thumb+`,` / `.` | Workspaces | `workspace <n>`, `workspace prev` / `next` |
| Thumb+Alt+`1` .. `6`, Thumb+Alt+`,` / `.` | Take window to workspace | `move-node-to-workspace <n>` / `prev` / `next` |
| Thumb+Shift+`,` / `.` | Focus monitor | `focus-monitor left` / `right` |
| Thumb+Alt+Shift+`,` / `.` | Move window to monitor | `move-node-to-monitor left` / `right` |
| Thumb+`r` | Reorganise mode | Karabiner variable `reorganise` (below) |
| Thumb+Ctrl+`p` | Pass-through mode | Thumb layer off until `q` (Karabiner variable) |

Unbound on the Mac: Thumb+`l` (lower), Thumb+Ctrl+`i` (restore minimised),
Thumb+Shift+`m` / Thumb+Ctrl+`m` (maximise one way), Thumb+Shift+`s`
(sticky).

**Reorganise mode** (Thumb+`r`; `q`, Esc or Space leaves):

| Keys | Linux reorganise mode | AeroSpace |
| --- | --- | --- |
| `h` `j` `k` `l` | Focus | `focus <dir>` |
| arrows | Move 10 px | `move <dir>` |
| Shift+arrows | Move 50 px | **`join-with <dir>`** |
| Ctrl+arrows | Resize 2% | `resize width` / `height` ±50 |
| Ctrl+Shift+arrows | Resize 10% | `resize width` / `height` ±200 |
| `m` | Maximise | `fullscreen` toggle |
| **`t`** | (none) | `layout tiles horizontal vertical`: tiles, or flip orientation |
| **`a`** | (none) | `layout accordion horizontal vertical`: accordion, or flip orientation |
| **`f`** | (none) | `layout floating tiling`: float or tile the window |
| **`b`** | (none) | `balance-sizes` |
| `0` | Two columns, left | **`flatten-workspace-tree`**: reset the workspace |
| Shift+`2` | Two columns | `layout --root h_tiles` |
| Shift+`;` | Two rows | `layout --root v_tiles` |
| Thumb+`,` / `.` etc. | Workspaces | as in main mode |

The other numbered presets (halves, quarters, thirds) have no tiling
equivalent and are dropped.

Built in `bin/karabiner-gen-config` (`window_operations()` and
`reorganise_rule()`). The mode is a Karabiner variable, like pass-through,
rather than an AeroSpace binding mode: Karabiner then sees the keys before
the alpha shift and the editing rules change them, and Thumb bindings
still work inside the mode. Command syntax checked against AeroSpace's
[command reference][aerospace-commands] and its
[`resize` man page source][aerospace-resize]; nothing has run on a Mac.

[aerospace-resize]: https://github.com/nikitabobko/AeroSpace/blob/main/docs/aerospace-resize.adoc

[aerospace-commands]: https://nikitabobko.github.io/AeroSpace/commands

## 6. Open questions

None. Adam chose editing approach A2 (5.2).

## 7. Installing and testing on the Mac

### Install

1. Install both apps, then grant the permissions each asks for (Karabiner:
   its driver extension and Input Monitoring; AeroSpace: Accessibility):

   ```sh
   brew install --cask karabiner-elements
   brew install --cask nikitabobko/tap/aerospace
   ```

2. Move aside the config directory Karabiner created on first launch, and
   any `~/.aerospace.toml`, which AeroSpace would report as ambiguous next
   to `~/.config/aerospace/aerospace.toml`:

   ```sh
   mv ~/.config/karabiner ~/.config/karabiner.orig
   ```

3. Stow this repository (`mr stow`, or `stow -t ~ .` in the repo).
4. Check that `~/.config/karabiner` is itself a symlink into the
   repository. Karabiner's docs say to link the directory, not
   `karabiner.json`, or it misses changes to the file
   ([configuration file path][ke-path]); its saves also replace the file
   by renaming, which would break a file link. If stow made a real
   directory of file links, replace it with one link and restart
   Karabiner's agent:

   ```sh
   rm ~/.config/karabiner/karabiner.json && rmdir ~/.config/karabiner
   ln -s ~/.STOW/desktop-config/.config/karabiner ~/.config/karabiner
   launchctl kickstart -k gui/$(id -u)/org.pqrs.service.agent.Karabiner-Console-User-Server
   ```

5. macOS settings:
   - Disable "Displays have separate Spaces", as AeroSpace recommends,
     then log out:
     `defaults write com.apple.spaces spans-displays -bool true && killall SystemUIServer`.
   - Arrange displays so each has a free bottom corner, where AeroSpace
     parks windows of hidden workspaces ([guide][as-guide]).
   - Make the external monitor the main display (the one with the menu
     bar) when docked.
   - The UK characters in 2.3 come from the input source, not Karabiner:
     "British – PC" should match Linux's shifted symbols (unchecked).

[ke-path]: https://karabiner-elements.pqrs.org/docs/manual/misc/configuration-file-path/

### Test

Karabiner-EventViewer (installed with Karabiner) shows key names and,
under Variables, whether `thumb` is set while right `command` is held.

1. **ISO keys**: press § (left of `1`) and `` ` `` (left of `Z`). If
   EventViewer names them the other way round from
   `grave_accent_and_tilde` and `non_us_backslash`, swap those two in
   `ALPHA_SHIFT` in `bin/karabiner-gen-config`.
2. **Letters**: in TextEdit, the left-hand keys should type 2.1's table,
   including `fn` → `z` and the key left of `Z` → `x`.
3. **Bottom row**: left `control` acts as Fn (Fn+arrows page). In
   TextEdit, right `option` with the key that types `a` moves to the
   paragraph start (Cocoa's `Ctrl+A`, so it is Control); in
   Terminal, right `option`+Tab switches apps (Command). In TextEdit,
   hold right `option` and press Tab twice: the app switcher should stay
   open and move to the second app; left `command`+Tab should switch
   tabs in a browser. In
   EventViewer, no app should show two keys sending `command`.
4. **Editing**: in TextEdit, Control-position+C / V copy and paste, and
   Control-position+arrows move by word; in Terminal, the same key+C
   interrupts `sleep 100`.
5. **Thumb**: Thumb+`e` (Emacs, again hides it), Thumb+`1`..`6`, Thumb+`/`,
   Thumb+`h` (GitHub tab picker; macOS asks to let Karabiner control
   Chrome), Thumb+`m` (Outlook mail; macOS asks for Accessibility).
6. **Bundle IDs**: `osascript -e 'id of app "Orca"'`, and likewise for the
   other apps in the generator's lists and in `aerospace.toml`. Fix any
   that differ.
7. **AeroSpace**: `aerospace list-apps` while the communication apps run,
   then check that new windows land on workspace 4.

Logs: `~/Library/Logs/mac-jump.log` for the jump helper, and
`~/.local/share/karabiner/log/console_user_server.log` for Karabiner's
`shell_command` output.

### Changing the config

Edit `bin/karabiner-gen-config`, never Karabiner's settings UI: the
generator's next run overwrites `karabiner.json` and loses changes made
there. If `git diff` shows Karabiner rewrote the file, port the change to
the generator. Run the generator, then `bin/test-karabiner-gen-config`;
Karabiner reloads the file on its own.
After editing `aerospace.toml`, run `aerospace reload-config`.

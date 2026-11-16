# link-router: open links in the right Chrome profile

Chrome opens links from other apps in whichever profile was used last.
link-router is set as the default browser instead.  It opens each link
in the Chrome profile its rules give, and asks about links that match
no rule.

## Rules

The rules live in `~/.config/link-router/rules.yaml`, outside this
public repository, as they name the sites in use.  Adam keeps his in a
private `desktop-config-private` repository, linked into `~/.config`
like this one.  Start from
[`rules.example.yaml`](../.local/lib/link-router/rules.example.yaml).

Both versions share the file.  It lists the profiles to offer, each
with a label and Chrome's folder for it (`dir`, as on
`chrome://version`), then the rules, first match wins.  Its header
comments describe the patterns.  Running `link-router` (or
`link-router.ps1`) with no link opens the file for editing.

A link matching no rule brings up a picker.  Choosing a profile opens
the link there once.  Adding a rule appends it to the rules file,
behind any symlink, so it shows up in `git diff` in the private
repository and travels to the other machines from there.

## macOS

- `bin/link-router` (Bash 3.2) matches, asks and opens.  The pickers
  are `osascript` dialogs, the only kind a shell script can show
  without extra software.
- `.local/lib/link-router/install-macos` builds
  `~/Applications/LinkRouter.app` and offers to make it the default
  browser.  The app is an `osacompile` applet, because macOS delivers
  links to apps as Apple Events, which a shell script cannot receive.
  It asks `~/bin/link-router --route` to open each link, and shows the
  picker itself when no rule matches: one panel with a button per
  profile (⌘1, ⌘2, …), then an "Always use it for links matching" box
  (⌘R) with the pattern to save; Esc cancels.  A dialog shown by
  osascript from the script took over 2s and often did not get the
  keyboard focus; the app is up front in about 0.3s.  It is one panel,
  not a series of dialogs, because AeroSpace moves the focus elsewhere
  whenever a window closes, so a second dialog lost the keyboard.  The
  script still does the matching, opening and rule-writing, so only
  changes to the picker need a rebuild.  `--uninstall` removes it.
- Run from a terminal, `link-router URL` shows an older picker, as a
  series of osascript dialogs.
- Errors, from the script or the app, go to
  `~/Library/Logs/link-router.log`.

Needs `yq` (and `defaultbrowser` to set the default from the command
line); the installer gets both from Homebrew.

## Windows

- `.local/lib/link-router/windows/link-router.ps1` (Windows PowerShell
  5.1) does the same, with a WinForms picker.  Nothing is stowed on
  Windows, so it reads the rules from a `desktop-config-private`
  checkout beside this one, or from `LINK_ROUTER_RULES`.
- `install.ps1` in the same folder registers it as a browser for the
  current user and opens Settings at Default apps, where Windows insists
  the default is chosen by hand.  `-Uninstall` removes it.
- Errors go to `%LOCALAPPDATA%\link-router\link-router.log`.

Needs `yq` (`winget install MikeFarah.yq`); the installer gets it.

## Tests

`bin/test-link-router` and `windows/test-link-router.ps1` check the
matching and the picker's outcomes, with the dialogs replaced by canned
answers.  Both run on Linux too.  Keep `matches()` in the Bash script
and `Test-Match` in the PowerShell one in step.

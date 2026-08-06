# Autorandr-based monitor management (experimental)

How to use and understand the hybrid monitor management PoC from
[specs/autorandr-hybrid-monitor-plan.md](../specs/autorandr-hybrid-monitor-plan.md).

Two switchable systems coexist:

- **legacy** — the original custom stack (`monitor-watcher` →
  `get-layout` → `setup-monitor` incl. its hardcoded `xrandr` calls).
  Always available as a fallback; nothing in it depends on autorandr.
- **autorandr** — experimental: [autorandr](https://github.com/phillipberndt/autorandr)
  handles profile detection and xrandr application; everything else
  (panels, DPI, terminals, fluxbox layout, ...) still runs through
  `setup-monitor`.

Exactly one is active at a time (the systemd units `Conflicts=` each
other). The choice persists across logins.

## Daily use

```sh
# which watcher is enabled / running
systemctl --user is-enabled monitor-watcher.service monitor-watcher-ng.service
systemctl --user is-active  monitor-watcher.service monitor-watcher-ng.service
autorandr                  # saved profiles, with (detected)/(current)

# switch to the experimental system
systemctl --user disable --now monitor-watcher.service
systemctl --user enable  --now monitor-watcher-ng.service

# switch back (instant, ssh-safe: systemctl --user needs no DISPLAY)
systemctl --user disable --now monitor-watcher-ng.service
systemctl --user enable  --now monitor-watcher.service
```

Watch what the experimental system is doing:

```sh
journalctl --user -f -t monitor-watcher-ng
```

Plugging/unplugging a monitor that already has a profile: nothing to
do — the watcher detects the DRM event, autorandr matches the EDID
fingerprint, applies the saved xrandr config, and the postswitch hook
runs the rest of the desktop pipeline.

## Plugging in a new monitor for the first time

An unknown monitor matches no profile, so autorandr deliberately does
**nothing** (the external screen stays dark). This is by design: the
same mechanism ignores transient topologies mid-hotplug. To teach it
the new monitor:

1. Switch to the legacy system and let it converge:

   ```sh
   systemctl --user disable --now monitor-watcher-ng.service
   systemctl --user enable  --now monitor-watcher.service
   ```

   Wait for the layout to apply fully (or run `setup-monitor` by hand).
   Check the result is what you want — resolution, refresh rate, and
   `xrandr | grep primary` on the right output.

2. Save the converged state as a profile, named after the *hardware*
   (not the layout — several monitors can share one layout):

   ```sh
   autorandr --save celtic+AOC-U28G2G6B    # example naming
   ```

3. If the fluxbox layout name differs from the profile name (usual
   case), record the mapping:

   ```sh
   echo celtic+external > ~/.config/autorandr/celtic+AOC-U28G2G6B/layout
   ```

   Without a `layout` file, the profile name itself is used as the
   layout name.

4. Verify:

   ```sh
   grep primary ~/.config/autorandr/<profile>/config   # primary saved?
   autorandr                                           # shows "(detected)"
   autorandr --change --dry-run                        # review the xrandr call
   ```

5. Profiles live in the repo (`~/.config/autorandr` is a stow symlink
   into `.config/autorandr/`), so commit:

   ```sh
   git add .config/autorandr/<profile> && git commit
   ```

6. Switch back:

   ```sh
   systemctl --user disable --now monitor-watcher.service
   systemctl --user enable  --now monitor-watcher-ng.service
   ```

The same procedure re-saves an *existing* profile after changing its
setup (different mode, refresh rate, primary, rotation, ...): converge
under legacy (or configure manually with plain `xrandr`), then
`autorandr --save <existing-name>` overwrites it.

Only save **stable** topologies you want auto-applied. Never save a
transient state (e.g. the 2-monitor stage while a dock is still
bringing up its third monitor) — leaving it unsaved is exactly what
makes autorandr ignore it.

## How it works

```
DRM hotplug event (udev)
  └─ bin/monitor-watcher-ng            (systemd user service)
       debounce 1s, drain event burst
       clear-dpy-cache                  # invalidate libdpy caches
       autorandr --change
         ├─ predetect hook              # wait until xrandr sees as many
         │                              # connected outputs as the kernel
         │                              # (sysfs) reports, max 6s
         ├─ fingerprint EDIDs, match against saved profiles
         │    no match → no-op          # unknown monitor or transient state
         ├─ apply profile's xrandr config (modes, positions, primary)
         └─ postswitch hook
              ├─ map profile → fluxbox layout
              │    (~/.config/autorandr/<profile>/layout, default: profile name)
              ├─ stale-kill any in-flight setup-monitor (TERM, 5s, KILL)
              ├─ refresh libdpy caches, compute md5 staleness token
              └─ setsid setup-monitor --skip-xrandr \
                     --layout L --expected-layout L --expected-md5 M &
                   # overlay, panels, keyboard, DPI, terminals,
                   # fluxbox-reconfigure, fonts, window layout (ly),
                   # fluxbox restart, xfce4-panel, nm-applet
```

Key differences from the legacy system:

- **No settle/md5 machinery in the watcher.** The legacy watcher had to
  guess when a topology had "settled". autorandr's exact-EDID-set
  matching replaces that: partial/transient states match nothing.
- **`setup-monitor --skip-xrandr`** reuses the entire non-xrandr
  pipeline unchanged; autorandr has already applied the xrandr config.
- **Staleness protection is unchanged**: `setup-monitor`'s
  `assert_expected_state` still aborts (exit 75) if the monitor md5 or
  detected layout changes mid-run, and postswitch kills a superseded
  run before starting the next.
- The ng service deliberately omits `NoNewPrivileges=true` (the legacy
  unit has it) so spawned processes can FUSE-mount AppImages — see the
  comment in `.xsession-progs.d/person-adam.spiers/01-window-manager`.

### Login

`01-window-manager` starts `fluxbox-session.target`, which both watcher
units are `WantedBy=`, so whichever one is enabled starts there. The
target must be started from the X session because it has to follow
`00-systemd-user-env` putting `DISPLAY` into the systemd `--user`
environment; `default.target` would be too early. Both units are also
`PartOf=` it, so the watcher stops when the session ends.
At startup the ng watcher runs one `autorandr --change` to reconcile;
if the matching profile is already active this is a no-op (hooks
included).

### Neutralised package auto-triggers

The autorandr package ships four triggers of its own; all are disabled
so that *only* `monitor-watcher-ng` invokes autorandr, and only in
autorandr mode:

| Trigger | Neutralised by |
|---|---|
| udev rule `40-monitor-hotplug.rules` → `autorandr.service` | `systemctl mask autorandr.service` |
| `autorandr.service` (also `WantedBy=sleep.target`) | same mask |
| `autorandr-lid-listener.service` | `systemctl mask autorandr-lid-listener.service` |
| `/etc/xdg/autostart/autorandr{,-kde}.desktop` at login | `Hidden=true` overrides in `.config/autostart/` (stowed) |

## Recovery / rollback

Instant switch back (works over ssh — no DISPLAY needed):

```sh
systemctl --user disable --now monitor-watcher-ng.service
systemctl --user enable  --now monitor-watcher.service
```

If the display state is wrong after switching:

```sh
. ~/.Xdisplay.celtic && setup-monitor
```

To switch just for the current session, without changing what starts at
next login, use `start`/`stop` instead of `enable`/`disable --now`:

```sh
systemctl --user stop  monitor-watcher-ng
systemctl --user start monitor-watcher
```

Full removal: revert the `autorandr-poc` branch, restow, `systemctl
--user daemon-reload`, `sudo systemctl unmask autorandr.service
autorandr-lid-listener.service`, `sudo zypper rm autorandr`. The legacy
path never depended on any of it.

## Known limitations (accepted for the PoC)

- **Exact-match only**: the legacy `get-layout` rules match *any*
  external monitor by width/aspect; autorandr needs a saved profile per
  EDID set. Unknown monitor → dark screen until captured (or switch
  back to the legacy watcher).
- A monitor can present **different EDIDs on different inputs** (the
  Samsung G75F does on HDMI vs DP), so the same physical monitor may
  need one profile per input used.
- During staged dock bring-up, outputs stay dark until the full
  topology matches — the legacy system force-enabled them earlier but
  then had to guess about settling.

## File reference

| File | Role |
|---|---|
| `bin/monitor-watcher-ng` | experimental watcher (udev → autorandr) |
| `.config/systemd/user/monitor-watcher-ng.service` | its unit; `Conflicts=` legacy |
| `.config/systemd/user/fluxbox-session.target` | raises `graphical-session.target`; both watchers are `WantedBy=`/`PartOf=` it |
| `.config/autorandr/predetect` | hotplug race guard |
| `.config/autorandr/postswitch` | bridge to `setup-monitor --skip-xrandr` |
| `.config/autorandr/<profile>/` | saved profiles (`config`, `setup`, optional `layout`) |
| `.config/autostart/autorandr*.desktop` | disable packaged login triggers |
| `bin/setup-monitor` | unchanged pipeline; `--skip-xrandr` added |

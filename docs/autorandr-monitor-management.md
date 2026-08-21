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
   autorandr --match-edid                              # shows "(detected)"
   autorandr --match-edid --change --dry-run           # review the xrandr call
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

```text
DRM hotplug event (persistent udev monitor)
  └─ bin/monitor-watcher-ng              (systemd user service)
       ├─ retain events while autorandr is running
       └─ bounded reconciliation loop    (max 30s / 12 attempts)
            ├─ if one inactive external has a checksum-valid base EDID
            │    exactly matching one saved profile, but incomplete extensions:
            │    enable only its admitted preferred mode as a wake-up probe;
            │    wait for a later complete autorandr match
            ├─ clear-dpy-cache            # invalidate libdpy caches
            ├─ autorandr --match-edid --change --skip-options gamma
            │    (later attempts explicitly reload the first matched profile
            │     while all of its outputs remain connected)
            │    ├─ predetect hook        # wait until xrandr sees as many
            │    │                        # connected outputs as sysfs, max 6s
            │    ├─ match saved EDIDs and apply xrandr configuration
            │    └─ postswitch hook records the applied profile but defers
            │         the desktop pipeline
            ├─ require an event-free 5s interval
            ├─ require detected profile == current profile
            │    mismatch or queued event → retry
            │    no detected profile      → retry without applying anything
            └─ run postswitch once after convergence
                 ├─ map profile → fluxbox layout
                 │    (~/.config/autorandr/<profile>/layout, default: profile name)
                 ├─ stale-kill any in-flight setup-monitor (TERM, 5s, KILL)
                 ├─ refresh libdpy caches, compute staleness token
                 └─ setsid setup-monitor --skip-xrandr \
                        --layout L --expected-layout L --expected-md5 M &
                      # overlay, panels, keyboard, DPI, terminals,
                      # fluxbox-reconfigure, fonts, window layout (ly),
                      # fluxbox restart, xfce4-panel, nm-applet
```

Key differences from the legacy system:

- **Autorandr still owns topology matching and xrandr.** Exact EDID-set
  matching rejects partial or unknown topologies without reimplementing
  profile selection in the watcher. For profiles with exact EDID fingerprints,
  `--match-edid` lets one saved profile follow the same physical monitors when
  a dock or GPU renames connectors (for example, AOC on `DisplayPort-2`
  becoming `DisplayPort-1`); the watcher consumes autorandr's reported mapping
  before validating connected and active outputs.
- **A wake-up probe is separate from profile selection.** Some Samsung plug
  sequences expose a checksum-valid, exact known 128-byte base identity but
  broken extension blocks indefinitely while the output stays disabled. Kernel
  connector presence blocks laptop fallback even before X catches up. When
  exactly one external output is connected/inactive, one internal output is
  active, one saved external profile entry resolves to that output with the
  exact base block and connected topology,
  no checksum-valid full DRM EDID/profile match exists, and X advertises a
  preferred mode, the watcher may enable only that captured mode beside the
  internal display. It does not set
  the primary output, record a selected profile, or run desktop work. The
  ordinary autorandr match, exact connected topology, matching X/DRM base
  blocks, and checksum-valid complete DRM EDIDs must all appear afterward
  before any profile is loaded. The probe is attempted at most once per
  reconciliation cycle.
- **The watcher verifies convergence rather than assuming `xrandr` exit 0
  means the physical link stayed active.** This handles slow monitors which
  first expose their EDID, then drop back to connected-but-disabled while
  DisplayPort link training continues. DRM events received during autorandr
  remain queued, and both detected-but-not-current and temporarily unmatched
  states are retried within the bounded window. If a monitor's EDID becomes
  temporarily truncated after an initial successful match, later attempts
  explicitly reload that remembered profile only while all of its configured
  outputs still report connected. This follows the working two-load workaround
  from upstream issue #402 without forcing an external profile after a genuine
  unplug. A matched profile is retained across a bounded timeout/service
  restart so the next cycle can resume recovery. The 5s quiet interval covers
  the observed final Samsung/hub event at 4.3s.
- **Gamma is ignored globally for detection and application** via
  `.config/autorandr/settings.ini` (`skip-options=gamma`) because Redshift owns
  the live CRTC gamma ramps. The watcher also passes the option explicitly so
  transaction-local/future isolated config roots retain the same safety rule;
  gamma differences do not make a display profile inactive or get reapplied.
- **`setup-monitor --skip-xrandr`** reuses the entire non-xrandr
  pipeline unchanged after autorandr has converged.
- **Staleness protection remains the final safety net**: `setup-monitor`'s
  `assert_expected_state` still aborts (exit 75) if the monitor identity or
  detected layout changes mid-run, and postswitch kills a superseded run
  before starting the next.
- The ng service deliberately omits `NoNewPrivileges=true` (the legacy
  unit has it) so spawned processes can FUSE-mount AppImages — see the
  comment in `.xsession-progs.d/person-adam.spiers/01-window-manager`.

### Performance diagnostics

`setup-monitor` emits elapsed phase markers to the service journal:

```sh
journalctl --user -t monitor-watcher-ng --since today |
    grep 'TIMING setup-monitor'
```

A measured Samsung/hub replug before the first performance pass took about
35.3s from the first DRM event to a settled tray: 12.1s converging the physical
link, 1.7s preparing postswitch state, and 21.5s in `setup-monitor`. Batching
per-window Fluxbox commands, removing obsolete fixed sleeps, and overlapping
the cold Emacs font reload with independent configuration reduced
`setup-monitor` to 18.0s. Visible window placement completed in 5.8s rather
than about 9.7s.

The remaining dominant setup cost is intentional: restarting Fluxbox takes
about 3s and rebuilding XFCE's panel/systray takes about 9s. The latter waits
through a delayed systray-wrapper replacement before starting nm-applet;
shortening it previously produced an invisible 21x21 icon. Optimize or skip
those restarts only with observable health checks and repeated real hotplug
verification.

### Login

After `00-systemd-user-env` has put `DISPLAY` into the systemd `--user`
environment, `01-window-manager` registers its exact `XDG_SESSION_ID`; a
user-global fallback is deliberately forbidden because it could identify a
different concurrent session. Registration starts `fluxbox-session.target`,
which both watcher units are `WantedBy=`, so whichever one is enabled starts
there. The separately supervised `fluxbox-session-lifetime@watch.service`
reference-counts registered logind sessions under a lock and stops the target
only after the last session is authoritatively closing or removed. The watcher
is started before registration and remains supervised even while no graphical
target is active. The same lock is held through target stop, so a concurrent
login registers and restarts the target afterward; a failed stop restarts the
watcher and retries. This logs out the watchers without coupling target
lifetime to routine Fluxbox process restarts.
At startup the ng watcher runs one `autorandr --change` to reconcile;
if the matching profile is already active this is a no-op (hooks
included).

### Neutralised package auto-triggers

The autorandr package ships four triggers of its own; all are disabled
so that *only* `monitor-watcher-ng` invokes autorandr, and only in
autorandr mode:

| Trigger | Neutralised by |
| --- | --- |
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

- **EDID matching**: the legacy `get-layout` rules match *any* external
  monitor by width/aspect; autorandr normally needs a saved profile per
  EDID set. Unknown monitor → dark screen until captured (or switch
  back to the legacy watcher). Autorandr permits one `*` wildcard in a
  profile's `setup` EDID when part of a monitor's fingerprint is unstable.
  Connector-name changes do not require duplicate profiles for exact EDIDs
  because the watcher invokes autorandr with `--match-edid`. Autorandr may not
  rename a wildcarded profile because the wildcard prevents it extracting the
  same serial-based fingerprint as the live EDID; such a profile can still
  require a connector-specific variant. Likewise, a profile containing
  multiple external monitors which exchange connectors may need a separate
  capture because autorandr does not always report both halves of the swap.
- A monitor can present **different EDIDs on different inputs** (the
  Samsung G75F does on HDMI vs DP), so the same physical monitor may
  need one profile per input used. Its DisplayPort profile wildcards
  volatile bytes 352–383 while retaining the stable manufacturer,
  model, serial, timing data, and final 16-byte suffix.
- The G75F DisplayPort EDID itself marks its first detailed timing,
  `3440x1440@59.97`, as preferred even though CTA VIC 126/193 advertise
  `5120x2160@60/120` and the same EDID advertises VESA DSC 1.2a. Therefore
  `xrandr --auto` correctly follows a sink firmware/EDID quirk rather than an
  amdgpu or negotiated-bandwidth failure. Saved autorandr geometry and the
  known-good replay path must select `5120x2160` explicitly.
- During staged dock bring-up, outputs stay dark until the full
  topology matches — the legacy system force-enabled them earlier but
  then had to guess about settling.

## File reference

| File | Role |
| --- | --- |
| `bin/monitor-watcher-ng` | persistent udev listener and bounded autorandr convergence controller |
| `.config/systemd/user/monitor-watcher-ng.service` | its unit; `Conflicts=` legacy |
| `.config/systemd/user/fluxbox-session.target` | raises `graphical-session.target`; both watchers are `WantedBy=`/`PartOf=` it |
| `.config/autorandr/predetect` | hotplug race guard |
| `.config/autorandr/postswitch` | records deferred attempts and bridges a converged/manual switch to `setup-monitor --skip-xrandr` |
| `bin/test-monitor-watcher-ng` | behavioral tests for retry, timeout, idempotence, and hook deferral |
| `.config/autorandr/<profile>/` | saved profiles (`config`, `setup`, optional `layout`) |
| `.config/autostart/autorandr*.desktop` | disable packaged login triggers |
| `bin/setup-monitor` | unchanged pipeline; `--skip-xrandr` added |

# Upstream audit: why `setup-monitor` still restarts Fluxbox, xfce4-panel and nm-applet

Bead: `dc-lij`. Audited 2026-09-02 against the sources of the installed
packages:

| Process | Installed package | Source audited | OBS home |
|---|---|---|---|
| fluxbox | `fluxbox-1.3.7-2.101` (built in `home:AndnoVember:test`, GCC 10.3) | `fluxbox-1.3.7.tar.xz` + upstream git `Release-1_3_7-221-g36f99b92` | `X11:windowmanagers/fluxbox` (classic OBS package) |
| xfce4-panel | `xfce4-panel-4.20.8-1.1` | `xfce4-panel-4.20.8.tar.bz2`, plus `libxfce4windowing-4.20.6` which it delegates monitor tracking to | `X11:xfce/xfce4-panel` (classic) |
| xfce4-notifyd | `xfce4-notifyd-0.9.7-1.7` | `xfce4-notifyd-0.9.7.tar.bz2` | `X11:xfce/xfce4-notifyd` (classic) |
| nm-applet | `NetworkManager-applet-1.36.0-5.4` | `network-manager-applet-1.36.0.tar.xz`, plus GTK `3.24.52` `gtk/deprecated/gtktrayicon-x11.c` | `GNOME:Factory/NetworkManager-applet` (**scmsync**, git at `src.opensuse.org/GNOME/NetworkManager-applet`) |

The workarounds in `bin/setup-monitor` are: `fr` (Fluxbox restart via an
xfwm4 swap), `xfce4-panel -r`, and `pkill nm-applet` + `wait_for_stable_tray`
+ relaunch. xfce4-notifyd is not restarted, but its `+0+0` placement bug
(`dc-esm`) is one of the things the Fluxbox restart is known to clear.

## Summary of verdicts

| Symptom | Responsible component | Status | Patchable in an OBS branch? |
|---|---|---|---|
| `_NET_DESKTOP_GEOMETRY` stale until Fluxbox restarts (`dc-fka` root cause) | **Fluxbox** | Confirmed defect, present in 1.3.7 **and** upstream master | Yes, ~5 lines |
| Fluxbox ignores CRTC/output changes that do not change the framebuffer size (monitor power-cycle, dock renegotiation) | **Fluxbox** | Confirmed defect in 1.3.7, fixed upstream Feb 2026 (PR 83, unreleased) | Yes, backport one 7-line commit |
| Panel found docked at Y=0 after a relayout; notifications at `+0+0`; both cleared by a Fluxbox restart (`dc-esm`) | **Fluxbox** placement of windows that already asked for a position | Mechanism identified in code (two paths), race not proven live | Yes, small change in `BScreen::clearHeads()` / `FluxboxWindow::init()`, plus a logging patch to prove it |
| `_NET_WORKAREA` only reflects head 1's struts | **Fluxbox** | Confirmed limitation (upstream `!!TODO`) | Yes, but low value: GTK only consults it for the primary monitor |
| Panel needs `xfce4-panel -r` after monitor changes | xfce4-panel / libxfce4windowing | **No defect found.** 4.20.8 relayouts on RandR events and on the xfconf changes `setup-panels` makes | Nothing to patch; needs `PANEL_DEBUG` capture if it still misbehaves after the Fluxbox fixes |
| All plugin wrappers respawn ~6 s after `xfce4-panel -r` (`dc-9l7`) | xfce4-panel | Not explained by code; only two paths respawn *all* wrappers (socket unrealize, D-Bus owner loss) | Diagnose first (`PANEL_DEBUG=external`), no patch yet |
| One systray wrapper orphaned to init (`dc-kmg`) | xfce4-panel wrapper | Plausible: wrapper only exits on D-Bus `Quit`/owner loss; no `PR_SET_PDEATHSIG` | Yes, trivial (`prctl(PR_SET_PDEATHSIG, SIGTERM)` in `wrapper/main.c`) |
| nm-applet icon 21x21 one pixel below the screen after a tray rebuild (`dc-9l7`) | **xfce4-panel systray host**, not nm-applet | The "size negotiated once" hypothesis is wrong; GTK re-docks and re-reads the size. 21x21 is produced by the host's shrink-to-fit loop | Best fix is configuration: run `nm-applet --indicator` (StatusNotifier). Appindicator support is compiled in on x86_64 |
| nm-applet | NetworkManager-applet | No defect in nm-applet code | Would need `osc fork` (scmsync), but no code patch is warranted |

## Fluxbox

### F1. `_NET_DESKTOP_GEOMETRY` is written once and never updated (confirmed)

`Ewmh::updateGeometry()` (`src/Ewmh.cc:904`) writes the property. Its only
caller is `Ewmh::initForScreen()` (`src/Ewmh.cc:616`). Nothing connects it
to `BScreen::m_resize_sig`; the only subscribers of `resizeSig()` are the
Slit and Toolbar (`src/Slit.cc:257`, `src/Toolbar.cc:227`).

`BScreen::updateSize()` (`src/Screen.cc:1510`) does emit
`m_workspace_area_sig`, which reaches `Fluxbox::workspaceAreaChanged` →
`AtomHandler::updateWorkarea` (`src/fluxbox.cc:1365`), so `_NET_WORKAREA`
*is* refreshed on resize. `_NET_DESKTOP_GEOMETRY` is not. This is exactly
the stale value (`6720x2160` on an `8000x2160` screen) that `dc-fka` traced
to "setup-monitor never reached its Fluxbox restart".

Upstream master (`36f99b92`, 2026-02-15) has the identical call graph, so
this is unfixed upstream.

**Patch:** in `Ewmh::updateWorkarea(BScreen&)` call `updateGeometry(screen)`
first, or add a `join(screen->resizeSig(), …)` in `Fluxbox::initScreen()`
that calls it. Either is a handful of lines.

### F2. Only `RRScreenChangeNotify` is handled (confirmed, fixed upstream)

`src/fluxbox.cc:800-810`:

```cpp
if (e->type == s_randr_event_type) {
    XRRUpdateConfiguration(e);
    BScreen *scr = searchScreen(e->xany.window);
    if (scr != 0) scr->updateSize();
}
```

`s_randr_event_type` is the RandR event base, i.e. `RRScreenChangeNotify`
only. CRTC/output changes arrive as `base + RRNotify` and are dropped, even
though `BScreen` selects for them (`src/Screen.cc:278-288`). Upstream
[fluxbox/fluxbox#83](https://github.com/fluxbox/fluxbox/pull/83) fixes
precisely this ("after a monitor power cycle, newly created windows would
not have any decoration and would be placed in the top left of the
screen"). The fix is a 7-line change to `src/fluxbox.cc` and applies cleanly
to 1.3.7.

Note `updateSize()` calls `initXinerama()` unconditionally, but everything
else (root geometry, workarea, `clearHeads()`) only runs if the root window
size changed (`src/Screen.cc:1515`). A head change with an unchanged
framebuffer therefore never re-runs `clearHeads()` or the workarea update.

### F3. Windows that asked for a position get re-placed at the top-left (mechanism identified)

Both the panel and xfce4-notifyd position their windows with
`gtk_window_move()` before mapping; GTK 3.24 then sets `PPosition`
(`gtk/gtkwindow.c:10012-10020`). xfce4-notifyd windows are WM-managed,
not override-redirect, by default (`xfce-notify-daemon.c:266-270`,
default `FALSE`; Adam's xfconf has no override set).

Two Fluxbox paths ignore that request:

1. **At map time**, `FluxboxWindow::init()` (`src/Window.cc:460-474`):

   ```cpp
   bool is_visible = isWindowVisibleOnSomeHeadOrScreen(*this);
   ...
   else if (m_client->isTransient() ||
       m_client->normal_hint_flags & (PPosition|USPosition)) {
       m_placed = is_visible;
   }
   ...
   if (m_placed) moveResize(frame().x(), frame().y(), ...);
   else          placeWindow(getOnHead());
   ```

   `isWindowVisibleOnSomeHeadOrScreen` (`src/Window.cc:200-208`) tests only
   the window's **top-left corner** against Fluxbox's cached Xinerama head
   list. If the corner is not on a cached head, the window goes to
   `placeWindow(0)`: `RowMinOverlapPlacement` over the whole screen, whose
   first candidate is `(0,0)`. That is the `+0+0` notification signature.

2. **On every root-size change**, `BScreen::clearHeads()`
   (`src/Screen.cc:1627-1657`) walks every managed window, and any window
   not overlapping a head is handed to `placeWindow(closest_head)`. It does
   not exempt `_NET_WM_WINDOW_TYPE_DOCK` windows or windows with
   `PPosition`. A panel that was on the external monitor when that monitor
   disappears is therefore re-placed by the WM at the top-left of the
   remaining head, i.e. Y=0 with its full width. The panel only moves itself
   back if it subsequently gets a `size_allocate` with a *different*
   position; if it already recomputed its final position before Fluxbox
   processed a later intermediate RandR event (the dock renegotiation
   sequence `640x480 → 3440x1440 → 5120x2160` is exactly such a burst),
   Fluxbox's placement wins and the panel stays at Y=0.

   This matches the `dc-esm` capture (panel `0x600003` at `Absolute Y: 0`,
   width 2880, notification at `0,0`, everything else correct) and matches
   why a Fluxbox restart fixes both at once: on restart `isStartup()` makes
   `m_placed = true` for every existing window and rebuilds the head list.

   Not proven live: the race between the panel's own move and Fluxbox's
   `clearHeads()` needs a log of Fluxbox placement decisions, which the
   openSUSE build cannot emit (`-DEBUG`, `fbdbg` compiled out).

**Patch:** in `clearHeads()` skip windows whose `m_state.type` is
`TYPE_DOCK`/`TYPE_DESKTOP`, and windows whose client set
`PPosition|USPosition`; in `init()` treat a `PPosition` window as visible if
its rectangle *intersects* any head rather than requiring its corner to be
on one. Add an `fbdbg`-independent log line in `placeWindow()` (to
`~/.log/fluxbox.log`) so the next occurrence is attributable. All small.

### F4. `_NET_WORKAREA` covers head 1 only (confirmed, low value)

`Ewmh::updateWorkarea()` (`src/Ewmh.cc:925-960`, "!!TODO … just doing this
on the first head") publishes screen-wide bounds minus head 1's struts.
Struts are bound to whichever head the panel's **window centre** was on when
it last set `_NET_WM_STRUT` (`src/Ewmh.cc:1391-1405`,
`BScreen::getHead(win)` at `src/Screen.cc:1673`), and heads are renumbered
whenever the primary output changes. GTK only reads `_NET_WORKAREA` for the
primary monitor and falls back to the monitor geometry when it does not
intersect (`gdk/x11/gdkmonitor-x11.c`, `gdk_x11_monitor_get_workarea`), so
this cannot itself yield `+0+0`; it just makes bottom-right placement ignore
the panel on non-primary heads. Fixing it properly means emitting
`_GTK_WORKAREAS_D<n>` per monitor. Not worth doing before F1-F3.

### Fluxbox packaging notes

- The running binary is from `home:AndnoVember:test`, built with GCC 10.3;
  that project's `openSUSE_Factory` build now **fails**, so the package is
  frozen. `X11:windowmanagers/fluxbox` (= `openSUSE:Factory/fluxbox`,
  `1.3.7-2.8`, adds `gcc11.patch`) builds and is the right branch target:
  `osc branch X11:windowmanagers fluxbox`.
- Alternative: package the git snapshot `Release-1_3_7-221-g36f99b92`
  (221 commits, includes PR 83, artificial per-head struts, `_NET_WM_NAME`,
  `_NET_DESKTOP_NAMES` encoding fixes). Bigger review surface; F1 and F3
  still need local patches either way.
- Upstream: neither F1 nor F3 has an issue on GitHub (searched
  `_NET_DESKTOP_GEOMETRY`, `workarea`, `xrandr`, `xinerama`); only PR 83
  exists. Both are worth filing with the patches.

## xfce4-panel (+ libxfce4windowing)

### P1. Monitor tracking is event-driven and complete for our case (no defect found)

The panel does not use GDK monitor signals directly; it subscribes to
`XfwScreen::monitors-changed` (`panel/panel-window.c:3839`).
libxfce4windowing's X11 backend refreshes on **both** `RRScreenChangeNotify`
and `RRNotify` (`xfw-monitor-x11.c:568-577`, idle-coalesced), reads the
RandR primary flag (`:515`), and re-reads `_NET_WORKAREA` on
`PropertyNotify`. `panel_window_screen_layout_changed()`
(`panel/panel-window.c:2741-2960`) resolves `output-name = "Primary"` via
`xfw_screen_get_primary_monitor()` (`:2842-2848`), falls back to the first
monitor if there is none, hides the window only when the resolved geometry
is `0x0` (`:2896-2905`), and otherwise sets `window->area`, queues a resize
and re-sets struts. `p=8` is `SNAP_POSITION_SW` (`:306-324`), whose Y is
`area.y + area.height - window_height` (`:1883`). The xfconf writes
`setup-panels` makes (`position`, `output-name`, `length`, `size`) all
re-run `panel_window_screen_layout_changed()` from `set_property`
(`:984-999`).

So nothing in 4.20.8 requires a restart after `setup-panels` + xrandr. The
only code path that puts the panel at Y=0 with a correct width is external
(F3 above). The two 4.20.4 regressions in this area
([#925](https://gitlab.xfce.org/xfce/xfce4-panel/-/issues/925),
[#927](https://gitlab.xfce.org/xfce/xfce4-panel/-/issues/927)) were fixed
in 4.20.5.

Also: `xfwm4` is not needed to "repair" the panel. The panel never listens
for `window-manager-changed`; the xfwm4 swap in `bin/fluxbox-restart` works
only because the *Fluxbox* restart resets `m_placed`.

### P2. Whole-wrapper respawn ~6 s after `xfce4-panel -r` (unexplained by code)

`xfce4-panel -r` → old instance gets `Terminate(restart)` → destroys its
windows (each `PanelPluginExternal` socket is unrealized → child asked to
`QUIT` → exits `PLUGIN_EXIT_SUCCESS`, no restart,
`panel/panel-plugin-external.c:383-401`, `:687-745`) → `main()` returns and
`g_spawn_command_line_async(argv[0])` starts the new instance
(`panel/main.c:405-409`). The new instance spawns wrappers when its sockets
realize (`:355-376`). Only two code paths then respawn **every** wrapper at
once:

- the panel window being unrealized/re-realized (sockets unrealize → child
  quits → `realize` respawns). Nothing in the 4.20.8 panel does this on a
  monitor change; `composited-changed` only toggles opacity
  (`panel/panel-base-window.c:518-548`). `xcompmgr` (pid 6785, started at
  login, parent 1) is not restarted by any relayout script, so this trigger
  is unlikely;
- every wrapper's D-Bus proxy losing the `org.xfce.Panel` owner
  (`wrapper/main.c:46-58`), which quits the wrapper with exit 0 and lets the
  panel respawn it after `PANEL_PLUGIN_AUTO_RESTART`.

`monitor-panel-restart@.service` (a second `xfce4-panel -r` from the
monitor controller) did not exist when the 2026-07-28 observation was made
and has run once since (2026-08-31), so a double restart does not explain
it either.

**Next step, not a patch:** start the panel once with
`PANEL_DEBUG=external,positioning,systray` (env is inherited across `-r`
because the new instance is spawned by the old one) and log to a file; the
`"%s-%d: child exited with status %d"` and `"plugin unrealized; quitting
child"` lines identify which of the two paths fires.

### P3. Systray wrapper orphaned to init (`dc-kmg`, plausible cause)

`wrapper/main.c` exits only on a D-Bus `Quit`, on `QUIT_FOR_RESTART`, or
when the `org.xfce.Panel` name owner disappears (`:46-58`). There is no
`prctl(PR_SET_PDEATHSIG)` (`:283` only sets `PR_SET_NAME`) and no watch on
the parent PID. If the panel dies **while another instance already owns the
name** (the `-r` handover overlaps, or the panel is killed after the new one
claimed the name), the orphan never sees an owner change and lives on. A
one-line `prctl(PR_SET_PDEATHSIG, SIGTERM)` after fork, or a parent-PID
check in the wrapper, closes this. Trivially patchable in an
`X11:xfce/xfce4-panel` branch; also worth reporting upstream.

### P4. Where 21x21 comes from (nm-applet symptom)

`plugins/systray/systray-box.c:560-600`: when a row overflows the box,
allocation restarts with `icon_size--` until it fits ("y overflow … restart
with icon_size=%d"). The socket child is then allocated `icon_size` square
(`:496`, `:537-545`). The docked XEMBED plug is resized to whatever the
socket allocates. So a 21px icon means the systray box was, at that
allocation, ~21px tall, i.e. it was laid out inside a panel/box allocation
that was not the final 40px one. Whether that stale allocation persists
depends on GTK re-allocating the socket afterwards, which the code will do
on the next box `size_allocate`; the observed persistence suggests the plug
was docked to a socket that never received a further allocation, e.g. a
wrapper about to be replaced (see P2). Cannot be settled without the
`PANEL_DEBUG=systray` "allocate rows=%d, icon_size=%d" lines.

## xfce4-notifyd

Placement (`xfce-notify-window.c:1774-1830`) is arithmetic on a cached
per-monitor workarea: bottom-right is
`wa.x + wa.width - rect.x - rect.width`, `wa.y + wa.height - rect.y -
rect.height`, then `gtk_window_move()` (`:1631`). The cache
`monitors_workarea[]` is filled from `gdk_monitor_get_workarea()` at first
use, on `GdkScreen::monitors-changed` and on every `_NET_WORKAREA`
`PropertyNotify` (`xfce-notify-daemon.c:397-500`). The active monitor is
`gdk_display_get_monitor_at_point(pointer)` (`:1566-1571`).

- No path in this code produces `(0,0)` from correct inputs, and GDK's X11
  workarea cannot come back empty (it falls back to the monitor geometry).
  Combined with the `dc-esm` finding that a fresh notifyd still lands at
  `+0+0` while a Fluxbox restart fixes it, the placement error is the WM's
  (F3), not notifyd's. **Nothing to patch here for this bug.**
- Because the window is WM-managed, the daemon-side mitigation is to set
  `/windows-use-override-redirect=true` in the `xfce4-notifyd` xfconf
  channel; override-redirect windows bypass Fluxbox placement entirely.
  That is a config change, not a patch, and only masks F3.
- Minor code smell, not our bug: the `_NET_WORKAREA` handler indexes
  `monitors_workarea[j]` for the *current* GDK monitor count without
  checking it matches the allocated size (`:428-446`). X event ordering
  makes the RandR event (and hence the realloc) arrive first, so it is not
  exploitable in practice.

## nm-applet

- **Build:** the openSUSE spec passes `-Dappindicator=yes` on every arch
  except ppc64le (`NetworkManager-applet.spec:103-107`), so StatusNotifier
  support is compiled in. It is used only with `nm-applet --indicator`
  (`src/main.c:50-54`); Adam's `nm-applet.service` and `setup-monitor` run
  plain `nm-applet`, so the XEMBED `GtkStatusIcon` path is in use.
- **"Negotiates its size once" is not what the code does.** GTK's
  `GtkTrayIcon` gets `DestroyNotify` for the manager window, clears it, and
  on the next `MANAGER` `ClientMessage` re-finds the selection owner,
  re-reads `_NET_SYSTEM_TRAY_ORIENTATION/VISUAL/COLORS/PADDING/ICON_SIZE`
  and re-docks (`gtktrayicon-x11.c`, `gtk_tray_icon_manager_filter`,
  `gtk_tray_icon_update_manager_window`, `gtk_tray_icon_manager_window_destroyed`).
  `notify::icon-size` re-emits `GtkStatusIcon::size-changed`, and
  nm-applet's handler reloads icons at the nearest of 16/22/24/32
  (`src/applet.c:3075-3105`). The **plug window size** itself is dictated by
  the host socket allocation (P4), not by nm-applet, so a patch to nm-applet
  cannot fix a 21x21 plug.
- nm-applet does not exit when the tray vanishes; it only hides the fallback
  via `gtk_status_icon_is_embedded` (`src/applet.c:711`,
  `applet_embedded_cb` at `:3198`). The old "nm-applet dying" note is not
  supported by this code.
- **Recommended fix is configuration:** `ExecStart=/usr/bin/nm-applet
  --indicator`. The xfce4-panel systray plugin hosts StatusNotifier items
  natively (`plugins/systray/sn-*.c`), sized by the panel, with no XEMBED
  dock, no `MANAGER` race and therefore no reason to `pkill`/relaunch it or
  to `wait_for_stable_tray`. Verify the menu/secrets dialogs still work
  under Fluxbox before removing the workaround.
- **OBS:** `GNOME:Factory/NetworkManager-applet` is scmsync-managed
  (`<scmsync>https://src.opensuse.org/GNOME/NetworkManager-applet#…`), so
  `osc branch` yields an uneditable pointer; source changes need
  `osc fork openSUSE:Factory NetworkManager-applet` (osc 1.27.3 supports it;
  needs a Gitea login in `~/.config/tea/config.yml`). Not needed: there is
  no code defect to patch.

## Side findings

- `bin/setup-monitor` does `pkill nm-applet` and `nohup nm-applet`, but
  nm-applet is also a systemd user unit (`nm-applet.service`,
  `Restart=on-failure`). Only one instance is running now, under the unit,
  so the nohup'd copy is being lost or never started; either way the two
  launch paths should be reconciled (`systemctl --user restart
  nm-applet.service` instead of pkill/nohup).
- `bin/fluxbox-restart` swaps in xfwm4 "to fix corrupted xfce4-panel". The
  panel code does not react to a WM change at all; the repair comes from
  Fluxbox restarting (F3). Once F1-F3 are patched the xfwm4 detour should
  be unnecessary, and with them the Fluxbox restart itself becomes a
  fallback rather than a step.

## Recommended order

1. Branch `X11:windowmanagers/fluxbox`, add three patches (F1 geometry on
   resize; F2 backport of PR 83; F3 `clearHeads()`/`init()` exemption plus a
   placement log line). Build for `openSUSE_Tumbleweed`, install, and let
   normal dock/undock cycles run with `fr` still in place; the log line
   shows whether Fluxbox ever re-places the panel or a notification.
2. Switch nm-applet to `--indicator` and drop `pkill`/`wait_for_stable_tray`.
3. Run the panel with `PANEL_DEBUG=external,positioning,systray` logging to
   `~/.log/` to explain the 6 s wrapper respawn before touching the panel.
4. Only then remove `fr` and `xfce4-panel -r` from `setup-monitor`
   (`dc-643` fast path), keeping them as a fallback keyed on
   `_NET_DESKTOP_GEOMETRY` disagreement.

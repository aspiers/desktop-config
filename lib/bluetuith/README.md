# bluetuith 0.2.7 quoted-name override

Tumbleweed's `bluetuith-0.2.7` displays literal JSON quotes around adapter and
device names. BlueZ aliases are not quoted. The regression is in
[`bluetooth-classic` v0.0.8][backend]: `Optional[T].UnmarshalJSON` delegates to
`UnmarshalText`, so string optionals retain the lexical JSON bytes—including
the surrounding quotes and escapes.

`bluetooth-classic-quoted-optional.patch` decodes JSON into `T` before storing
it and makes the matching marshal path produce valid JSON. Its tests cover the
variant-map boundary, preserve legitimate embedded quote characters, retain
primitive behavior, and reject JSON `null` without mutating an existing value.
The quote regressions fail against unpatched v0.0.8.

Run:

```sh
./bin/install-bluetuith-quoted-names-fix
```

The installer fetches pinned commits for bluetuith v0.2.7 and
bluetooth-classic v0.0.8, applies and tests the patch, runs the full bluetuith
suite, and atomically installs `~/.local/bin/bluetuith`. That path precedes
`/usr/bin`, leaving the Tumbleweed RPM untouched. Desktop launches such as
Mod4-u run as systemd user units, so they see it only because
`.cfg-post.d/xsession-progs` puts `~/.local/bin` in the manager's `PATH`; if
the quotes come back, check `readlink /proc/$(pgrep -x bluetuith)/exe`. Remove the local binary to
roll back:

```sh
rm ~/.local/bin/bluetuith
```

Reported upstream as [bluetuith#87][upstream-issue] on 2026-11-07, as a bug
report without the patch: the maintainer has said "No AI contributions". Once
a bluetuith release picks up a fixed bluetooth-classic, remove the local
binary.

[upstream-issue]: https://github.com/bluetuith-org/bluetuith/issues/87

[backend]: https://github.com/bluetuith-org/bluetooth-classic/tree/v0.0.8

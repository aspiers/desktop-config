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
`/usr/bin`, leaving the Tumbleweed RPM untouched. Remove the local binary to
roll back:

```sh
rm ~/.local/bin/bluetuith
```

As of 2026-08-21, searches of open and closed issues and pull requests in both
canonical repositories found no matching report or fix. Public issue/patch
submission still requires explicit approval.

[backend]: https://github.com/bluetuith-org/bluetooth-classic/tree/v0.0.8

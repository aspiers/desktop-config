# autorandr parser fixtures

Four profile directories are read-only copies of the tracked real profiles at
implementation time. They exercise the installed `config`, `setup`, and
optional `layout` grammar, including the real Samsung and Level39 single-`*`
EDID patterns. The `ambiguous` and `collision` profiles are synthetic rejection
cases. Command fixtures model only documented stdout interfaces;
`samsung-renamed.fingerprint` and `aoc-renamed.fingerprint` change connector
names while preserving profile identity. No fixture came from invoking the live
display during tests.

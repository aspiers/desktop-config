# Shadow trace evidence (`dc-a5y.11`)

This directory deliberately separates three provenance classes. Only the first
is live shadow acceptance evidence.

| Provenance | Meaning | Artifacts |
| --- | --- | --- |
| `live` | Bounded production audit/status evidence observed with the fixed parser running | `live_samsung_restart_steady.audit.jsonl`, `live_samsung_restart_steady.evidence.json` |
| `retained_raw_derived` | Deterministically sanitized excerpts or fixtures derived from retained raw XRandR/ng evidence | `ng-retained-excerpts.json`, `../xrandr/live-samsung-20260822.*`, historical hashes in `manifest.json` |
| `synthetic_policy` | Reducer scenario replay used only for deterministic regression coverage | the other eight `*.jsonl` files and `../../scenarios/shadow-trace-scenarios.json` |

The eight synthetic traces model seven required physical cases because
controller restart has unresolved and verification policy variants. **None of
those seven physical cases currently satisfies live shadow acceptance.**

## Bounded live Samsung restart/steady capture

The shadow service had already been restarted after deployment of the parser
fix. Collection did not restart, stop, reload, or reconfigure either service
and did not induce a display event. The bounded live evidence records:

- `monitor-controller-shadow.service`: active/running, PID 3975780,
  `NRestarts=0`, started `2026-08-22 03:22:04 BST`;
- authoritative `monitor-watcher-ng.service`: active/running, unchanged PID
  2734905, `NRestarts=0`, started `2026-08-18 23:38:32 BST`;
- five valid observations with exact and current profile
  `celtic+Samsung-Odyssey-G75F`;
- production decisions admitting `RequestPlan` and then `PrepareDesktop`, and
  exactly one audit record of kind `WOULD_PREPARE`;
- production wake reasons, monotonic times, state-key pairs, and command,
  observation, reduction, persistence, and worker timing slots;
- absent `/run/user/1000/monitor-controller/shadow/transactions`;
- a `monitor-controller-*` unit listing containing only the shadow service and
  therefore zero monitor worker units; and
- identical pre/post read-only `xrandr --query` SHA-256
  `ad6770392a78362f09931cea88b8d5c2bf7768e4ee7c9593609b7d5d7910a3f6`.

The live source is the first 18 lines (bytes `[0, 268952)`) of the production
audit segment whose bounded SHA-256 is
`b6d1aa8cb09787eaf07612dc22d671a7c4f87ab879e73b488c84036b402bd0a9`.
The containing 251-line, 4,185,167-byte rotated file has SHA-256
`0e43c88f2975ca724f8256baa9ce29868839ee9ef457df2391843a8e53403152`.
Complete bounds and service evidence are in
`live_samsung_restart_steady.evidence.json`.

This proves only restart into, and subsequent observation of, the already
steady Samsung topology. It is not a Samsung plug, broken-EDID, unplug,
suspend/resume, AOC, or pending-action restart trace.

### Deterministic audit sanitization

Every selected source line is parsed independently. The capture retains the
source line number and SHA-256 of the complete source line including LF.
Header, decision, and `would_dispatch` semantics are retained. Decision audit
metadata, events, effects, and would-dispatch payloads are retained unchanged.

State objects are projected onto this fixed whitelist:

```text
action_sequence_high_water, aggressive_deadline_ms, application,
baseline_adoption, candidate, controller_instance,
desktop_finalized_profile, event_generation, external_intent, finalization,
next_timer_ms, observation_generation, phase, physical_epoch, physical_token,
planning, planning_state, preparation, preparation_state, probe,
reconcile_epoch, stable_x_profile, transition_sequence_high_water,
unknown_key, unknown_since_ms, unplug_proof, verify_since_ms,
latest_observation
```

All canonical fields of `latest_observation` and event observations are kept,
except each `live_fingerprints[].value`. Each such lowercase even-length hex
string is decoded to bytes and replaced with
`value_redaction={sha256: SHA256(decoded_bytes), bytes: len(decoded_bytes)}`;
`output` is unchanged. The resulting records are serialized with sorted keys,
ASCII JSON, no insignificant whitespace, and one trailing LF. The Samsung and
eDP EDIDs are therefore retained only as hash+length (400 and 384 bytes). No
other timestamps, IDs, keys, timings, wake reasons, effects, mappings, profile
names, raw-evidence hashes, or non-EDID values are rewritten.

## Retained ng and XRandR evidence

`ng-retained-excerpts.json` records the exact bounded journal command and the
SHA-256/byte/line bounds of its output. For each case with retained source it
stores exact short-monotonic lines, inclusive requested/selected time ranges,
a deterministic line-selection rule, and an excerpt hash. It explicitly marks
these unavailable comparisons:

- no Samsung same-profile suspend/resume range was identified; the retained
  celtic internal same-profile excerpt is only an analogue; and
- ng was not restarted, so no ng restart comparison exists.

Even where an ng excerpt exists, it is comparison input only: there was no
simultaneous valid shadow transition trace. The AOC excerpts do establish the
real connector mapping in retained source:

```text
saved autorandr output DisplayPort-2 -> live output DisplayPort-1
```

The pre-restart read-only XRandR fixture is
`retained_raw_derived`, not acceptance. It reproduces the parser defect that
had made 303 prior shadow observations invalid. Its raw EDIDs are also replaced
by per-output SHA-256 and byte length.

## Synthetic policy replay contract

`laptop_startup.jsonl`, `samsung_plug.jsonl`,
`samsung_broken_edid_beyond_30_seconds.jsonl`, `genuine_unplug.jsonl`,
`same_profile_suspend_resume.jsonl`, `restart_unresolved.jsonl`,
`restart_verification.jsonl`, and `aoc_connector_rename.jsonl` are
`synthetic_policy` scenario replays. Their JSONL `header` and `decision` names
belong to the reducer replay format, not the production audit format. They do
not contain production `audit` metadata and do not claim production wake
reasons, state-key chains, or command timings.

The scenarios still provide useful structural regression coverage:

- each timestamped state/effect/count expectation is checked;
- each replay fixture is regenerated byte-for-byte and replays identically;
- exact effect counts, phase order, and timer deadlines are checked; and
- the AOC canonical observation explicitly contains
  `OutputMapping("DisplayPort-2", "DisplayPort-1")`, rather than relying on an
  opaque physical token or generic DP connector names.

Synthetic `*Dispatched` and `*Finished` events model hypothetical future active
worker acknowledgements only. They are not live shadow worker records.

## Genuine acceptance still blocked

Physical events with the fixed parser running are still required for all seven
transition cases: laptop startup, Samsung plug, Samsung broken/slow EDID beyond
30 seconds, genuine unplug, Samsung same-profile suspend/resume, controller
restart while unresolved or verifying/pending, and AOC connector rename. The
live steady Samsung capture does not remove those blockers.

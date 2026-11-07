# Keyboard mouse cheatsheet

Driving the pointer without touching the mouse, on the X11 fluxbox desktop.

- **Neru** jumps the pointer: to a labelled spot on the screen (grid), or to
  something clickable it can see (hints).
- **fievel** steers the pointer from the home row (free mouse mode), for
  fine positioning after a jump.
- **keyd** still does all other key remapping. fievel sits after it and
  only adds the free mouse chord.

## Starting a mode

| Keys | Opens | Use it to |
| --- | --- | --- |
| `Super` + `-` | Neru grid | Jump to any spot on the current monitor |
| `Super` + `;` | Neru hints | Jump to a button, link or piece of text |
| hold `d`+`f`, or hold `F3` | fievel free mouse mode | Steer the pointer and click |

## Neru grid (`Super` + `-`)

The screen is covered in cells with two-letter labels.

| Step | Keys | What happens |
| --- | --- | --- |
| 1 | two-letter label, e.g. `ab` | Pointer jumps to that cell |
| 2a | `Escape` | Stop there; grid closes |
| 2b | sub-cell key (see below) | Pointer moves to that part of the cell; grid closes |
| 2c | `Shift`+`L` / `Shift`+`R` | Click there |

Sub-cell keys are laid out like the cell itself:

| | | |
| :---: | :---: | :---: |
| `u` | `i` | `o` |
| `j` | `k` | `l` |
| `m` | `,` | `.` |

## Neru hints (`Super` + `;`)

Neru reads the focused window's text (OCR) and labels what it finds.

| Keys | What happens |
| --- | --- |
| two-letter label | Pointer moves to that target; new labels appear |
| `Tab` / `Shift`+`Tab` | Cycle to the next / previous hint |
| `/` | Search hints by their text |
| `Backspace` | Undo the last letter |
| `Escape` | Close hints |

## Keys inside any Neru mode

| Keys | Action |
| --- | --- |
| `Shift`+`L` | Left click |
| `Shift`+`R` | Right click |
| `Shift`+`M` | Middle click |
| `Shift`+`I` / `Shift`+`U` | Press / release the left button (drag) |
| `Shift`+`N` / `Shift`+`P` | Jump to the next / previous monitor (grid and hints) |
| arrow keys | Nudge the pointer a few pixels |
| `Escape` | Leave the mode |

Grid only: `Space` starts the label again, `Backspace` undoes a letter.

## fievel free mouse mode (hold `d`+`f` or `F3`)

The mode lasts while the trigger is held. Press `z` to lock it on, and `z`
or the trigger again to release it.

| Keys | Action |
| --- | --- |
| `j` `k` `i` `l` | Move left, down, up, right |
| `Space` | Left click |
| `o` | Right click |
| hold `a` | Move slowly |
| hold `s` | Move fast |
| `n` `m` `,` `.` | Scroll left, down, up, right |
| `,` + `m` together | `Home` |
| `n` + `.` together | `End` |
| `z` | Lock the mode on / off |

fievel has no middle click; use `Shift`+`M` in a Neru mode.

## When something stops working

| Symptom | Fix |
| --- | --- |
| `Super`+`-` or `Super`+`;` does nothing | `systemctl --user restart neru` |
| `d`+`f` / `F3` does nothing | `systemctl --user restart fievel-keyd` |
| Keys still run old commands after editing `keys.erb` | `fluxbox-gen-config`, then `fluxbox-remote Reconfigure` |
| Hints finds nothing useful | Use the grid instead; hints only scan the focused window |

## Where this is configured

All paths are in this repository. Update this cheatsheet whenever any of
them changes a key or a behaviour described here.

| File | Defines |
| --- | --- |
| `.fluxbox/keys.erb` | `Super`+`-` and `Super`+`;` |
| `.config/neru/config.toml` | Neru labels, sub-cell keys, monitor keys, hint strategy |
| `.config/fievel/fievel.config` | fievel trigger, movement and click keys |
| `bin/fievel-keyd` | Runs fievel on keyd's virtual keyboard |
| `.config/systemd/user/neru.service`, `fievel-keyd.service` | Start both with the fluxbox session |

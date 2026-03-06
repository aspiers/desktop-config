# Plan: HWINFO Hybrid Monitor Detection

## Status: IMPLEMENTED

## Problem Statement
INXI is failing to detect monitor models due to a bug in its
`map_monitor_ids()` function which can't match kernel connector names
(e.g. DP-10) to X11 connector names (e.g. DisplayPort-12) when port
numbers differ significantly. See `specs/fix-inxi-edid-parsing.md`
for the full root cause analysis.

## Solution
Hybrid monitor detection: use hwinfo for model names, match to xrandr
outputs via EDID serial numbers extracted from `xrandr --props`.
Falls back to inxi if hwinfo is unavailable.

## Why This Approach
- EDID serial numbers are globally unique (perfect for matching)
- hwinfo reliably detects all monitor models
- Bypasses inxi's broken port-number matching entirely
- Confirmed perfect matches:
  - Dell U2414H: `X4J717CQ1R4L`
  - BenQ BL3200: `A1F01944SL0`
  - Laptop: `0` (special case, matched by eDP output prefix)

## Files Modified

### Primary
1. **`lib/libdpy.py`** - Added `extract_edid_serial_from_xrandr()`,
   `get_hybrid_monitors()`. Updated `large_monitor_connected()`,
   `external_monitor_connected()`, `find_monitor_by_attribute()`, and
   `get_inxi_primary_monitor()` to use hybrid detection.
2. **`bin/connected-monitor-props`** - Switched to `get_hybrid_monitors()`
3. **`bin/get-layout`** - Switched to `get_hybrid_monitors()`

### Test
4. **`bin/test-libdpy`** - Updated MD5 stability test to use hwinfo cache

## Implementation Summary

### `extract_edid_serial_from_xrandr()`
Parses EDID hex from `xrandr --props`, extracts serial number string
descriptors (tag 0xFF) from the four 18-byte descriptor blocks in the
base EDID. Returns `{output_name: serial_string}`.

### `get_hybrid_monitors()`
1. Gets monitor models from `hwinfo --monitor` (via cached `get_hwinfo_monitors()`)
2. Gets xrandr screen geometries (via cached `get_xrandr_screen_geometries()`)
3. Extracts EDID serials from `xrandr --props`
4. Matches hwinfo monitors to xrandr outputs by:
   - **EDID serial number** (primary, reliable method)
   - **eDP prefix** for laptop displays with serial="0"
5. Any unmatched xrandr outputs get model="Unknown"
6. Falls back to `get_inxi_monitors()` if hwinfo fails

### Design Decision: No physical size matching
Physical size matching was considered as a fallback but rejected
because two monitors can easily have the same panel size, making
matches ambiguous. Only serial-based matching is used.

## Backward Compatibility

- All function signatures unchanged
- `get_inxi_monitors()` still available for direct inxi data access
- Automatic fallback to inxi if hwinfo unavailable
- Cache files coexist (inxi + hwinfo)

## Edge Cases

1. **hwinfo not installed**: Falls back to inxi automatically
2. **No EDID serial**: Monitor left unmatched (logged via debug)
3. **Laptop serial="0"**: Matched by eDP output name prefix
4. **Monitor unplugged**: No xrandr match, skipped (expected)

## Rollback

If issues occur:
```python
def get_hybrid_monitors(use_cache=True):
    return get_inxi_monitors(use_cache)
```

All calling code unchanged - simple one-line revert.

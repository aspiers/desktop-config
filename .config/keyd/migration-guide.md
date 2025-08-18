# Migrating from XKB to keyd

## Disable XKB remappings

When using keyd, you must disable your existing XKB remappings to avoid double-remapping issues.

### 1. Temporarily disable (for testing)

```bash
# Reset to standard keyboard layout without custom remappings
# Use 'gb' since your XKB config includes both 'us' and 'gb' (UK layout)
setxkbmap -layout gb -option

# Note: This maintains UK symbol mappings (£ on Shift+3, " on Shift+2)
# while removing your custom 'adam' symbol remappings
```

### 2. Permanently disable

Comment out or remove these from your startup scripts:

#### In `~/.xsession-progs.d/person-adam.spiers/50-keyboard`:
```bash
# setup-keyboard  # Comment this out
```

#### In `keyboard-watcher` service:
Either stop the service or modify it to not call `setup-keyboard`:
```bash
# Stop the watcher service
systemctl --user stop keyboard-watcher
systemctl --user disable keyboard-watcher
```

#### In `keymap-menu`:
This script will become obsolete with keyd, as keyd handles device detection automatically.

### 3. Prevent accidental XKB activation

Rename or disable the keymap scripts:
```bash
# Make scripts non-executable (reversible)
chmod -x ~/bin/keymap-menu
chmod -x ~/bin/setup-keyboard

# Or rename them (also reversible)
mv ~/bin/keymap-menu ~/bin/keymap-menu.xkb-backup
mv ~/bin/setup-keyboard ~/bin/setup-keyboard.xkb-backup
```

## Testing keyd safely

### 1. Test in a separate session

Before committing fully:

```bash
# Start keyd in debug mode to test
sudo keyd -d

# In another terminal, check your keyboard is working as expected
# If something goes wrong, Ctrl+C in the debug terminal
```

### 2. Have a recovery plan

Keep a way to revert if needed:

```bash
# Create recovery script
cat > ~/bin/restore-xkb.sh << 'EOF'
#!/bin/bash
# Emergency restore of XKB settings
sudo systemctl stop keyd
setxkbmap -layout gb  # or your preferred base layout
~/bin/keymap-menu     # Re-apply your XKB customizations
EOF
chmod +x ~/bin/restore-xkb.sh
```

### 3. Gradual migration

You could run different keyboards with different systems:
- Use keyd for external USB keyboards
- Keep XKB for built-in laptop keyboard (by excluding its ID from keyd config)

## Advantages of switching to keyd

1. **Works everywhere**: X11, Wayland, console, even in BIOS/GRUB
2. **Automatic device detection**: No need for `keyboard-watcher`
3. **Simpler configuration**: One config file instead of complex XKB symbol files
4. **Better performance**: Lower-level remapping is more efficient
5. **Per-device configuration**: Different layouts for different keyboards automatically

## Verification

After switching to keyd, verify:

```bash
# Check keyd is running
sudo systemctl status keyd

# Monitor key events
sudo keyd -m

# Verify XKB is reset to defaults
setxkbmap -print | grep xkb_symbols
# Should show basic symbols without your custom "adam" modifications
```

## Rollback procedure

If you need to return to XKB:

```bash
# Stop keyd
sudo systemctl stop keyd
sudo systemctl disable keyd

# Restore execute permissions
chmod +x ~/bin/keymap-menu
chmod +x ~/bin/setup-keyboard  

# Re-enable keyboard-watcher
systemctl --user enable keyboard-watcher
systemctl --user start keyboard-watcher

# Apply your XKB layout
~/bin/setup-keyboard
```

## Notes

- keyd and XKB **cannot** be used together for remapping
- You can still use XKB for layout selection (us/gb/de) with keyd doing the remapping
- Your custom XKB directory `.xkb/` can be kept as a reference/backup
# keyd Configuration - Adam's Custom Keyboard Layout

This directory contains a keyd configuration that replicates Adam's highly customized XKB keyboard mappings found in the `.xkb/` directory.

## Overview

The configuration translates the complex XKB symbol definitions from `.xkb/symbols/adam` and `.xkb/symbols/adam-common` into keyd format. Adam's layout includes several distinctive features:

### Core Features

1. **XCV Layout Modification**: The x, c, v keys are relocated to unusual positions
2. **QAZ Layout Modification**: The q, a, z keys are moved from their standard QWERTY locations
3. **Left Modifier Remapping**: Left Alt becomes Control, Left Meta becomes Alt
4. **Right Modifier Customization**: Various right-side modifiers become Super, Menu, or Compose
5. **Specialized Alpha Key Positions**: Several alphabetic keys are moved to create a personalized layout

## Key Mappings Summary

### Fundamental Layout Changes

| Standard Key | Becomes | XKB Reference |
|--------------|---------|---------------|
| Left Alt | Left Control | adam-common(laptop-left-mods) |
| Left Meta/Win | Left Alt | adam-common(laptop-left-mods) |
| Right Alt | Right Meta/Super | adam-common(right-alt-super-right) |
| Right Control | Compose/Menu | adam-common(right-ctrl-menu) |
| Caps Lock | 'a' | adam-common(standard-qaz) |
| Left Control | 'z' | adam-common(standard-qaz) |
| Grave/Tilde | 'q' | adam-common(standard-qaz) |
| Backslash | 'x' | adam-common(xcv2) |
| Z key | 'c' | adam-common(xcv2) |
| X key | 'v' | adam-common(xcv2) |
| Y key | Grave/Tilde | adam-common(left-alpha-common) |
| H key | Backslash | adam-common(left-alpha-common) |
| Apostrophe | Hash/Numbersign | adam-common(hash-tilde-by-enter) |

### Keyboard-Specific Configurations

The configuration includes specialized sections for different keyboard models:

#### Framework Laptop (`f0f0:0210`)
- Based on XKB `adam(framework)` configuration
- Includes standard laptop modifications plus Framework-specific function keys
- Search key mapping included

#### ThinkPad X1 Extreme G2 (`17aa:*`)
- Based on XKB `adam(thinkpad-x1-extreme-g2)` configuration
- Uses standard laptop modifier layout

#### Kinesis Advantage (`05f3:0007`, `05f3:0081`)
- Based on XKB `adam(kinesis)` and `adam(kinesis-360)` configurations
- Extensive thumb pad remapping due to unique Kinesis layout
- Arrow key swapping (up/down reversed)
- Caps Lock area becomes minus/underscore
- Special thumb cluster mappings for modifiers

#### Dell E7450 (`413c:*`)
- Based on XKB `adam(dell-e7450)` configuration
- Different right modifier behavior (Right Ctrl becomes Super instead of Menu)

#### Keyboardio Keyboards
- **Model 01** (`1209:2301`): Minimal modifications, most handled by firmware
- **Atreus** (`1209:2302`): Special sterling/Euro symbol access on number keys

#### Specialized Layouts
- **HHKL**: Happy Hacking Keyboard Lite with unique escape and modifier mappings
- **Lenovo W520**: Specific W520 laptop modifier configuration
- **Gyration**: Wireless keyboard with special less/greater key mapping
- **US QWERTY Hacked**: Unusual variant where Left Shift becomes 'x'

## Installation and Usage

1. **Install keyd**:
   ```bash
   # On most Linux distributions
   sudo pacman -S keyd        # Arch
   sudo apt install keyd      # Debian/Ubuntu
   sudo dnf install keyd      # Fedora
   ```

2. **Copy configuration**:
   ```bash
   sudo cp default.conf /etc/keyd/
   ```

3. **Enable and start keyd**:
   ```bash
   sudo systemctl enable keyd
   sudo systemctl start keyd
   ```

4. **Reload configuration** (after changes):
   ```bash
   sudo keyd reload
   ```

## Comparison with XKB Setup

The original XKB configuration used `setxkbmap` with custom symbol definitions:

```bash
# Framework laptop example from keymap-menu
setxkbmap -keycodes framework -symbols adam(framework) -geometry pc(pc105)
```

This keyd configuration provides equivalent functionality in a more modern, Wayland-compatible format.

## USB ID Detection

To find the correct USB ID for your keyboard:

```bash
# List input devices
sudo keyd -m

# Or check directly
lsusb | grep -i keyboard
```

Add new keyboard USB IDs to the appropriate `[ids]` section in the configuration.

## Troubleshooting

### Common Issues

1. **Keys not working as expected**: Check that the USB ID matches your keyboard
2. **Modifiers not functioning**: Ensure keyd service is running and configuration is loaded
3. **Conflicts with existing tools**: Disable other keyboard remapping tools (xmodmap, setxkbmap, etc.)

### Debug Mode

Run keyd in debug mode to see key events:
```bash
sudo keyd -v
```

### Testing Configuration

Test specific mappings:
```bash
# Check current keyd status
sudo keyd -l

# Reload configuration
sudo keyd reload
```

## Migration Notes

When migrating from the XKB setup:

1. **Disable XKB mappings**: Comment out or remove `keymap-menu` calls from startup scripts
2. **Test gradually**: Start with a simple configuration and add complexity
3. **Backup**: Keep the original XKB configuration for reference and fallback

## File Structure

```
.config/keyd/
├── default.conf    # Main keyd configuration
└── README.md       # This documentation
```

## References

- Original XKB configuration: `.xkb/symbols/adam*`
- Keyboard detection: `bin/setup-keyboard`
- Keymap selection: `bin/keymap-menu`
- [keyd documentation](https://github.com/rvaiya/keyd)

## Notes

This configuration represents years of keyboard customization and may not be suitable for general use. The layout is highly specialized for Adam's workflow and muscle memory. Consider cherry-picking individual features rather than adopting the entire configuration.

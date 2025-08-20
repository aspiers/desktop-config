# keyd Configuration - Adam's Custom Keyboard Layout

This directory contains a keyd configuration that replicates Adam's
highly customized XKB keyboard mappings found in the `.xkb/`
directory.


## Installation and Usage

1. **Install keyd**:
   ```bash
   # On most Linux distributions
   sudo zypper in keyd        # openSUSE
   sudo apt install keyd      # Debian/Ubuntu
   sudo dnf install keyd      # Fedora
   sudo pacman -S keyd        # Arch
   ```

2. **Copy configuration**:
   ```bash
   sudo ln -s `pwd`/keyd /etc
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

## References

- Original XKB configuration: `.xkb/symbols/adam*`
- Keyboard detection: `bin/setup-keyboard`
- Keymap selection: `bin/keymap-menu`
- [keyd documentation](https://github.com/rvaiya/keyd)

## Notes

This configuration represents years of keyboard customization and is
not suitable for general use.  The layout is highly specialized for
Adam's workflow and muscle memory.  It's recommended to cherry-pick
individual features or ideas rather than adopting the entire
configuration.

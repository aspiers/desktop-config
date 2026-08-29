# keyd Configuration - Adam's Custom Keyboard Layout

This directory contains keyd configuration that replicates Adam's
highly customized XKB keyboard mappings found in the `.xkb/`
directory. Device configuration lives in `daemon/`; `app.conf` contains
application-specific bindings consumed by `keyd-application-mapper`.

## Installation and Usage

1. **Install keyd**:

   ```bash
   # On most Linux distributions
   sudo zypper in keyd        # openSUSE
   sudo apt install keyd      # Debian/Ubuntu
   sudo dnf install keyd      # Fedora
   sudo pacman -S keyd        # Arch
   ```

2. **Link the daemon configuration only** from the repository root:

   ```bash
   sudo ln -sfnT "$PWD/.config/keyd/daemon" /etc/keyd
   ```

   Keeping `/etc/keyd` separate from the parent directory prevents the
   daemon from parsing `app.conf` as device configuration.

3. **Enable and start keyd**:

   ```bash
   sudo systemctl enable keyd
   sudo systemctl start keyd
   ```

4. **Enable application-specific mappings**:

   ```bash
   systemctl --user enable keyd-application-mapper.service
   ```

   The service starts with `fluxbox-session.target`, after the X session has
   published `DISPLAY` to the user service manager.

5. **Reload device configuration** after changes:

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

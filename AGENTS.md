# AGENTS.md

This file provides guidance to AI agents like Claude Code, Gemini CLI,
opencode etc. when working with code in this repository.

## Repository Overview

This is Adam's personal Xorg desktop configuration repository (dating
back to 2000) - a collection of scripts, configuration files, and
utilities for managing Linux desktop environments, primarily XFCE and
Fluxbox. It contains desktop layout management, browser automation,
system utilities, and task management integration.

## Symlink-based installation mechanism

**Important**: Files in this repository are installed to the home
directory ~ via GNU Stow, which creates symlinks from ~ and certain
sub-directories back to this repository.  For example:

    lrwxrwxrwx 1 adam adam 38 Jul  9  2023 /home/adam/bin/get-layout -> ../.STOW/desktop-config/bin/get-layout*

So when dealing with files that exist in both the repository (e.g.,
`bin/get-layout`) and the home directory (e.g., `~/bin/get-layout`),
default to operating on the repository files, to avoid having to ask
for permission to read/write those files.

## Development Commands

### Testing

Run scripts using relative paths rather than via `~`, like this:

```bash
# Test core display library functionality
./bin/test-libdpy

# Test individual scripts directly (no formal test framework)
./bin/desktop-layout --help
./bin/setup-monitor --dry-run
./lib/libdpy.py
```

### Key Commands

```bash
# Deploy configuration (creates symlinks to home directory)
stow .

# Main desktop management
./bin/desktop-layout [options] layout_name    # Apply desktop layouts
./bin/setup-monitor                           # Auto-configure monitors
./bin/get-layout                             # Get current layout

# System monitoring
./bin/monitor-watcher                        # Watch monitor changes
./bin/keyboard-watcher                       # Watch keyboard events
./bin/network-watcher                        # Watch network changes
```

## Architecture

### Core Libraries

- **`lib/libdpy.py`** - Display/monitor detection with caching (~/.cache/libdpy/)
- **`lib/liblayout.py`** - YAML layout file parsing and window positioning
- **`lib/libfonts.sh`** - Font configuration management
- **`lib/libproc.sh`** - Process management utilities

### Layout System

- **Configuration**: YAML files in `.fluxbox/layouts/` define monitor setups and window positions
- **Model-based**: Monitor identification by model strings for consistent configuration
- **HiDPI support**: Scaling factors for different display densities
- **Event-driven**: Automatic reconfiguration on hardware changes

### Browser Integration

- **Userscripts**: Extensive collection in `lib/browser/userscripts/` for site automation
- **Page ID helpers**: Tools for extracting references from project management sites
- **Chrome session tools**: Scripts for managing browser sessions

### AI-Assisted Coding

- **Configuration**: Cursor rules in `.cursor/rules/` define development workflow

## Dependencies

### System Requirements

- X11/Xorg desktop environment
- Python 3 with standard library
- inotify-tools (for watchers)
- xrandr, xkb, and other X11 utilities
- GNU Stow for deployment

### Python Dependencies

- No package manager files (requirements.txt, setup.py)
- Uses only Python standard library modules
- Optional: `leveldb` for Tampermonkey script extraction

## File Organization

### Key Directories

- **`bin/`** - Executable scripts (100+ utilities)
- **`lib/`** - Shared libraries and browser userscripts
- **`.fluxbox/layouts/`** - YAML layout configuration files
- **`.cursor/rules/`** - Development workflow and coding guidelines

### Configuration Patterns

- **Standards compliance**: Follows XDG Base Directory where possible
- **Personal optimization**: Heavily customized for specific hardware setups
- **Modular design**: Clear separation between libraries and executables
- **Caching strategy**: Performance optimization through cached monitor detection

## Development Guidelines

### Code Style

- Self-documenting code with extensive comments
- Consistent naming conventions across shell and Python scripts
- Error handling and logging throughout
- Follow existing patterns when modifying scripts

### Personal Configuration Focus

- Designed for single-user desktop management
- Not intended as general-purpose tools for other users
- Heavy customization for specific workflow preferences
- Consider cherry-picking individual files rather than wholesale adoption

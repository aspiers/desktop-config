// Blank every screen with a black window until a key, click or mouse
// movement, for Thumb+Ctrl+Alt+b (spec 3.5). Turning the displays off
// would lock the Mac, since the work Mac requires a password as soon as
// they go off and IT has greyed that setting out (Adam, 2026-11-18); a
// black window does not, though the backlight stays on.
//
// Built and run by `mac-jump blank`, which compiles this with swiftc on
// first use.

import AppKit

final class BlankWindow: NSWindow {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { true }
}

// Ignore input briefly, so letting go of the keys that started this does
// not end it at once.
let started = Date()
let graceSeconds = 1.0

func finish() {
    guard Date().timeIntervalSince(started) > graceSeconds else { return }
    NSCursor.unhide()
    exit(0)
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)

var windows: [NSWindow] = []
for screen in NSScreen.screens {
    let window = BlankWindow(contentRect: screen.frame, styleMask: .borderless, backing: .buffered, defer: false)
    window.setFrame(screen.frame, display: true)
    window.backgroundColor = .black
    window.isOpaque = true
    window.hasShadow = false
    window.level = .screenSaver
    window.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary, .ignoresCycle]
    window.acceptsMouseMovedEvents = true
    window.orderFrontRegardless()
    windows.append(window)
}

let ends: NSEvent.EventTypeMask = [.keyDown, .leftMouseDown, .rightMouseDown, .otherMouseDown, .mouseMoved, .scrollWheel]
// Events for this app's windows; swallowed, so nothing behind sees them.
NSEvent.addLocalMonitorForEvents(matching: ends) { _ in
    finish()
    return nil
}
// Events elsewhere, in case another window keeps the keyboard.
NSEvent.addGlobalMonitorForEvents(matching: ends) { _ in finish() }

NSCursor.hide()
app.activate(ignoringOtherApps: true)
windows.first?.makeKeyAndOrderFront(nil)
app.run()

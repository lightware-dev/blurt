import AppKit
import Carbon
import CoreGraphics

/// Inserts text into whatever field currently has focus. Requires Accessibility
/// permission (to post keyboard events to other apps).
enum TextInjector {
    static func inject(_ text: String) {
        guard !text.isEmpty else { return }
        if Settings.injectMode == "type" {
            typeUnicode(text)
        } else {
            paste(text)
        }
    }

    /// Fast path: put text on the clipboard, synthesize ⌘V, then restore the
    /// previous clipboard string once the target app has had time to paste.
    private static func paste(_ text: String) {
        let pb = NSPasteboard.general
        let previous = pb.string(forType: .string)
        pb.clearContents()
        pb.setString(text, forType: .string)
        let ourChange = pb.changeCount
        pressCmdV()
        // Slow apps (Electron, browsers under load) can take well over 250 ms
        // to service the synthesized ⌘V; restoring before they read the
        // pasteboard makes them paste the *old* clipboard instead of the
        // transcript. Skip the restore entirely if anything else has written
        // the pasteboard since — ours is no longer the value to undo.
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) {
            guard pb.changeCount == ourChange else { return }
            pb.clearContents()
            if let previous = previous { pb.setString(previous, forType: .string) }
        }
    }

    private static func pressCmdV() {
        let src = CGEventSource(stateID: .combinedSessionState)
        let vKey: CGKeyCode = 0x09 // 'v'
        let down = CGEvent(keyboardEventSource: src, virtualKey: vKey, keyDown: true)
        down?.flags = .maskCommand
        let up = CGEvent(keyboardEventSource: src, virtualKey: vKey, keyDown: false)
        up?.flags = .maskCommand
        down?.post(tap: .cghidEventTap)
        up?.post(tap: .cghidEventTap)
    }

    /// Robust path: emit each character as a Unicode keyboard event. Slower but
    /// works in terminals and apps that ignore synthetic ⌘V.
    private static func typeUnicode(_ text: String) {
        let src = CGEventSource(stateID: .combinedSessionState)
        for scalar in text.unicodeScalars {
            let utf16 = Array(String(scalar).utf16)
            for keyDown in [true, false] {
                guard let e = CGEvent(keyboardEventSource: src, virtualKey: 0, keyDown: keyDown) else { continue }
                utf16.withUnsafeBufferPointer { buf in
                    if let base = buf.baseAddress {
                        e.keyboardSetUnicodeString(stringLength: buf.count, unicodeString: base)
                    }
                }
                e.post(tap: .cghidEventTap)
            }
        }
    }
}

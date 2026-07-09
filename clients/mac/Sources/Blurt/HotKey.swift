import Carbon
import AppKit

/// System-wide hotkey via Carbon RegisterEventHotKey. Works without Accessibility
/// permission (unlike a CGEventTap), and fires on the main run loop.
///
/// A single process-wide Carbon handler dispatches every hotkey press to the right
/// instance by EventHotKeyID. (Installing one handler per instance is fragile: same-
/// target handlers only chain via eventNotHandledErr, and a mis-chain lets one key
/// swallow another's presses.)
final class HotKey {
    private static var callbacks: [UInt32: () -> Void] = [:]
    private static var handlerInstalled = false

    private var ref: EventHotKeyRef?
    private let id: UInt32

    init?(keyCode: UInt32, modifiers: UInt32, id: UInt32 = 1, onFire: @escaping () -> Void) {
        self.id = id
        HotKey.installHandlerIfNeeded()
        HotKey.callbacks[id] = onFire

        let hotID = EventHotKeyID(signature: OSType(0x56444B31) /* 'VDK1' */, id: id)
        let regStatus = RegisterEventHotKey(keyCode, modifiers, hotID,
                                            GetApplicationEventTarget(), 0, &ref)
        if regStatus != noErr { HotKey.callbacks[id] = nil; return nil }
    }

    deinit {
        if let ref = ref { UnregisterEventHotKey(ref) }
        HotKey.callbacks[id] = nil
    }

    private static func installHandlerIfNeeded() {
        guard !handlerInstalled else { return }
        handlerInstalled = true

        var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard),
                                 eventKind: UInt32(kEventHotKeyPressed))
        InstallEventHandler(
            GetApplicationEventTarget(),
            { (_, event, _) -> OSStatus in
                var fired = EventHotKeyID()
                let status = GetEventParameter(event, EventParamName(kEventParamDirectObject),
                                               EventParamType(typeEventHotKeyID), nil,
                                               MemoryLayout<EventHotKeyID>.size, nil, &fired)
                guard status == noErr, let cb = HotKey.callbacks[fired.id] else {
                    return OSStatus(eventNotHandledErr)
                }
                cb()
                return noErr
            },
            1, &spec, nil, nil)
    }
}

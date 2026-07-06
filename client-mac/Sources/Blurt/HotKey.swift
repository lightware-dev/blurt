import Carbon
import AppKit

/// System-wide hotkey via Carbon RegisterEventHotKey. Works without Accessibility
/// permission (unlike a CGEventTap), and fires on the main run loop.
final class HotKey {
    private var ref: EventHotKeyRef?
    private var handlerRef: EventHandlerRef?
    private let onFire: () -> Void

    init?(keyCode: UInt32, modifiers: UInt32, onFire: @escaping () -> Void) {
        self.onFire = onFire

        var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard),
                                 eventKind: UInt32(kEventHotKeyPressed))
        let selfPtr = Unmanaged.passUnretained(self).toOpaque()

        let installStatus = InstallEventHandler(
            GetApplicationEventTarget(),
            { (_, _, userData) -> OSStatus in
                guard let userData = userData else { return noErr }
                let me = Unmanaged<HotKey>.fromOpaque(userData).takeUnretainedValue()
                me.onFire()
                return noErr
            },
            1, &spec, selfPtr, &handlerRef)
        if installStatus != noErr { return nil }

        let hotID = EventHotKeyID(signature: OSType(0x56444B31) /* 'VDK1' */, id: 1)
        let regStatus = RegisterEventHotKey(keyCode, modifiers, hotID,
                                            GetApplicationEventTarget(), 0, &ref)
        if regStatus != noErr { return nil }
    }

    deinit {
        if let ref = ref { UnregisterEventHotKey(ref) }
        if let handlerRef = handlerRef { RemoveEventHandler(handlerRef) }
    }
}

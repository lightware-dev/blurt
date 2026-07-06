import AppKit

// Menu-bar agent entrypoint. No storyboard / no @main so we control the
// activation policy (accessory = no Dock icon, no menu bar app menu).
let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()

// Loft.app: a native macOS window around the local Loft server (WebKit, no browser).
// Built by install.sh with `swiftc`; Info.plist carries LoftPython and LoftCLI so the
// app can start the server itself when launched from Spotlight or the Dock.
import Cocoa
import UserNotifications
import WebKit

// `loft app --port N` passes `--port N`; Spotlight/Dock launches use the default.
let args = CommandLine.arguments
let port = args.firstIndex(of: "--port").flatMap { $0 + 1 < args.count ? args[$0 + 1] : nil } ?? "7717"
let home = URL(string: "http://127.0.0.1:\(port)/")!

// Only Loft's own page (scheme, host and port) gets the microphone and the native bridge.
func isLoft(_ o: WKSecurityOrigin) -> Bool {
    o.protocol == "http" && o.host == home.host && o.port == home.port
}
func isLoft(_ u: URL) -> Bool {
    u.scheme == "http" && u.host == home.host && u.port == home.port
}

// The page guards on `"Notification" in window`; WKWebView has none, so give it one
// that hands the notification to the app.
let notificationShim = """
(() => {
  const post = m => window.webkit.messageHandlers.loft.postMessage(m);
  const clicks = {};
  class N {
    constructor(title, o = {}) {
      this.title = title; this.body = o.body || ""; this.tag = o.tag || String(Math.random());
      clicks[this.tag] = this; post({ kind: "notify", title, body: this.body, tag: this.tag });
    }
    close() {}
    static get permission() { return "granted"; }
    static requestPermission() { return Promise.resolve("granted"); }
  }
  window.Notification = N;
  window.__loftNotificationClick = tag => { const n = clicks[tag]; if (n && n.onclick) n.onclick(); };
})();
"""

final class App: NSObject, NSApplicationDelegate, WKUIDelegate, WKNavigationDelegate,
    WKScriptMessageHandler, UNUserNotificationCenterDelegate
{
    var window: NSWindow!
    var web: WKWebView!

    func applicationDidFinishLaunching(_ note: Notification) {
        buildMenu()
        let cfg = WKWebViewConfiguration()
        cfg.userContentController.add(self, name: "loft")
        cfg.userContentController.addUserScript(
            WKUserScript(source: notificationShim, injectionTime: .atDocumentStart, forMainFrameOnly: true))
        web = WKWebView(frame: .zero, configuration: cfg)
        web.uiDelegate = self
        web.navigationDelegate = self

        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1280, height: 820),
            styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "Loft"
        window.contentView = web
        window.center()
        window.setFrameAutosaveName("LoftMain")
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)

        let center = UNUserNotificationCenter.current()
        center.delegate = self
        center.requestAuthorization(options: [.alert, .sound]) { _, _ in }

        DispatchQueue.global().async {
            let ok = self.startServer()
            DispatchQueue.main.async {
                if ok { self.web.load(URLRequest(url: home)) } else { self.showError() }
            }
        }
    }

    // `loft app --no-open` starts the server if needed and returns once it answers.
    func startServer() -> Bool {
        let info = Bundle.main.infoDictionary ?? [:]
        guard let py = info["LoftPython"] as? String, let cli = info["LoftCLI"] as? String else { return false }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: py)
        p.arguments = [cli, "app", "--no-open", "--port", port]
        p.standardOutput = FileHandle.nullDevice
        p.standardError = FileHandle.nullDevice
        do { try p.run() } catch { return false }
        p.waitUntilExit()
        return p.terminationStatus == 0
    }

    func showError() {
        web.loadHTMLString(
            "<body style='font:14px -apple-system;padding:40px;color:#888'>Loft's server didn't start. "
                + "Run <code>loft app</code> in a terminal to see why, then reopen Loft.</body>", baseURL: nil)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ app: NSApplication) -> Bool { true }

    // The window only ever shows Loft; anything else opens in the default browser.
    func webView(_ w: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = action.request.url else { return decisionHandler(.cancel) }
        if isLoft(url) || url.absoluteString == "about:blank" { return decisionHandler(.allow) }
        if action.navigationType == .linkActivated, ["http", "https", "mailto"].contains(url.scheme ?? "") {
            NSWorkspace.shared.open(url)
        }
        decisionHandler(.cancel)
    }

    func webView(_ w: WKWebView, createWebViewWith c: WKWebViewConfiguration, for action: WKNavigationAction,
                 windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = action.request.url, ["http", "https", "mailto"].contains(url.scheme ?? "") {
            NSWorkspace.shared.open(url)
        }
        return nil
    }

    // Voice input: allow the microphone for Loft's own page only.
    func webView(_ w: WKWebView, requestMediaCapturePermissionFor origin: WKSecurityOrigin,
                 initiatedByFrame frame: WKFrameInfo, type: WKMediaCaptureType,
                 decisionHandler: @escaping (WKPermissionDecision) -> Void) {
        decisionHandler(isLoft(origin) && frame.isMainFrame ? .grant : .deny)
    }

    func webView(_ w: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let a = NSAlert(); a.messageText = message; a.runModal(); completionHandler()
    }

    func webView(_ w: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let a = NSAlert(); a.messageText = message
        a.addButton(withTitle: "OK"); a.addButton(withTitle: "Cancel")
        completionHandler(a.runModal() == .alertFirstButtonReturn)
    }

    // Server restarted or not up yet: retry instead of leaving a blank window.
    func webView(_ w: WKWebView, didFailProvisionalNavigation n: WKNavigation!, withError e: Error) {
        DispatchQueue.main.asyncAfter(deadline: .now() + 1) { w.load(URLRequest(url: home)) }
    }

    func userContentController(_ c: WKUserContentController, didReceive m: WKScriptMessage) {
        guard isLoft(m.frameInfo.securityOrigin), let d = m.body as? [String: String], d["kind"] == "notify"
        else { return }
        let content = UNMutableNotificationContent()
        content.title = d["title"] ?? "Loft"
        content.body = d["body"] ?? ""
        content.userInfo = ["tag": d["tag"] ?? ""]
        UNUserNotificationCenter.current().add(
            UNNotificationRequest(identifier: d["tag"] ?? UUID().uuidString, content: content, trigger: nil))
    }

    func userNotificationCenter(_ c: UNUserNotificationCenter, didReceive r: UNNotificationResponse,
                                withCompletionHandler done: @escaping () -> Void) {
        let tag = r.notification.request.content.userInfo["tag"] as? String ?? ""
        let json = (try? String(data: JSONSerialization.data(withJSONObject: [tag]), encoding: .utf8)) ?? "[\"\"]"
        DispatchQueue.main.async {
            self.window.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
            self.web.evaluateJavaScript("window.__loftNotificationClick(\(json)[0])")
        }
        done()
    }

    func userNotificationCenter(_ c: UNUserNotificationCenter, willPresent n: UNNotification,
                                withCompletionHandler done: @escaping (UNNotificationPresentationOptions) -> Void) {
        done([.banner, .sound])
    }

    func applicationShouldHandleReopen(_ app: NSApplication, hasVisibleWindows: Bool) -> Bool {
        window.makeKeyAndOrderFront(nil); return true
    }

    // Without an Edit menu, Cmd+C / Cmd+V / Cmd+A do nothing in a WKWebView.
    func buildMenu() {
        let main = NSMenu()
        func sub(_ title: String, _ items: [NSMenuItem]) {
            let item = NSMenuItem(); let m = NSMenu(title: title)
            items.forEach(m.addItem); item.submenu = m; main.addItem(item)
        }
        func it(_ t: String, _ a: Selector?, _ k: String, _ mods: NSEvent.ModifierFlags = .command) -> NSMenuItem {
            let i = NSMenuItem(title: t, action: a, keyEquivalent: k); i.keyEquivalentModifierMask = mods; return i
        }
        sub("Loft", [
            it("Hide Loft", #selector(NSApplication.hide(_:)), "h"),
            it("Hide Others", #selector(NSApplication.hideOtherApplications(_:)), "h", [.command, .option]),
            .separator(),
            it("Quit Loft", #selector(NSApplication.terminate(_:)), "q"),
        ])
        sub("Edit", [
            it("Undo", Selector(("undo:")), "z"),
            it("Redo", Selector(("redo:")), "z", [.command, .shift]),
            .separator(),
            it("Cut", #selector(NSText.cut(_:)), "x"),
            it("Copy", #selector(NSText.copy(_:)), "c"),
            it("Paste", #selector(NSText.paste(_:)), "v"),
            it("Select All", #selector(NSText.selectAll(_:)), "a"),
        ])
        sub("View", [it("Reload", #selector(WKWebView.reload(_:)), "r")])
        sub("Window", [
            it("Minimize", #selector(NSWindow.miniaturize(_:)), "m"),
            it("Close", #selector(NSWindow.performClose(_:)), "w"),
        ])
        NSApp.mainMenu = main
    }
}

let app = NSApplication.shared
let delegate = App()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()

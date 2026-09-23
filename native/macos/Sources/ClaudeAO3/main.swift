import AppKit
import Darwin
import SwiftTerm

/// A native terminal host. Reading, input routing, persistence, and source access
/// remain owned by the bundled Python/Textual application.
final class ReaderApplication: NSObject, NSApplicationDelegate, NSWindowDelegate,
    LocalProcessTerminalViewDelegate {
    private let productName = "Claude 凹3"
    private let initialFontSize: CGFloat = 16
    private let shutdownGracePeriod: TimeInterval = 8
    private var window: NSWindow!
    private var terminal: LocalProcessTerminalView!
    private var childPID: pid_t?
    private var childExitConfirmed = false
    private var shutdownRequested = false
    private var terminationReplyPending = false
    private var shutdownTimer: Timer?
    private var shutdownAlert: NSAlert?

    // SwiftTerm can report PTY EOF before its child-exit event. Do not equate
    // process.running == false (EOF) with a safely reaped reader process.
    private var hasLiveChild: Bool { childPID != nil && !childExitConfirmed }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        NSApp.appearance = NSAppearance(named: .darkAqua)
        makeMenus()
        makeWindow()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        window.makeFirstResponder(terminal)
        // Start after AppKit has laid out the terminal's actual rows/columns.
        DispatchQueue.main.async { [weak self] in self?.startReader() }
    }

    private func makeWindow() {
        let size = NSSize(width: 980, height: 660)
        window = NSWindow(contentRect: NSRect(origin: .zero, size: size),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = productName
        window.minSize = NSSize(width: 540, height: 360)
        window.isReleasedWhenClosed = false
        window.tabbingMode = .disallowed
        window.delegate = self
        window.backgroundColor = NSColor(calibratedWhite: 0.125, alpha: 1)
        window.center()

        terminal = LocalProcessTerminalView(frame: NSRect(origin: .zero, size: size))
        terminal.autoresizingMask = [.width, .height]
        terminal.processDelegate = self
        terminal.font = NSFont.monospacedSystemFont(ofSize: initialFontSize, weight: .regular)
        terminal.nativeBackgroundColor = NSColor(calibratedWhite: 0.125, alpha: 1)
        terminal.nativeForegroundColor = NSColor(calibratedWhite: 0.90, alpha: 1)
        // Let AppKit's NSTextInputClient implementation handle Chinese IMEs;
        // Option remains available for text composition instead of Meta bytes.
        terminal.optionAsMetaKey = false
        window.contentView = terminal
    }

    private func startReader() {
        guard let resources = Bundle.main.resourceURL else {
            showLaunchFailure("找不到应用资源。请重新下载完整的 Claude 凹3应用。")
            return
        }
        let executable = resources.appendingPathComponent("reader", isDirectory: true)
            .appendingPathComponent("claude-ao3", isDirectory: false)
        guard FileManager.default.isExecutableFile(atPath: executable.path) else {
            showLaunchFailure("阅读组件缺失或无法运行。请重新下载完整应用后再试。")
            return
        }
        var environment = ProcessInfo.processInfo.environment
        environment["TERM"] = "xterm-256color"
        environment["COLORTERM"] = "truecolor"
        environment["COLORFGBG"] = "15;0"
        environment["PYTHONUTF8"] = "1"
        environment["PYTHONUNBUFFERED"] = "1"
        if environment["LANG", default: ""].isEmpty {
            environment["LANG"] = "en_US.UTF-8"
        }
        let arguments = Array(CommandLine.arguments.dropFirst()).filter { !$0.hasPrefix("-psn_") }
        // The bundle path and every argument are separate argv elements. There
        // is no shell, command interpolation, or fallback to a system Python.
        terminal.startProcess(executable: executable.path, args: arguments,
                              environment: environment.map { "\($0.key)=\($0.value)" },
                              execName: "claude-ao3")
        guard terminal.process.shellPid > 0 else {
            showLaunchFailure("无法启动阅读器。请关闭应用后重试，或重新下载完整应用。")
            return
        }
        childPID = terminal.process.shellPid
    }

    private func showLaunchFailure(_ message: String) {
        terminal.feed(text: message + "\r\n")
        window.subtitle = "阅读器未能启动"
        // Leave the explanation visible; closing this window now is safe.
        childExitConfirmed = true
    }

    private func makeMenus() {
        let main = NSMenu()
        let appItem = NSMenuItem(title: productName, action: nil, keyEquivalent: "")
        let appMenu = NSMenu(title: productName)
        appMenu.addItem(withTitle: "关于 \(productName)",
                        action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "隐藏 \(productName)", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "退出 \(productName)", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        main.addItem(appItem)

        let editItem = NSMenuItem(title: "编辑", action: nil, keyEquivalent: "")
        let edit = NSMenu(title: "编辑")
        edit.addItem(withTitle: "复制", action: #selector(TerminalView.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "粘贴", action: #selector(TerminalView.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "全选", action: #selector(TerminalView.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = edit
        main.addItem(editItem)

        let viewItem = NSMenuItem(title: "显示", action: nil, keyEquivalent: "")
        let view = NSMenu(title: "显示")
        for (title, action, key) in [
            ("增大字号", #selector(increaseFont(_:)), "+"),
            ("减小字号", #selector(decreaseFont(_:)), "-"),
            ("恢复默认字号", #selector(resetFont(_:)), "0")
        ] {
            let item = NSMenuItem(title: title, action: action, keyEquivalent: key)
            item.target = self
            view.addItem(item)
        }
        viewItem.submenu = view
        main.addItem(viewItem)

        let windowItem = NSMenuItem(title: "窗口", action: nil, keyEquivalent: "")
        let windowMenu = NSMenu(title: "窗口")
        windowMenu.addItem(withTitle: "最小化", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        windowMenu.addItem(withTitle: "缩放", action: #selector(NSWindow.performZoom(_:)), keyEquivalent: "")
        windowItem.submenu = windowMenu
        main.addItem(windowItem)
        NSApp.windowsMenu = windowMenu
        NSApp.mainMenu = main
    }

    @objc private func increaseFont(_ sender: Any?) { changeFont(to: terminal.font.pointSize + 1) }
    @objc private func decreaseFont(_ sender: Any?) { changeFont(to: terminal.font.pointSize - 1) }
    @objc private func resetFont(_ sender: Any?) { changeFont(to: initialFontSize) }

    private func changeFont(to size: CGFloat) {
        terminal.font = NSFont.monospacedSystemFont(ofSize: min(36, max(10, size)), weight: .regular)
        window.makeFirstResponder(terminal)
        // SwiftTerm recalculates cells and sets TIOCSWINSZ on the child's PTY.
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        guard hasLiveChild else { return true }
        requestGracefulShutdown()
        return false
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard hasLiveChild else { return .terminateNow }
        terminationReplyPending = true
        requestGracefulShutdown()
        return .terminateLater
    }

    private func requestGracefulShutdown() {
        guard hasLiveChild, let pid = childPID else { return }
        if !shutdownRequested {
            shutdownRequested = true
            window.subtitle = "正在保存阅读进度…"
            // Do NOT call SwiftTerm.terminate(): v1.15 closes the PTY immediately.
            // Keeping it open lets Textual/CLI handle SIGTERM and finish saving.
            if Darwin.kill(pid, SIGTERM) != 0 && errno != ESRCH {
                window.subtitle = "暂时无法结束阅读器"
            }
        }
        scheduleShutdownPrompt()
    }

    private func scheduleShutdownPrompt() {
        shutdownTimer?.invalidate()
        shutdownTimer = Timer.scheduledTimer(withTimeInterval: shutdownGracePeriod, repeats: false) { [weak self] _ in
            self?.showShutdownPrompt()
        }
    }

    private func showShutdownPrompt() {
        guard hasLiveChild, shutdownAlert == nil else { return }
        let alert = NSAlert()
        alert.messageText = "阅读器仍在结束运行"
        alert.informativeText = "继续等待可让阅读器完成保存。强制退出可能丢失最近的阅读进度。"
        alert.alertStyle = .warning
        alert.addButton(withTitle: "继续等待")
        alert.addButton(withTitle: "强制退出")
        shutdownAlert = alert
        alert.beginSheetModal(for: window) { [weak self] response in
            guard let self else { return }
            self.shutdownAlert = nil
            guard self.hasLiveChild else { return }
            if response == .alertSecondButtonReturn, let pid = self.childPID {
                // Only this explicit user choice permits SIGKILL. Still wait
                // for the actual child-exit event before terminating the host.
                _ = Darwin.kill(pid, SIGKILL)
                self.window.subtitle = "正在结束阅读器…"
            }
            self.scheduleShutdownPrompt()
        }
    }

    func processTerminated(source: TerminalView, exitCode: Int32?) {
        DispatchQueue.main.async { [weak self] in
            guard let self, !self.childExitConfirmed else { return }
            self.childExitConfirmed = true
            self.shutdownTimer?.invalidate()
            if let alert = self.shutdownAlert {
                self.window.endSheet(alert.window)
                self.shutdownAlert = nil
            }
            if exitCode == 0 {
                if self.terminationReplyPending {
                    NSApp.reply(toApplicationShouldTerminate: true)
                } else {
                    NSApp.terminate(nil)
                }
            } else {
                // A close request must not hide a failed final save. Cancel
                // Cmd+Q's pending reply and keep the child's error on screen.
                self.shutdownRequested = false
                if self.terminationReplyPending {
                    self.terminationReplyPending = false
                    NSApp.reply(toApplicationShouldTerminate: false)
                }
                // v1.15 forwards waitpid status. Keep stderr visible on lock
                // conflicts and other failures rather than silently closing.
                if let status = exitCode, status & 0x7f == 0 {
                    self.window.subtitle = "阅读器已退出（代码 \((status >> 8) & 0xff)）"
                } else {
                    self.window.subtitle = "阅读器已结束，请查看终端提示"
                }
            }
        }
    }

    func sizeChanged(source: LocalProcessTerminalView, newCols: Int, newRows: Int) {
        // LocalProcessTerminalView already propagates this to TIOCSWINSZ.
    }

    func setTerminalTitle(source: LocalProcessTerminalView, title: String) {
        // Keep a stable product title, including when Ctrl+G hides reading.
    }

    func hostCurrentDirectoryUpdate(source: TerminalView, directory: String?) {}
}

let application = NSApplication.shared
let applicationDelegate = ReaderApplication()
application.delegate = applicationDelegate
application.run()

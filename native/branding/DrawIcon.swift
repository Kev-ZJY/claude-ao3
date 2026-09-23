import AppKit
import Foundation

// Original terminal-style icon. It does not copy the system Terminal artwork.
let destination = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
try FileManager.default.createDirectory(at: destination, withIntermediateDirectories: true)
let sizes = [(16,1),(16,2),(32,1),(32,2),(128,1),(128,2),(256,1),(256,2),(512,1),(512,2)]
for (base, scale) in sizes {
    let pixels = base * scale
    let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: pixels,
        pixelsHigh: pixels, bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true,
        isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: bitmap)
    let transform = NSAffineTransform()
    transform.scale(by: CGFloat(pixels) / 1024)
    transform.concat()
    let outer = NSBezierPath(roundedRect: NSRect(x: 66, y: 66, width: 892, height: 892), xRadius: 196, yRadius: 196)
    let shadow = NSShadow()
    shadow.shadowColor = NSColor.black.withAlphaComponent(0.28)
    shadow.shadowBlurRadius = 30
    shadow.shadowOffset = NSSize(width: 0, height: -12)
    shadow.set()
    NSColor(calibratedWhite: 0.24, alpha: 1).setFill()
    outer.fill()
    NSShadow().set()
    NSGradient(starting: NSColor(calibratedWhite: 0.36, alpha: 1), ending: NSColor(calibratedWhite: 0.11, alpha: 1))!.draw(in: outer, angle: -90)
    let inner = NSBezierPath(roundedRect: NSRect(x: 93, y: 93, width: 838, height: 838), xRadius: 170, yRadius: 170)
    NSGradient(starting: NSColor(calibratedWhite: 0.15, alpha: 1), ending: NSColor(calibratedWhite: 0.055, alpha: 1))!.draw(in: inner, angle: -90)
    NSColor(calibratedWhite: 0.70, alpha: 0.6).setStroke()
    outer.lineWidth = 3
    outer.stroke()
    let prompt = NSBezierPath()
    prompt.move(to: NSPoint(x: 252, y: 689))
    prompt.line(to: NSPoint(x: 401, y: 552))
    prompt.line(to: NSPoint(x: 252, y: 416))
    prompt.lineWidth = 54
    prompt.lineCapStyle = .round
    prompt.lineJoinStyle = .round
    NSColor(calibratedWhite: 0.94, alpha: 1).setStroke()
    prompt.stroke()
    let caret = NSBezierPath(roundedRect: NSRect(x: 454, y: 394, width: 244, height: 47), xRadius: 10, yRadius: 10)
    NSColor(calibratedRed: 0.81, green: 0.46, blue: 0.34, alpha: 1).setFill()
    caret.fill()
    NSGraphicsContext.restoreGraphicsState()
    let name = "icon_\(base)x\(base)\(scale == 2 ? "@2x" : "").png"
    try bitmap.representation(using: .png, properties: [:])!.write(to: destination.appendingPathComponent(name))
}

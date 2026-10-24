// Draws macmesh's app icon (two Macs linked by a spark) at 1024 px: swift scripts/make-icon.swift OUT.png
import AppKit

let size: CGFloat = 1024
let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(size), pixelsHigh: Int(size), bitsPerSample: 8,
                           samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
                           bytesPerRow: 0, bitsPerPixel: 0)!
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
let ctx = NSGraphicsContext.current!.cgContext

// The tile: macOS's icon grid (824 px square, 185 px corners) with a soft shadow.
let tile = NSRect(x: 100, y: 100, width: 824, height: 824)
let tilePath = NSBezierPath(roundedRect: tile, xRadius: 185, yRadius: 185)
ctx.saveGState()
ctx.setShadow(offset: CGSize(width: 0, height: -12), blur: 28, color: NSColor.black.withAlphaComponent(0.35).cgColor)
NSColor.black.setFill()
tilePath.fill()
ctx.restoreGState()
tilePath.addClip()
NSGradient(colors: [NSColor(srgbRed: 0.10, green: 0.16, blue: 0.42, alpha: 1),
                    NSColor(srgbRed: 0.12, green: 0.42, blue: 0.70, alpha: 1),
                    NSColor(srgbRed: 0.18, green: 0.74, blue: 0.78, alpha: 1)])!
    .draw(in: tile, angle: -60)
// A faint glow behind the link.
NSGradient(colors: [NSColor.white.withAlphaComponent(0.22), NSColor.white.withAlphaComponent(0)])!
    .draw(fromCenter: NSPoint(x: 512, y: 540), radius: 0, toCenter: NSPoint(x: 512, y: 540), radius: 320, options: [])

func mac(_ x: CGFloat) {
    // A display: bezel, screen, stand and foot.
    let body = NSRect(x: x, y: 450, width: 270, height: 196)
    NSColor.white.setFill()
    NSBezierPath(roundedRect: body, xRadius: 24, yRadius: 24).fill()
    let screen = NSRect(x: x + 15, y: 471, width: 240, height: 160)
    NSGradient(colors: [NSColor(srgbRed: 0.16, green: 0.30, blue: 0.62, alpha: 1),
                        NSColor(srgbRed: 0.20, green: 0.62, blue: 0.80, alpha: 1)])!
        .draw(in: NSBezierPath(roundedRect: screen, xRadius: 11, yRadius: 11), angle: -70)
    let stand = NSBezierPath()
    stand.move(to: NSPoint(x: x + 110, y: 450))
    stand.line(to: NSPoint(x: x + 160, y: 450))
    stand.line(to: NSPoint(x: x + 170, y: 388))
    stand.line(to: NSPoint(x: x + 100, y: 388))
    stand.close()
    NSColor.white.withAlphaComponent(0.92).setFill()
    stand.fill()
    NSBezierPath(roundedRect: NSRect(x: x + 72, y: 374, width: 126, height: 17), xRadius: 8.5, yRadius: 8.5).fill()
}
mac(150)
mac(604)

// The link: a bright band from screen to screen, broken by a spark in the middle.
let gold = NSColor(srgbRed: 1.0, green: 0.84, blue: 0.32, alpha: 1)
ctx.saveGState()
ctx.setShadow(offset: .zero, blur: 32, color: NSColor(srgbRed: 1, green: 0.85, blue: 0.3, alpha: 0.95).cgColor)
for (a, b) in [(420.0, 470.0), (554.0, 604.0)] {
    let band = NSBezierPath()
    band.move(to: NSPoint(x: a, y: 551))
    band.line(to: NSPoint(x: b, y: 551))
    band.lineWidth = 20
    band.lineCapStyle = .round
    gold.setStroke()
    band.stroke()
}
let bolt = NSBezierPath()
for (i, p) in [(540, 660), (482, 548), (520, 548), (494, 438), (560, 565), (522, 565)].enumerated() {
    let pt = NSPoint(x: CGFloat(p.0), y: CGFloat(p.1))
    if i == 0 { bolt.move(to: pt) } else { bolt.line(to: pt) }
}
bolt.close()
NSColor(srgbRed: 1.0, green: 0.93, blue: 0.58, alpha: 1).setFill()
bolt.fill()
ctx.restoreGState()

let png = rep.representation(using: .png, properties: [:])!
try! png.write(to: URL(fileURLWithPath: CommandLine.arguments[1]))

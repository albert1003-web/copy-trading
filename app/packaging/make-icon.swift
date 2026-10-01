// Draws the Trade Tracker app icon as a 1024x1024 PNG.
// Usage: swift make-icon.swift out.png   (package-mac.sh turns it into TradeTracker.icns)
import AppKit

let size: CGFloat = 1024
let out = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "icon.png"

let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(size), pixelsHigh: Int(size),
                           bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                           colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
let ctx = NSGraphicsContext.current!.cgContext

func rgb(_ hex: UInt32, _ a: CGFloat = 1) -> NSColor {
    NSColor(srgbRed: CGFloat((hex >> 16) & 0xff) / 255, green: CGFloat((hex >> 8) & 0xff) / 255,
            blue: CGFloat(hex & 0xff) / 255, alpha: a)
}

// macOS icon grid: 824pt rounded square centered in the 1024 canvas, with a soft drop shadow.
let tile = NSRect(x: 100, y: 100, width: 824, height: 824)
let tilePath = NSBezierPath(roundedRect: tile, xRadius: 185, yRadius: 185)
ctx.saveGState()
ctx.setShadow(offset: CGSize(width: 0, height: -12), blur: 28, color: NSColor.black.withAlphaComponent(0.35).cgColor)
rgb(0x14254f).setFill()
tilePath.fill()
ctx.restoreGState()

// Background: deep navy gradient.
ctx.saveGState()
tilePath.addClip()
NSGradient(starting: rgb(0x2b4fb8), ending: rgb(0x0f1d44))!.draw(in: tile, angle: -90)

// Faint grid.
rgb(0xffffff, 0.08).setStroke()
for i in 1...4 {
    let y = tile.minY + CGFloat(i) * tile.height / 5
    let line = NSBezierPath()
    line.move(to: NSPoint(x: tile.minX, y: y))
    line.line(to: NSPoint(x: tile.maxX, y: y))
    line.lineWidth = 4
    line.stroke()
}

// Volume bars along the bottom.
let bars: [CGFloat] = [70, 120, 90, 150, 110, 190, 140]
let barW: CGFloat = 54
for (i, h) in bars.enumerated() {
    let x = tile.minX + 120 + CGFloat(i) * 88
    rgb(0xffffff, 0.16).setFill()
    NSBezierPath(roundedRect: NSRect(x: x, y: tile.minY + 110, width: barW, height: h), xRadius: 10, yRadius: 10).fill()
}

// Rising price line with a soft area fill.
let pts: [NSPoint] = [(0, 360), (110, 430), (210, 395), (320, 520), (420, 480), (520, 610), (640, 700)]
    .map { NSPoint(x: tile.minX + 92 + $0.0, y: tile.minY + $0.1) }
let area = NSBezierPath()
area.move(to: NSPoint(x: pts[0].x, y: tile.minY))
pts.forEach { area.line(to: $0) }
area.line(to: NSPoint(x: pts.last!.x, y: tile.minY))
area.close()
NSGradient(starting: rgb(0x4ade80, 0.38), ending: rgb(0x4ade80, 0.0))!.draw(in: area, angle: -90)

let line = NSBezierPath()
line.move(to: pts[0])
pts.dropFirst().forEach { line.line(to: $0) }
line.lineWidth = 34
line.lineCapStyle = .round
line.lineJoinStyle = .round
rgb(0xffffff).setStroke()
line.stroke()

// Highlight dot at the latest point.
let end = pts.last!
rgb(0x4ade80).setFill()
NSBezierPath(ovalIn: NSRect(x: end.x - 46, y: end.y - 46, width: 92, height: 92)).fill()
rgb(0xffffff).setStroke()
let ring = NSBezierPath(ovalIn: NSRect(x: end.x - 46, y: end.y - 46, width: 92, height: 92))
ring.lineWidth = 14
ring.stroke()

// Subtle top sheen.
NSGradient(starting: rgb(0xffffff, 0.0), ending: rgb(0xffffff, 0.12))!
    .draw(in: NSRect(x: tile.minX, y: tile.midY, width: tile.width, height: tile.height / 2), angle: 90)
ctx.restoreGState()

NSGraphicsContext.restoreGraphicsState()
try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: out))
print("wrote \(out)")

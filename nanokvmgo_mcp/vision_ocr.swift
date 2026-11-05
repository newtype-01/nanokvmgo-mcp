import Foundation
import Vision
import ImageIO

// Capability probing takes no screenshot and runs before any device input.
if CommandLine.arguments.contains("--check") {
    do {
        let request = VNRecognizeTextRequest()
        let languages = try request.supportedRecognitionLanguages()
        let available = languages.contains("zh-Hans") && languages.contains("en-US")
        let result: [String: Any] = ["ok": available, "languages": languages]
        let data = try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys])
        FileHandle.standardOutput.write(data)
        FileHandle.standardOutput.write(Data([10]))
    } catch {
        print("{\"ok\":false,\"languages\":[]}")
    }
    exit(0)
}

struct Input: Decodable {
    let id: String?
    let path: String
}

struct TextLine: Encodable {
    let text: String
    let confidence: Float
    // Normalized source-image coordinates: left, top, right, bottom.
    let box: [Double]
}

struct Output: Encodable {
    let id: String?
    let ok: Bool
    let lines: [TextLine]
    let elapsed_ms: Double
    let error: String?
}

func recognize(_ input: Input) throws -> [TextLine] {
    let url = URL(fileURLWithPath: input.path)
    guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
          let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any],
          let width = properties[kCGImagePropertyPixelWidth] as? Int,
          let height = properties[kCGImagePropertyPixelHeight] as? Int,
          width > 0, height > 0, width <= 20000, height <= 20000,
          width * height <= 20_000_000,
          let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
        throw NSError(domain: "LocalOCR", code: 1)
    }
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = false
    let supported = try request.supportedRecognitionLanguages()
    guard supported.contains("zh-Hans") else {
        throw NSError(domain: "LocalOCR", code: 2)
    }
    request.recognitionLanguages = ["zh-Hans", "en-US"]
    request.minimumTextHeight = 0.008
    try VNImageRequestHandler(cgImage: image, orientation: .up).perform([request])
    return (request.results ?? []).compactMap { observation in
        guard let text = observation.topCandidates(1).first else { return nil }
        let rect = observation.boundingBox
        return TextLine(text: text.string, confidence: text.confidence,
                        box: [rect.minX, 1 - rect.maxY, rect.maxX, 1 - rect.minY])
    }.sorted {
        let firstRow = Int(($0.box[1] / 0.008).rounded())
        let secondRow = Int(($1.box[1] / 0.008).rounded())
        if firstRow != secondRow { return firstRow < secondRow }
        return $0.box[0] < $1.box[0]
    }
}

while let line = readLine() {
    let started = ProcessInfo.processInfo.systemUptime
    let output: Output = autoreleasepool {
        var requestID: String? = nil
        do {
            guard line.utf8.count <= 16384, let data = line.data(using: .utf8) else {
                throw NSError(domain: "LocalOCR", code: 3)
            }
            let input = try JSONDecoder().decode(Input.self, from: data)
            requestID = input.id
            let lines = try recognize(input)
            return Output(id: requestID, ok: true, lines: lines,
                          elapsed_ms: (ProcessInfo.processInfo.systemUptime - started) * 1000,
                          error: nil)
        } catch {
            let diagnostic = error as NSError
            return Output(id: requestID, ok: false, lines: [],
                          elapsed_ms: (ProcessInfo.processInfo.systemUptime - started) * 1000,
                          error: "Local text recognition failed (\(diagnostic.domain):\(diagnostic.code))")
        }
    }
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.sortedKeys]
    if let data = try? encoder.encode(output) {
        FileHandle.standardOutput.write(data)
        FileHandle.standardOutput.write(Data([10]))
    }
}

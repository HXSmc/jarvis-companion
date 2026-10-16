import Foundation

/// Splits a chat reply into markdown blocks the chat view renders one by one.
/// Inline marks (**bold**, *italic*, `code`, links) stay inside each block's
/// text and are rendered by AttributedString's inline markdown parser; this
/// only handles the line-level syntax that parser leaves as raw characters
/// (headings, bullets, numbered items, code fences, rules). Pure Foundation
/// so it runs in the headless test (ios/tests/run_chat_markdown_tests.sh).
enum ChatMarkdown {
    enum Block: Equatable {
        case paragraph(String)
        case heading(String)
        case bullet(String)
        case numbered(marker: String, text: String)
        case code(String)
        case rule
    }

    static func blocks(_ text: String) -> [Block] {
        var out: [Block] = []
        var paragraph: [String] = []
        var code: [String]?

        func flush() {
            if !paragraph.isEmpty { out.append(.paragraph(paragraph.joined(separator: "\n"))) }
            paragraph = []
        }

        for line in text.components(separatedBy: "\n") {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if trimmed.hasPrefix("```") {
                if let lines = code {
                    out.append(.code(lines.joined(separator: "\n")))
                    code = nil
                } else {
                    flush()
                    code = []
                }
                continue
            }
            if code != nil {
                code?.append(line)
                continue
            }
            if trimmed.isEmpty {
                flush()
            } else if let rest = match(trimmed, #"^#{1,6}\s+(.*)$"#) {
                flush()
                out.append(.heading(rest[0]))
            } else if trimmed == "---" || trimmed == "***" || trimmed == "___" {
                flush()
                out.append(.rule)
            } else if let rest = match(trimmed, #"^[-*+]\s+(.*)$"#) {
                flush()
                out.append(.bullet(rest[0]))
            } else if let rest = match(trimmed, #"^(\d{1,3}[.)])\s+(.*)$"#) {
                flush()
                out.append(.numbered(marker: rest[0], text: rest[1]))
            } else {
                paragraph.append(line)
            }
        }
        // An unclosed fence still shows its lines as code.
        if let lines = code { out.append(.code(lines.joined(separator: "\n"))) }
        flush()
        return out
    }

    /// Capture groups of a whole-line regex match, or nil.
    private static func match(_ text: String, _ pattern: String) -> [String]? {
        guard let regex = try? NSRegularExpression(pattern: pattern),
              let found = regex.firstMatch(in: text, range: NSRange(text.startIndex..., in: text))
        else { return nil }
        return (1..<found.numberOfRanges).map { index in
            Range(found.range(at: index), in: text).map { String(text[$0]) } ?? ""
        }
    }
}

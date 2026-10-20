import Foundation

/// Splits a chat reply into markdown blocks the chat view renders one by one.
/// Inline marks (**bold**, *italic*, `code`, links) stay inside each block's
/// text and are rendered by AttributedString's inline markdown parser; this
/// only handles the line-level syntax that parser leaves as raw characters
/// (headings, bullets, numbered items, code fences, rules, pipe tables). Pure Foundation
/// so it runs in the headless test (ios/tests/run_chat_markdown_tests.sh).
enum ChatMarkdown {
    enum Block: Equatable {
        case paragraph(String)
        case heading(String)
        case bullet(String)
        case numbered(marker: String, text: String)
        case code(String)
        case rule
        /// GFM pipe table; every row is padded or cut to the header's width.
        case table(header: [String], rows: [[String]])
    }

    static func blocks(_ text: String) -> [Block] {
        var out: [Block] = []
        var paragraph: [String] = []
        var code: [String]?

        func flush() {
            if !paragraph.isEmpty { out.append(.paragraph(paragraph.joined(separator: "\n"))) }
            paragraph = []
        }

        let lines = text.components(separatedBy: "\n")
        var index = 0
        while index < lines.count {
            let line = lines[index]
            index += 1
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
            } else if index < lines.count, let header = cells(trimmed), isDelimiter(lines[index], columns: header.count) {
                // GFM table: header row, then a |---|---| row, then rows until a line with no pipe.
                flush()
                index += 1
                var rows: [[String]] = []
                while index < lines.count, let row = cells(lines[index].trimmingCharacters(in: .whitespaces)) {
                    rows.append(Array((row + Array(repeating: "", count: header.count)).prefix(header.count)))
                    index += 1
                }
                out.append(.table(header: header, rows: rows))
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

    /// Cells of a table row, or nil if the line has no unescaped pipe.
    /// `\|` stays a literal pipe inside a cell.
    private static func cells(_ line: String) -> [String]? {
        var parts: [String] = []
        var current = ""
        var escaped = false
        for char in line {
            if escaped {
                current.append(char == "|" ? "|" : "\\\(char)")
                escaped = false
            } else if char == "\\" {
                escaped = true
            } else if char == "|" {
                parts.append(current)
                current = ""
            } else {
                current.append(char)
            }
        }
        if escaped { current.append("\\") }
        guard !parts.isEmpty else { return nil }
        parts.append(current)
        if line.hasPrefix("|") { parts.removeFirst() }
        if line.hasSuffix("|") && !line.hasSuffix("\\|") { parts.removeLast() }
        return parts.map { $0.trimmingCharacters(in: .whitespaces) }
    }

    /// A |---|:--:| row with one dash cell per header column.
    private static func isDelimiter(_ line: String, columns: Int) -> Bool {
        guard let parts = cells(line.trimmingCharacters(in: .whitespaces)), parts.count == columns else { return false }
        return parts.allSatisfy { match($0, #"^:?-+:?$"#) != nil }
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

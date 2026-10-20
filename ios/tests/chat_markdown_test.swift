import Foundation

// Headless check for ChatMarkdown.blocks (run_chat_markdown_tests.sh).
// Inline marks are left in the text on purpose: the view's AttributedString
// inline parser renders them; this test also proves that parser turns
// **h** into bold "h" with no stars left.

@main
enum ChatMarkdownTest {
    static func main() {
        var failures = 0
        func check(_ name: String, _ got: [ChatMarkdown.Block], _ want: [ChatMarkdown.Block]) {
            if got != want {
                failures += 1
                print("FAIL \(name)\n  got:  \(got)\n  want: \(want)")
            }
        }

        check("plain keeps inline marks", ChatMarkdown.blocks("make **h** bold"), [.paragraph("make **h** bold")])
        check("lines join, blank splits",
              ChatMarkdown.blocks("one\ntwo\n\nthree"), [.paragraph("one\ntwo"), .paragraph("three")])
        check("heading", ChatMarkdown.blocks("## Plan\nbody"), [.heading("Plan"), .paragraph("body")])
        check("bullets", ChatMarkdown.blocks("- a\n* b\n  + c"), [.bullet("a"), .bullet("b"), .bullet("c")])
        check("bold line is not a bullet", ChatMarkdown.blocks("**Done** now"), [.paragraph("**Done** now")])
        check("numbered", ChatMarkdown.blocks("1. x\n2) y"),
              [.numbered(marker: "1.", text: "x"), .numbered(marker: "2)", text: "y")])
        check("code fence keeps marks raw", ChatMarkdown.blocks("```swift\nlet **a** = 1\n- no\n```\nafter"),
              [.code("let **a** = 1\n- no"), .paragraph("after")])
        check("unclosed fence", ChatMarkdown.blocks("```\nx"), [.code("x")])
        check("rule", ChatMarkdown.blocks("a\n---\nb"), [.paragraph("a"), .rule, .paragraph("b")])
        check("empty", ChatMarkdown.blocks(""), [])
        check("table", ChatMarkdown.blocks("intro\n| Need | Pick |\n|---|:--:|\n| **Cloud** | Sonnet |\n| Riyadh | Qwen |\n\nafter"),
              [.paragraph("intro"),
               .table(header: ["Need", "Pick"], rows: [["**Cloud**", "Sonnet"], ["Riyadh", "Qwen"]]),
               .paragraph("after")])
        check("table ends at line without pipe", ChatMarkdown.blocks("| a | b |\n|---|---|\n| 1 | 2 |\nplain"),
              [.table(header: ["a", "b"], rows: [["1", "2"]]), .paragraph("plain")])
        check("no outer pipes, ragged rows pad and cut", ChatMarkdown.blocks("a | b\n--- | ---\n1 |\n1 | 2 | 3"),
              [.table(header: ["a", "b"], rows: [["1", ""], ["1", "2"]])])
        check("escaped pipe stays in cell", ChatMarkdown.blocks("| a | b |\n|---|---|\n| x \\| y | z |"),
              [.table(header: ["a", "b"], rows: [["x | y", "z"]])])
        check("pipe line without delimiter is text", ChatMarkdown.blocks("a | b\nc"), [.paragraph("a | b\nc")])
        check("column count mismatch is not a table", ChatMarkdown.blocks("| a | b |\n|---|"),
              [.paragraph("| a | b |\n|---|")])

        // Linux Foundation has no markdown AttributedString; this half runs on the Mac.
        #if canImport(Darwin)
        let inline = AttributedString.MarkdownParsingOptions(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        if let rendered = try? AttributedString(markdown: "make **h** bold", options: inline) {
            let plain = String(rendered.characters)
            let boldRun = rendered.runs.first { $0.inlinePresentationIntent?.contains(.stronglyEmphasized) == true }
            if plain != "make h bold" || boldRun.map({ String(rendered[$0.range].characters) }) != "h" {
                failures += 1
                print("FAIL inline bold: \(plain)")
            }
        } else {
            failures += 1
            print("FAIL inline parser threw")
        }
        #endif

        if failures == 0 { print("ok") } else { exit(1) }
    }
}

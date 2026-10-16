#!/bin/bash
# Compiles ChatMarkdown.swift together with its headless test and runs it.
# Prints "ok" and exits 0 only if every scenario passes.
set -euo pipefail
cd "$(dirname "$0")/.."
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
swiftc -o "$tmp/chat_markdown_test" \
    JarvisCompanion/JarvisCompanion/ChatMarkdown.swift \
    tests/chat_markdown_test.swift
"$tmp/chat_markdown_test"

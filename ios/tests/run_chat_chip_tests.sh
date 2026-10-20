#!/bin/bash
# Compiles ChatChip.swift together with its headless test and runs it.
# Prints "ok" and exits 0 only if every scenario passes.
set -euo pipefail
cd "$(dirname "$0")/.."
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
swiftc -o "$tmp/chat_chip_test" \
    JarvisCompanion/JarvisCompanion/ChatChip.swift \
    tests/chat_chip_test.swift
"$tmp/chat_chip_test"

// Cursor-mode pure logic for the Remote Screen Control page. Inlined into
// screen.html at serve time (browser: plain functions, no module syntax) and
// loaded by tests/test_screen_cursor.py under node via module.exports.
// No DOM, no fetch — everything here is testable without a browser.
"use strict";

var GAIN = 1.5;          // finger CSS px -> cursor CSS px
var TAP_MS = 300;        // tap = hold at most this long...
var TAP_DIST_CSS = 8;    // ...and move at most this far (CSS px)
var LONGPRESS_MS = 500;  // held this long without moving -> right-click
var SLEEP_CONFIRM_MS = 3000;  // Sleep button: second tap must land within this

function initialCursor(frameW, frameH) {
  return { x: Math.round(frameW / 2), y: Math.round(frameH / 2), frameW: frameW, frameH: frameH };
}

function nextCursor(prev, dxCss, dyCss, gain, scale, frameW, frameH) {
  // scale = displayed CSS px per frame px; never mutates prev.
  var dxFrame = dxCss * gain / scale;
  var dyFrame = dyCss * gain / scale;
  return {
    x: Math.min(Math.max(Math.round(prev.x + dxFrame), 0), frameW - 1),
    y: Math.min(Math.max(Math.round(prev.y + dyFrame), 0), frameH - 1),
    frameW: frameW,
    frameH: frameH
  };
}

function classifyGesture(dtMs, distCss) {
  return dtMs <= TAP_MS && distCss <= TAP_DIST_CSS ? "tap" : "drag";
}

function clickPayload(kind, cursor, frameW, frameH) {
  // frame px -> the protocol's normalized 0..1 space.
  return { type: kind, x: cursor.x / frameW, y: cursor.y / frameH };
}

// Two-tap confirm for the Sleep PC button: the first tap arms it for
// SLEEP_CONFIRM_MS, a second tap within that window fires. Pure — the page
// owns the timers and rendering. state is null (disarmed) or {deadline}.
function sleepTap(state, nowMs) {
  if (state && nowMs <= state.deadline)
    return { state: null, fire: true };
  return { state: { deadline: nowMs + SLEEP_CONFIRM_MS }, fire: false };
}

function sleepLabel(armed) {
  return armed ? "Confirm" : "Sleep";
}

if (typeof module !== "undefined")
  module.exports = {
    initialCursor: initialCursor, nextCursor: nextCursor,
    classifyGesture: classifyGesture, clickPayload: clickPayload,
    sleepTap: sleepTap, sleepLabel: sleepLabel,
    GAIN: GAIN, TAP_MS: TAP_MS, TAP_DIST_CSS: TAP_DIST_CSS,
    LONGPRESS_MS: LONGPRESS_MS, SLEEP_CONFIRM_MS: SLEEP_CONFIRM_MS
  };

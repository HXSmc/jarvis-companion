// The drawing card's page (jfollow10c). The sanitised SVG arrives only as the image drawing.svg:
// an SVG inside an <img> runs no script and loads nothing, and its text never becomes markup here.
// The legend comes from data.json and is set with textContent only.
"use strict";

(() => {
  const MIN = 0.5;
  const MAX = 8;
  const stage = document.getElementById("stage");
  const img = document.getElementById("drawing");
  const legend = document.getElementById("legend");
  const status = document.getElementById("status");
  const reset = document.getElementById("reset");
  let scale = 1;
  let x = 0;
  let y = 0;
  let drag = null;

  function apply() {
    img.style.transform = `translate(${x}px, ${y}px) scale(${scale})`;
  }

  function fit() {
    scale = 1;
    x = 0;
    y = 0;
    apply();
  }

  // Zoom by `factor` keeping the stage point (px, py) still.
  function zoomAt(px, py, factor) {
    const next = Math.min(MAX, Math.max(MIN, scale * factor));
    const k = next / scale;
    x = px - (px - x) * k;
    y = py - (py - y) * k;
    scale = next;
    apply();
  }

  function centre() {
    const r = stage.getBoundingClientRect();
    return [r.width / 2, r.height / 2];
  }

  function unavailable() {
    img.remove();
    status.textContent = "Drawing unavailable";
    status.hidden = false;
  }
  img.addEventListener("error", unavailable);
  img.src = img.dataset.src;  // set only after the error listener exists, so no failure is missed

  // Trackpad pinch arrives as wheel with ctrlKey; a plain scroll zooms too, more gently.
  stage.addEventListener("wheel", (e) => {
    e.preventDefault();
    const r = stage.getBoundingClientRect();
    zoomAt(e.clientX - r.left, e.clientY - r.top, Math.exp(-e.deltaY * (e.ctrlKey ? 0.01 : 0.002)));
  }, { passive: false });

  stage.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    drag = { id: e.pointerId, sx: e.clientX - x, sy: e.clientY - y };
    stage.setPointerCapture(e.pointerId);
    stage.classList.add("dragging");
  });
  stage.addEventListener("pointermove", (e) => {
    if (!drag || drag.id !== e.pointerId) return;
    x = e.clientX - drag.sx;
    y = e.clientY - drag.sy;
    apply();
  });
  const endDrag = (e) => {
    if (!drag || drag.id !== e.pointerId) return;
    drag = null;
    stage.classList.remove("dragging");
  };
  stage.addEventListener("pointerup", endDrag);
  stage.addEventListener("pointercancel", endDrag);
  stage.addEventListener("dblclick", fit);
  reset.addEventListener("click", fit);

  // Keys on the focused drawing: + and - zoom, 0 resets, arrows move.
  stage.addEventListener("keydown", (e) => {
    const step = 40;
    const moves = { ArrowLeft: [step, 0], ArrowRight: [-step, 0], ArrowUp: [0, step], ArrowDown: [0, -step] };
    if (e.key === "+" || e.key === "=") zoomAt(...centre(), 1.25);
    else if (e.key === "-") zoomAt(...centre(), 0.8);
    else if (e.key === "0") fit();
    else if (moves[e.key]) {
      x += moves[e.key][0];
      y += moves[e.key][1];
      apply();
    } else return;
    e.preventDefault();
  });

  fetch("data.json")
    .then((r) => (r.ok ? r.json() : null))
    .then((data) => {
      const items = data && Array.isArray(data.legend) ? data.legend : [];
      for (const item of items.slice(0, 12)) {
        const li = document.createElement("li");
        li.textContent = String(item);
        legend.append(li);
      }
    })
    .catch(() => {});
})();

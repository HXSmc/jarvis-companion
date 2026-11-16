// The web card's map page. Data comes only from data.json (served by the app's scheme handler);
// every label goes into the page with textContent, never as HTML. No popups, no tooltips, default pin icons only.
"use strict";

(function () {
  var ATTRIBUTION = "© OpenStreetMap contributors";
  var statusEl = document.getElementById("status");
  var legendEl = document.getElementById("legend");
  var resetEl = document.getElementById("reset");
  document.getElementById("attribution").textContent = ATTRIBUTION;

  function say(text) {
    statusEl.textContent = text;
    statusEl.hidden = false;
  }

  L.Icon.Default.imagePath = "vendor/leaflet/images/";
  var map = L.map("map", {attributionControl: false, zoomSnap: 0.25, maxZoom: 19});
  map.setView([51.5, -0.12], 12);
  var tileErrors = 0;
  L.tileLayer("tiles/{z}/{x}/{y}.png", {maxZoom: 19}).on("tileerror", function () {
    tileErrors += 1;
    if (tileErrors === 1) {
      say("Map tiles unavailable");
    }
  }).addTo(map);

  var bounds = null;

  function fit() {
    if (bounds && bounds.isValid()) {
      map.fitBounds(bounds, {padding: [28, 28], maxZoom: 16});
    }
  }

  function addLegend(cls, text) {
    var li = document.createElement("li");
    li.className = cls;
    var mark = document.createElement("span");
    mark.className = cls === "pin" ? "" : "swatch";
    li.appendChild(mark);
    var label = document.createElement("span");
    label.textContent = text;
    li.appendChild(label);
    legendEl.appendChild(li);
  }

  function draw(data) {
    var layers = [];
    var routes = Array.isArray(data.routes) ? data.routes : [];
    var pins = Array.isArray(data.pins) ? data.pins : [];
    // Alternatives first, so the best route is drawn on top.
    for (var i = routes.length - 1; i >= 0; i -= 1) {
      var best = i === 0;
      layers.push(L.polyline(routes[i].points, {
        color: best ? "#3d8bfd" : "#8b97a8",
        weight: best ? 6 : 4,
        opacity: best ? 0.95 : 0.75,
        interactive: false
      }).addTo(map));
    }
    routes.forEach(function (r, n) {
      addLegend(n === 0 ? "best" : "alt", Math.round(r.min) + " min, " + Number(r.km).toFixed(1) + " km");
    });
    pins.forEach(function (p, n) {
      layers.push(L.marker([p.lat, p.lon], {keyboard: false, interactive: false}).addTo(map));
      var role = pins.length === 2 ? (n === 0 ? "Start: " : "End: ") : "";
      addLegend("pin", role + String(p.label || ""));
    });
    if (layers.length) {
      bounds = L.featureGroup(layers).getBounds();
      fit();
    }
  }

  resetEl.addEventListener("click", fit);

  // Trackpad pinch in WebKit arrives as gesture events: zoom the map around its centre.
  var startZoom = null;
  document.addEventListener("gesturestart", function (e) {
    e.preventDefault();
    startZoom = map.getZoom();
  });
  document.addEventListener("gesturechange", function (e) {
    e.preventDefault();
    if (startZoom !== null) {
      map.setZoom(startZoom + Math.log2(e.scale), {animate: false});
    }
  });
  document.addEventListener("gestureend", function () {
    startZoom = null;
  });

  fetch("data.json").then(function (r) {
    return r.json();
  }).then(draw).catch(function () {
    say("Map data unavailable");
  });
})();

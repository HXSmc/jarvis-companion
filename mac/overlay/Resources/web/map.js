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

  // Tiles load only once the route data has arrived, so a failed data.json fetches no tiles.
  function addTiles() {
    L.tileLayer("tiles/{z}/{x}/{y}.png", {maxZoom: 19}).on("tileerror", function () {
      tileErrors += 1;
      if (tileErrors === 1) {
        say("Map tiles unavailable");
      }
    }).addTo(map);
  }

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

  // jfollow10f: credit keys from data.json map to these fixed lines; no text from the data itself.
  var CREDITS = {
    osm: ATTRIBUTION,
    tfl: "Powered by TfL Open Data",
    clusterflick: "Screening data from Clusterflick (CC BY 4.0)"
  };
  var NOTES = {times_not_listed: "times not listed", none: "no showings left"};

  function addLine(parent, cls, text) {
    var p = document.createElement("p");
    p.className = cls;
    p.textContent = text;
    parent.appendChild(p);
  }

  // The ranked places under the map: one numbered item per row, every part through textContent.
  function addPlaces(data) {
    var rows = Array.isArray(data.rows) ? data.rows : [];
    if (!rows.length) {
      return false;
    }
    document.body.classList.add("places");
    var box = document.createElement("section");
    box.id = "places";
    box.setAttribute("aria-label", "Places, best first");
    var list = document.createElement("ol");
    rows.forEach(function (r, n) {
      var li = document.createElement("li");
      addLine(li, "name", (n + 1) + ". " + String(r.name || "") + (r.road ? ", " + String(r.road) : ""));
      var facts = [];
      if (typeof r.min === "number") {
        facts.push(String(r.mode || "walk") + " " + r.min + " min");
      }
      facts.push(Number(r.km).toFixed(1) + " km");
      if (r.hours) {
        facts.push(String(r.hours));
      }
      addLine(li, "facts", facts.join(" · "));
      var times = Array.isArray(r.times) ? r.times : [];
      if (times.length) {
        addLine(li, "times", times.map(function (t) {
          return String(t.at) + " " + String(t.film || "");
        }).join(" · "));
      } else if (NOTES[r.note]) {
        addLine(li, "times", NOTES[r.note]);
      }
      list.appendChild(li);
    });
    box.appendChild(list);
    var keys = Array.isArray(data.credits) ? data.credits.slice() : [];
    if (keys.indexOf("osm") < 0) {
      keys.unshift("osm");  // the map tiles always carry the OSM credit
    }
    keys.forEach(function (k) {
      if (Object.prototype.hasOwnProperty.call(CREDITS, k)) {
        var asof = k === "clusterflick" && data.asof ? " · as of " + String(data.asof) : "";
        addLine(box, "credit", CREDITS[k] + asof);
      }
    });
    document.body.appendChild(box);
    map.invalidateSize();
    return true;
  }

  function draw(data) {
    var places = addPlaces(data);
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
      if (!places) {  // with places the numbered list says it
        addLegend(n === 0 ? "best" : "alt", Math.round(r.min) + " min, " + Number(r.km).toFixed(1) + " km");
      }
    });
    pins.forEach(function (p, n) {
      layers.push(L.marker([p.lat, p.lon], {keyboard: false, interactive: false}).addTo(map));
      var role = pins.length === 2 ? (n === 0 ? "Start: " : "End: ") : "";
      if (!places) {
        addLegend("pin", role + String(p.label || ""));
      }
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
  }).then(function (data) {
    draw(data);
    addTiles();
  }).catch(function () {
    say("Map data unavailable");
  });
})();

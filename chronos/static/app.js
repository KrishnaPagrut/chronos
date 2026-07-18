/* Chronos map UI: markers by category, magnitude filter, before/after slider. */
(function () {
  "use strict";

  /* Legend order is fixed — it matches the validated palette order. */
  const CATEGORIES = [
    ["construction", "construction"],
    ["signage", "signage"],
    ["demolition", "demolition"],
    ["road_infrastructure", "road infrastructure"],
    ["storefront_change", "storefront change"],
    ["vegetation", "vegetation"],
    ["street_furniture", "street furniture"],
    ["surface_condition", "surface condition"],
    ["other", "other"],
  ];
  const CATEGORY_LABEL = Object.fromEntries(CATEGORIES);
  /* Shape backs up color for the two color-confusable pairs (see style.css). */
  const CATEGORY_SHAPE = { surface_condition: "shape-square", signage: "shape-diamond" };

  const state = {
    changes: [],
    markers: new Map(), // pair_id -> {marker, el, change}
    magnitudes: new Set(["major", "moderate"]), // Subtle is opt-in
    categoriesOff: new Set(),
    showUnchanged: false,
    selected: null,
  };

  const $ = (id) => document.getElementById(id);

  /* ---------------- theme ---------------- */
  const THEMES = ["auto", "light", "dark"];
  function applyTheme(theme) {
    if (theme === "auto") delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = theme;
  }
  (function initTheme() {
    let theme = localStorage.getItem("chronos-theme") || "auto";
    const urlTheme = new URLSearchParams(location.search).get("theme");
    if (THEMES.includes(urlTheme)) theme = urlTheme; // demo override, not persisted
    applyTheme(theme);
    $("themeBtn").addEventListener("click", () => {
      theme = THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length];
      localStorage.setItem("chronos-theme", theme);
      applyTheme(theme);
      $("themeBtn").title = "Theme: " + theme;
    });
  })();

  /* ---------------- map ---------------- */
  let map = null;
  try {
    map = new maplibregl.Map({
      container: "map",
      style: {
        version: 8,
        sources: {
          osm: {
            type: "raster",
            tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
            tileSize: 256,
            maxzoom: 19,
            attribution: "© OpenStreetMap contributors",
          },
        },
        layers: [{ id: "osm", type: "raster", source: "osm" }],
      },
      center: [-122.418, 37.775],
      zoom: 14,
      attributionControl: { compact: true },
    });
    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
    map.on("click", deselect);
  } catch (err) {
    const ribbon = $("ribbon");
    ribbon.innerHTML = "<b>Map unavailable</b> — " + err.message;
    ribbon.hidden = false;
  }

  /* ---------------- data ---------------- */
  async function loadData() {
    const [stats, changes] = await Promise.all([
      fetch("/api/stats").then((r) => r.json()),
      fetch("/api/changes?include_unchanged=1").then((r) => r.json()),
    ]);
    $("statImages").textContent = stats.images;
    $("statPairs").textContent = stats.pairs;
    $("statJudged").textContent = stats.judged;
    $("statChanged").textContent = stats.changed;

    state.changes = changes;
    if (!changes.length) {
      const ribbon = $("ribbon");
      ribbon.innerHTML =
        "<b>No judged pairs yet</b> — run <code>python -m chronos ingest</code> then <code>inspect</code>";
      ribbon.hidden = false;
      return;
    }
    buildMarkers();
    buildLegend();
    applyFilters();
    fitToMarkers();

    // Deep link: /?pair=<pair_id> preselects a marker (used in demos).
    const pairId = new URLSearchParams(location.search).get("pair");
    if (pairId && state.markers.has(pairId)) select(pairId);
  }

  function buildMarkers() {
    if (!map) return;
    for (const change of state.changes) {
      const el = document.createElement("button");
      const label = change.changed
        ? CATEGORY_LABEL[change.category] || change.category
        : "no change";
      el.className = "marker" + (change.changed ? "" : " nochange");
      if (change.changed && CATEGORY_SHAPE[change.category]) {
        el.classList.add(CATEGORY_SHAPE[change.category]);
      }
      el.title = label + " · " + change.magnitude;
      el.setAttribute(
        "aria-label",
        label + " at " + change.lat.toFixed(5) + ", " + change.lon.toFixed(5)
      );
      const dot = document.createElement("span");
      dot.className = "dot";
      el.appendChild(dot);
      if (change.changed) {
        el.style.setProperty("--cat", "var(--c-" + change.category + ")");
      }
      el.addEventListener("click", (e) => {
        e.stopPropagation();
        select(change.pair_id);
      });
      const marker = new maplibregl.Marker({ element: el })
        .setLngLat([change.lon, change.lat])
        .addTo(map);
      state.markers.set(change.pair_id, { marker, el, change });
    }
  }

  function fitToMarkers() {
    const visible = [...state.markers.values()].filter((m) => !m.el.hidden);
    if (!map || !visible.length) return;
    const bounds = new maplibregl.LngLatBounds();
    for (const m of visible) bounds.extend([m.change.lon, m.change.lat]);
    map.fitBounds(bounds, { padding: 80, maxZoom: 17, duration: 600 });
  }

  /* ---------------- legend ---------------- */
  function buildLegend() {
    const counts = {};
    for (const c of state.changes)
      if (c.changed) counts[c.category] = (counts[c.category] || 0) + 1;

    const ul = $("legendList");
    ul.innerHTML = "";
    for (const [key, label] of CATEGORIES) {
      if (!counts[key]) continue;
      const li = document.createElement("li");
      li.dataset.cat = key;
      li.title = "Click to toggle";
      li.innerHTML =
        '<span class="swatch ' + (CATEGORY_SHAPE[key] || "") + '" style="--cat: var(--c-' +
        key + ')"></span>' +
        "<span>" + label + "</span>" +
        '<span class="count">' + counts[key] + "</span>";
      li.addEventListener("click", () => {
        if (state.categoriesOff.has(key)) state.categoriesOff.delete(key);
        else state.categoriesOff.add(key);
        li.classList.toggle("off", state.categoriesOff.has(key));
        applyFilters();
      });
      ul.appendChild(li);
    }
  }

  /* ---------------- filters ---------------- */
  function isVisible(change) {
    if (!change.changed) return state.showUnchanged;
    if (!state.magnitudes.has(change.magnitude)) return false;
    if (state.categoriesOff.has(change.category)) return false;
    return true;
  }

  function applyFilters() {
    for (const { el, change } of state.markers.values()) {
      el.hidden = !isVisible(change);
    }
    if (state.selected && !isVisible(state.markers.get(state.selected).change)) {
      deselect();
    }
  }

  for (const btn of document.querySelectorAll(".mchip")) {
    btn.addEventListener("click", () => {
      const mag = btn.dataset.mag;
      if (state.magnitudes.has(mag)) state.magnitudes.delete(mag);
      else state.magnitudes.add(mag);
      btn.classList.toggle("active", state.magnitudes.has(mag));
      applyFilters();
    });
  }

  $("showUnchanged").addEventListener("change", (e) => {
    state.showUnchanged = e.target.checked;
    applyFilters();
  });

  /* ---------------- selection & detail panel ---------------- */
  function deselect() {
    if (state.selected) {
      const prev = state.markers.get(state.selected);
      if (prev) prev.el.classList.remove("selected");
    }
    state.selected = null;
    $("panelDetail").hidden = true;
    $("panelEmpty").hidden = false;
  }

  function select(pairId) {
    if (state.selected === pairId) return;
    if (state.selected) {
      const prev = state.markers.get(state.selected);
      if (prev) prev.el.classList.remove("selected");
    }
    state.selected = pairId;
    const entry = state.markers.get(pairId);
    entry.el.classList.add("selected");
    renderDetail(entry.change);
    if (map) map.easeTo({ center: [entry.change.lon, entry.change.lat], duration: 400 });
  }

  function renderDetail(c) {
    const catVar = c.changed ? "var(--c-" + c.category + ")" : "var(--c-no_change)";
    const catLabel = c.changed
      ? CATEGORY_LABEL[c.category] || c.category
      : "no change";

    $("locTitle").textContent = c.changed
      ? catLabel.charAt(0).toUpperCase() + catLabel.slice(1)
      : "No durable change";
    $("locCoords").textContent = c.lat.toFixed(5) + ", " + c.lon.toFixed(5);

    $("catChip").style.setProperty("--cat", catVar);
    $("catName").textContent = catLabel;
    $("magTag").textContent = c.magnitude;
    $("confFill").style.setProperty("--cat", catVar);
    $("confFill").style.width = Math.round(c.confidence * 100) + "%";
    $("confNum").textContent = c.confidence.toFixed(2);

    $("imgBefore").src = "/images/" + c.older.image_id + ".jpg";
    $("imgAfter").src = "/images/" + c.newer.image_id + ".jpg";
    $("dateBefore").textContent = c.older.date;
    $("dateAfter").textContent = c.newer.date;
    resetSlider();

    $("evidence").textContent = c.evidence;
    $("descOldK").textContent = c.older.date;
    $("descNewK").textContent = c.newer.date;
    $("descOld").textContent = c.old_description || "–";
    $("descNew").textContent = c.new_description || "–";

    $("metaCaptured").textContent = c.older.date + " → " + c.newer.date;
    $("metaGap").textContent = (c.gap_days / 365).toFixed(1) + " yr";
    $("metaDist").textContent = c.distance_m.toFixed(1) + " m";
    $("metaHeading").textContent = Math.round(c.heading_diff_deg) + "°";
    $("metaModel").textContent = c.model;
    $("pairId").textContent = "PAIR " + c.pair_id;

    $("panelEmpty").hidden = true;
    $("panelDetail").hidden = false;
  }

  /* ---------------- before/after slider ---------------- */
  const slider = $("slider");
  function setSlider(pct) {
    $("imgAfter").style.clipPath = "inset(0 0 0 " + pct + "%)";
    $("handle").style.left = pct + "%";
    document.querySelector(".grip").style.left = pct + "%";
  }
  slider.addEventListener("input", () => setSlider(slider.value));
  function resetSlider() {
    slider.value = 50;
    setSlider(50);
  }

  loadData().catch((err) => {
    const ribbon = $("ribbon");
    ribbon.innerHTML = "<b>Failed to load data</b> — " + err.message;
    ribbon.hidden = false;
  });
})();

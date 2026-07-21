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
    // "search this area" flow
    searchReady: false,
    areaBusy: false,
    areaBbox: null,
    areaJudgeLimit: 0,
    briefBusy: false,
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
    // Offer to run detection on a new area once the user pans/zooms.
    map.on("moveend", () => {
      if (state.searchReady && !state.areaBusy) showAreaIdle();
    });
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
    $("briefAction").hidden = false;

    // Deep link: /?z=<zoom> and /?c=<lat>,<lon> pin an explicit map view;
    // otherwise fit to the markers.
    const params = new URLSearchParams(location.search);
    const z = params.get("z");
    const c = params.get("c");
    if ((z || c) && map) {
      if (c) {
        const [lat, lon] = c.split(",").map(Number);
        if (isFinite(lat) && isFinite(lon)) map.setCenter([lon, lat]);
      }
      if (z && isFinite(Number(z))) map.setZoom(Number(z));
    } else {
      fitToMarkers();
    }

    // Deep link: /?pair=<pair_id> preselects a marker; add &expand=1 to open
    // straight into the fullscreen before/after comparison (used in demos).
    const pairId = params.get("pair");
    if (pairId && state.markers.has(pairId)) {
      select(pairId);
      if (params.get("expand")) openLightbox();
    }

    // Enable "search this area" only after the initial view settles, so the
    // opening fit-to-markers move doesn't pop the button up immediately.
    setTimeout(() => { state.searchReady = true; }, 1200);
  }

  function addMarker(change) {
    if (!map || state.markers.has(change.pair_id)) return;
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

  function buildMarkers() {
    for (const change of state.changes) addMarker(change);
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
    $("panelBrief").hidden = true;
    $("panelEmpty").hidden = false;
  }

  function select(pairId, opts) {
    opts = opts || {};
    if (state.selected === pairId) return;
    if (state.selected) {
      const prev = state.markers.get(state.selected);
      if (prev) prev.el.classList.remove("selected");
    }
    state.selected = pairId;
    const entry = state.markers.get(pairId);
    entry.el.classList.add("selected");
    renderDetail(entry.change);
    if (map && !opts.noMove) {
      map.easeTo({ center: [entry.change.lon, entry.change.lat], duration: 400 });
    }
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
    $("panelBrief").hidden = true;
    $("panelDetail").hidden = false;
  }

  /* ---------------- evidence-linked area brief ---------------- */
  function showBrief(result) {
    const brief = result.brief;
    $("briefTitle").textContent = brief.title;
    $("briefSummary").textContent = brief.summary;
    $("briefCaveat").textContent = brief.coverage_caveat;
    $("briefModel").textContent = "Generated with " + result.model +
      " · every finding opens its source pair";
    const findings = $("briefFindings");
    findings.innerHTML = "";
    for (const finding of brief.findings) {
      const item = document.createElement("button");
      item.className = "brief-finding";
      item.type = "button";
      item.innerHTML =
        '<span class="brief-action-tag"></span><span class="brief-pair"></span>' +
        '<span class="brief-rationale"></span>';
      item.querySelector(".brief-action-tag").textContent = finding.action;
      item.querySelector(".brief-pair").textContent = "PAIR " + finding.pair_id;
      item.querySelector(".brief-rationale").textContent = finding.rationale;
      item.addEventListener("click", () => select(finding.pair_id));
      findings.appendChild(item);
    }
    if (state.selected) {
      const selected = state.markers.get(state.selected);
      if (selected) selected.el.classList.remove("selected");
      state.selected = null;
    }
    $("panelEmpty").hidden = true;
    $("panelDetail").hidden = true;
    $("panelBrief").hidden = false;
  }

  async function generateBrief() {
    if (!map || state.briefBusy) return;
    state.briefBusy = true;
    $("briefBtn").disabled = true;
    $("briefLabel").textContent = "Writing brief…";
    try {
      const r = await fetch("/api/brief_area?" + bboxQuery(), { method: "POST" });
      if (!r.ok) {
        let detail = "Could not generate area brief.";
        try { detail = (await r.json()).detail || detail; } catch (_) {}
        throw new Error(detail);
      }
      showBrief(await r.json());
    } catch (err) {
      toast(err.message || "Could not generate area brief.");
    } finally {
      state.briefBusy = false;
      $("briefBtn").disabled = false;
      $("briefLabel").textContent = "Brief this area";
    }
  }
  $("briefBtn").addEventListener("click", generateBrief);
  $("briefClose").addEventListener("click", deselect);

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

  /* ---------------- resizable panes ---------------- */
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  const shell = () => document.querySelector(".shell");

  let resizeRaf = 0;
  function scheduleMapResize() {
    if (!map || resizeRaf) return;
    resizeRaf = requestAnimationFrame(() => {
      resizeRaf = 0;
      map.resize();
    });
  }

  // ``compute`` returns the new pane width in px from the pointer event.
  function makeResizer(splitter, cssVar, compute) {
    if (!splitter) return;
    function onMove(e) {
      shell().style.setProperty(cssVar, Math.round(compute(e)) + "px");
      scheduleMapResize();
    }
    function onUp() {
      splitter.classList.remove("dragging");
      window.removeEventListener("pointermove", onMove);
      scheduleMapResize();
    }
    splitter.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      splitter.classList.add("dragging");
      splitter.setPointerCapture(e.pointerId);
      window.addEventListener("pointermove", onMove);
      window.addEventListener("pointerup", onUp, { once: true });
    });
  }

  // Panel is the rightmost pane: its width grows as the pointer moves left.
  makeResizer($("panelSplitter"), "--panel-w", (e) => {
    const r = shell().getBoundingClientRect();
    return clamp(r.right - e.clientX, 300, r.width - 360);
  });
  // Street View sits between the map and the panel; its right edge is pinned by
  // the panel, so width = (panel's left edge) - pointer.
  makeResizer($("svSplitter"), "--sv-w", (e) => {
    const r = shell().getBoundingClientRect();
    const panelLeft = $("panelSplitter").getBoundingClientRect().left;
    return clamp(panelLeft - e.clientX, 340, r.width - 420);
  });

  /* ---------------- lightbox (expand images) ---------------- */
  function lbSet(pct) {
    $("lbAfter").style.clipPath = "inset(0 0 0 " + pct + "%)";
    $("lbHandle").style.left = pct + "%";
    $("lbViewer").querySelector(".grip").style.left = pct + "%";
  }
  function openLightbox() {
    if (!state.selected) return;
    const c = state.markers.get(state.selected).change;
    const label = c.changed ? CATEGORY_LABEL[c.category] || c.category : "no change";
    $("lbCat").textContent = label + " · " + c.older.date + " → " + c.newer.date;
    $("lbBefore").src = $("imgBefore").src;
    $("lbAfter").src = $("imgAfter").src;
    $("lbDateBefore").textContent = c.older.date;
    $("lbDateAfter").textContent = c.newer.date;
    $("lbEvidence").textContent = c.evidence;
    $("lbSlider").value = 50;
    lbSet(50);
    $("lightbox").hidden = false;
  }
  function closeLightbox() {
    $("lightbox").hidden = true;
  }
  $("viewerExpand").addEventListener("click", openLightbox);
  $("lbSlider").addEventListener("input", () => lbSet($("lbSlider").value));
  $("lbClose").addEventListener("click", closeLightbox);
  $("lbBackdrop").addEventListener("click", closeLightbox);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("lightbox").hidden) closeLightbox();
  });

  /* ================= "Search this area" (two-step: find, then judge) ========= */

  function setArea(stateName, label, opts) {
    opts = opts || {};
    const btn = $("areaBtn");
    btn.dataset.state = stateName;
    $("areaLabel").textContent = label;
    btn.disabled = !!opts.disabled;
    btn.classList.toggle("spinning", !!opts.spinning);
    $("areaIco").textContent = opts.spinning
      ? "↻"
      : stateName === "found"
      ? "⚖"
      : "⌕";
    $("areaSearch").hidden = false;
  }
  function showAreaIdle() {
    setArea("idle", "Search this area", {});
  }
  function hideArea() {
    $("areaSearch").hidden = true;
  }

  async function postJob(path, query) {
    const r = await fetch(path + "?" + query, { method: "POST" });
    if (!r.ok) {
      let detail = "request failed";
      try { detail = (await r.json()).detail || detail; } catch (_) {}
      throw new Error(detail);
    }
    return r.json();
  }
  function pollJob(jobId, onProgress) {
    return new Promise((resolve, reject) => {
      const tick = async () => {
        let j;
        try {
          j = await fetch("/api/job/" + jobId).then((r) => r.json());
        } catch (err) {
          return reject(err);
        }
        if (j.status === "done") return resolve(j.result);
        if (j.status === "error") return reject(new Error(j.error || "job failed"));
        if (onProgress) onProgress(j);
        setTimeout(tick, 900);
      };
      tick();
    });
  }
  function bboxQuery() {
    const b = map.getBounds();
    state.areaBbox = {
      west: b.getWest(), south: b.getSouth(),
      east: b.getEast(), north: b.getNorth(),
    };
    return (
      "west=" + state.areaBbox.west + "&south=" + state.areaBbox.south +
      "&east=" + state.areaBbox.east + "&north=" + state.areaBbox.north
    );
  }

  async function startSearch() {
    state.areaBusy = true;
    setArea("searching", "Fetching imagery…", { spinning: true, disabled: true });
    try {
      const { job_id } = await postJob("/api/search_area", bboxQuery());
      const res = await pollJob(job_id, (j) =>
        setArea("searching", (j.phase || "working") + "…", { spinning: true, disabled: true })
      );
      state.areaBusy = false;
      if (!res.candidates) {
        setArea("idle", "No new pairs found here", {});
        setTimeout(() => { if (!state.areaBusy) hideArea(); }, 2200);
        return;
      }
      state.areaJudgeLimit = res.judge_limit;
      setArea(
        "found",
        "Judge " + res.judge_limit + " · ~$" + res.est_cost.toFixed(2),
        {}
      );
    } catch (err) {
      state.areaBusy = false;
      setArea("idle", err.message || "Search failed", {});
      setTimeout(() => { if (!state.areaBusy) showAreaIdle(); }, 2600);
    }
  }

  async function startJudge() {
    if (!state.areaBbox) return;
    state.areaBusy = true;
    setArea("judging", "Judging…", { spinning: true, disabled: true });
    try {
      const q = bboxFromState() + "&limit=" + state.areaJudgeLimit;
      const { job_id } = await postJob("/api/judge_area", q);
      const res = await pollJob(job_id, (j) =>
        setArea(
          "judging",
          j.total ? "Judging " + j.done + "/" + j.total + "…" : "Judging…",
          { spinning: true, disabled: true }
        )
      );
      state.areaBusy = false;
      const changes = res.changes || [];
      addChanges(changes);
      await refreshStats();

      const changed = changes.filter((c) => c.changed);
      if (!changed.length) {
        // Judging costs money regardless of the verdict; be explicit when an
        // area turned out to have no durable changes.
        toast(
          changes.length
            ? "Judged " + changes.length + " pair" + (changes.length === 1 ? "" : "s") +
              " — no durable changes found in this area."
            : "Nothing to judge here."
        );
        showAreaIdle();
        setTimeout(() => { if (!state.areaBusy) hideArea(); }, 2600);
        return;
      }
      // Make sure the new markers are actually visible: turn on any magnitude
      // chips they need (e.g. Subtle, which is off by default), then focus them.
      const revealed = revealMagnitudes(changed);
      applyFilters();
      fitToChanges(changed);
      const strongest = changed.slice().sort((a, b) => b.confidence - a.confidence)[0];
      select(strongest.pair_id, { noMove: true });
      toast(
        "Found " + changed.length + " change" + (changed.length === 1 ? "" : "s") +
        (revealed ? " (turned on hidden magnitudes to show them)" : "") + "."
      );
    } catch (err) {
      state.areaBusy = false;
      setArea("found", err.message || "Judge failed — retry", {});
    }
  }

  function revealMagnitudes(changes) {
    const mags = new Set(changes.map((c) => c.magnitude));
    let changedUI = false;
    for (const btn of document.querySelectorAll(".mchip")) {
      const m = btn.dataset.mag;
      if (mags.has(m) && !state.magnitudes.has(m)) {
        state.magnitudes.add(m);
        btn.classList.add("active");
        changedUI = true;
      }
    }
    return changedUI;
  }

  function fitToChanges(changes) {
    if (!map || !changes.length) return;
    const b = new maplibregl.LngLatBounds();
    for (const c of changes) b.extend([c.lon, c.lat]);
    map.fitBounds(b, { padding: 100, maxZoom: 18, duration: 700 });
  }

  function toast(msg, ms) {
    const r = $("ribbon");
    r.innerHTML = msg;
    r.hidden = false;
    clearTimeout(toast._t);
    toast._t = setTimeout(() => { r.hidden = true; }, ms || 4500);
  }

  // Rebuild the query from the stored bbox (map may have moved during judging).
  function bboxFromState() {
    const b = state.areaBbox;
    return "west=" + b.west + "&south=" + b.south + "&east=" + b.east + "&north=" + b.north;
  }

  function addChanges(changes) {
    let added = 0;
    for (const c of changes) {
      if (state.markers.has(c.pair_id)) continue;
      state.changes.push(c);
      addMarker(c);
      added++;
    }
    if (added) {
      buildLegend();
      applyFilters();
    }
  }

  async function refreshStats() {
    try {
      const s = await fetch("/api/stats").then((r) => r.json());
      $("statImages").textContent = s.images;
      $("statPairs").textContent = s.pairs;
      $("statJudged").textContent = s.judged;
      $("statChanged").textContent = s.changed;
    } catch (_) {}
  }

  $("areaBtn").addEventListener("click", () => {
    const st = $("areaBtn").dataset.state;
    if (st === "found") startJudge();
    else if (!state.areaBusy) startSearch();
  });

  /* ================= Street View mode (pegman + mapillary-js) ================= */

  const sv = { token: null, viewer: null, here: null, hereEl: null, markersAdded: false };

  /* Resolve a category's CSS color to a concrete hex for the 3D viewer markers. */
  function catColor(cat) {
    const v = getComputedStyle(document.documentElement)
      .getPropertyValue("--c-" + cat)
      .trim();
    return v || "#888888";
  }
  function fmtDate(ms) {
    return new Date(ms).toISOString().slice(0, 10);
  }

  async function initStreetView() {
    let cfg = {};
    try {
      cfg = await fetch("/api/config").then((r) => r.json());
    } catch (_) {
      /* leave pegman hidden below */
    }
    // Needs a token, the mapillary-js library, and a working WebGL map.
    if (!cfg.has_token || !window.mapillary || !map) {
      $("pegman").style.display = "none";
      return;
    }
    sv.token = cfg.mapillary_token;
    setupPegman();
    $("svClose").addEventListener("click", closeStreetView);

    // Deep link: /?sv=<lat>,<lon> opens Street View at that point on load.
    const svParam = new URLSearchParams(location.search).get("sv");
    if (svParam) {
      const [lat, lon] = svParam.split(",").map(Number);
      if (isFinite(lat) && isFinite(lon)) enterStreetView(lat, lon);
    }
  }

  /* ---------------- pegman drag ---------------- */
  function setupPegman() {
    const pegman = $("pegman");
    const mapWrap = document.querySelector(".map-wrap");
    let ghost = null;

    function moveGhost(e) {
      ghost.style.left = e.clientX + "px";
      ghost.style.top = e.clientY + "px";
    }
    function onDown(e) {
      e.preventDefault();
      pegman.classList.add("dragging");
      mapWrap.classList.add("drop-armed");
      ghost = document.createElement("div");
      ghost.className = "pegman-ghost";
      ghost.textContent = "🧍";
      document.body.appendChild(ghost);
      moveGhost(e);
      window.addEventListener("pointermove", moveGhost);
      window.addEventListener("pointerup", onUp, { once: true });
    }
    function onUp(e) {
      window.removeEventListener("pointermove", moveGhost);
      pegman.classList.remove("dragging");
      mapWrap.classList.remove("drop-armed");
      if (ghost) { ghost.remove(); ghost = null; }
      const rect = $("map").getBoundingClientRect();
      const inside =
        e.clientX >= rect.left && e.clientX <= rect.right &&
        e.clientY >= rect.top && e.clientY <= rect.bottom;
      if (inside && map) {
        const p = map.unproject([e.clientX - rect.left, e.clientY - rect.top]);
        enterStreetView(p.lat, p.lng);
      }
    }
    pegman.addEventListener("pointerdown", onDown);
    pegman.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        const c = map.getCenter();
        enterStreetView(c.lat, c.lng);
      }
    });
  }

  /* ---------------- viewer lifecycle ---------------- */
  function showSvEmpty(msg) {
    $("svEmpty").textContent = msg;
    $("svEmpty").hidden = false;
  }

  async function enterStreetView(lat, lon) {
    if (!sv.token) return;
    const shellEl = document.querySelector(".shell");
    shellEl.classList.add("sv-open");
    $("streetview").hidden = false;
    $("svSplitter").hidden = false;
    // Give the pane a sensible starting width the first time it opens.
    if (!shellEl.style.getPropertyValue("--sv-w")) {
      shellEl.style.setProperty(
        "--sv-w",
        Math.round(shellEl.getBoundingClientRect().width * 0.46) + "px"
      );
    }
    $("svEmpty").hidden = true;
    $("svDate").textContent = "…";
    if (map) setTimeout(() => map.resize(), 0);

    let data;
    try {
      data = await fetch("/api/nearest?lat=" + lat + "&lon=" + lon).then((r) => r.json());
    } catch (_) {
      showSvEmpty("Could not reach Mapillary.");
      return;
    }
    if (!data.image_id) {
      showSvEmpty(
        data.reason === "no_panorama"
          ? "No 360° Mapillary panorama nearby — try another street."
          : "No Mapillary imagery at this point — try dropping nearer a street."
      );
      return;
    }
    $("svEmpty").hidden = true;
    if (data.date) $("svDate").textContent = data.date;

    try {
      if (!sv.viewer) await initViewer(data.image_id);
      else await sv.viewer.moveTo(data.image_id);
    } catch (_) {
      if (sv.viewer) { sv.viewer.remove(); sv.viewer = null; }
      showSvEmpty("Could not load this 360° panorama — try another street.");
    }
  }

  async function initViewer(imageId) {
    sv.viewer = new mapillary.Viewer({
      accessToken: sv.token,
      container: "mly",
      // Create unbound, then move to a panorama below. This leaves retries and
      // later drops free to select another panorama if the first one fails.
      cameraControls: mapillary.CameraControls.Street,
      component: { cover: false, marker: true },
    });
    sv.viewer.on("image", onViewerImage);
    sv.viewer.on("bearing", onViewerBearing);
    sv.viewer.on("click", onViewerClick);
    addChangeMarkersToViewer();
    // Restrict the navigation graph to 360° captures. Without this filter,
    // following a link can switch the viewer back to a fixed-FOV photo.
    await sv.viewer.setFilter(["==", "cameraType", "spherical"]);
    await sv.viewer.moveTo(imageId);
  }

  /* Float every detected change as an interactive 3D marker in the scene. */
  function addChangeMarkersToViewer() {
    if (sv.markersAdded || !sv.viewer) return;
    const MarkerCls = mapillary.SimpleMarker || mapillary.CircleMarker;
    if (!MarkerCls) return;
    const mc = sv.viewer.getComponent("marker");
    const markers = [];
    for (const c of state.changes) {
      if (!c.changed) continue;
      markers.push(
        new MarkerCls(c.pair_id, { lat: c.lat, lng: c.lon }, {
          interactive: true,
          color: catColor(c.category),   // balloon body = category color
          ballColor: "#ffffff",          // white center reads cleanly at any hue
          ballOpacity: 0.95,
          opacity: 0.85,
          radius: 0.6,
        })
      );
    }
    if (markers.length) mc.add(markers);
    sv.markersAdded = true;
  }

  function onViewerImage(e) {
    const img = e.image;
    if (img.capturedAt) $("svDate").textContent = fmtDate(img.capturedAt);
    if (img.lngLat) updateHere(img.lngLat.lat, img.lngLat.lng);
  }
  function onViewerBearing(e) {
    if (sv.hereEl) sv.hereEl.style.setProperty("--bearing", e.bearing + "deg");
  }
  function onViewerClick(e) {
    if (!sv.viewer) return;
    const mc = sv.viewer.getComponent("marker");
    Promise.resolve(mc.getMarkerIdAt(e.pixelPoint)).then((id) => {
      if (id && state.markers.has(id)) select(id);
    });
  }

  /* The "you are here" marker on the map, following the viewer's position. */
  function updateHere(lat, lng) {
    if (!map) return;
    if (!sv.here) {
      const el = document.createElement("div");
      el.className = "here";
      el.innerHTML = '<div class="fan"></div><div class="pin"></div>';
      sv.hereEl = el;
      sv.here = new maplibregl.Marker({ element: el }).setLngLat([lng, lat]).addTo(map);
    } else {
      sv.here.setLngLat([lng, lat]);
    }
  }

  function closeStreetView() {
    document.querySelector(".shell").classList.remove("sv-open");
    $("streetview").hidden = true;
    $("svSplitter").hidden = true;
    if (sv.here) { sv.here.remove(); sv.here = null; sv.hereEl = null; }
    if (map) setTimeout(() => map.resize(), 0);
  }

  loadData().catch((err) => {
    const ribbon = $("ribbon");
    ribbon.innerHTML = "<b>Failed to load data</b> — " + err.message;
    ribbon.hidden = false;
  });
  initStreetView();
})();

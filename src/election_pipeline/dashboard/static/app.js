const $ = (id) => document.getElementById(id);
const NS = "http://www.w3.org/2000/svg";
const COLORS = {
  DEM: "#3979d9",
  REP: "#d9555d",
  OTHER: "#e6b735",
  UNKNOWN: "#909cab",
};
const METHODS = {
  total_votes: "All reported votes",
  election_day: "Election Day",
  early_voting: "Early in person",
  absentee_by_mail: "Absentee by mail",
  provisional: "Provisional",
};
const HOUSE = ["NC-01", "NC-03", "NC-04", "NC-11"];
const EMPTY = {
  total: 0,
  status: "unreported",
  candidates: [],
  leader: null,
  margin: null,
};
const fmt = (n) => new Intl.NumberFormat("en-US").format(n);
const esc = (s) =>
  String(s ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const time = (value) =>
  value
    ? new Intl.DateTimeFormat("en-US", {
        month: "short",
        day: "numeric",
        hour: "numeric",
        minute: "2-digit",
        second: "2-digit",
        timeZone: "America/New_York",
        timeZoneName: "short",
      }).format(new Date(value))
    : "Unavailable";
const partyName = (c) =>
  ({
    DEM: "Democrat",
    REP: "Republican",
    UNA: "Unaffiliated",
    LIB: "Libertarian",
    GRE: "Green",
  })[c.party] ||
  c.party ||
  (c.group === "OTHER" ? "Other / write-in" : "Party unavailable");
let geography,
  snapshot,
  etag,
  busy = false,
  networkError = "",
  activeLayer,
  drag,
  dragged = false;
let state = {
  race: "NC-SEN",
  county: null,
  method: "total_votes",
  labels: false,
};
let view;
const isSenate = () => state.race === "NC-SEN";
const race = (id) => snapshot?.races[id];
const stats = (id, county = null) =>
  (county ? race(id)?.counties[county]?.stats : race(id)?.stats)?.[
    state.method
  ] || EMPTY;
const fill = (s) =>
  s.status === "tie" || s.leader === "UNKNOWN"
    ? "url(#tie-fill)"
    : s.total
      ? COLORS[s.leader]
      : "#dce2e9";
const lead = (s) =>
  !s.total
    ? "No votes reported"
    : s.status === "tie"
      ? "Tied in reported votes"
      : `${s.candidates[0].name} leads`;

function setView(next) {
  view = next;
  $("map").setAttribute("viewBox", view.join(" "));
}
function zoom(factor, anchor) {
  const current = view[2] / geography.viewBox[2];
  const scale = Math.max(0.2, Math.min(1, current / factor)) / current;
  const point = anchor || {
    x: view[0] + view[2] / 2,
    y: view[1] + view[3] / 2,
  };
  setView([
    point.x - (point.x - view[0]) * scale,
    point.y - (point.y - view[1]) * scale,
    view[2] * scale,
    view[3] * scale,
  ]);
}
function point(event) {
  const p = $("map").createSVGPoint();
  p.x = event.clientX;
  p.y = event.clientY;
  return p.matrixTransform($("map").getScreenCTM().inverse());
}
function choose(id) {
  if (isSenate()) state.county = state.county === id ? null : id;
  else {
    state.race = id;
    state.county = null;
  }
  $("tooltip").hidden = true;
  render();
}
function tooltip(feature, event) {
  if (dragged) return;
  const s = isSenate() ? stats("NC-SEN", feature.id) : stats(feature.id);
  const outside = !isSenate() && !HOUSE.includes(feature.id);
  $("tooltip").innerHTML =
    `<strong>${esc(feature.name)}${isSenate() ? " County" : ""}</strong>${esc(outside ? "Outside current collection coverage" : lead(s))}<br><small>${outside ? "Select a tracked district for results" : `${fmt(s.total)} reported candidate votes`}</small>`;
  $("tooltip").hidden = false;
  $("tooltip").style.left =
    Math.min(event.clientX + 14, window.innerWidth - 245) + "px";
  $("tooltip").style.top =
    Math.min(event.clientY + 14, window.innerHeight - 125) + "px";
}
function buildMap() {
  const layer = isSenate() ? "counties" : "districts";
  if (activeLayer === layer) return;
  activeLayer = layer;
  $("shapes").replaceChildren();
  $("map-labels").replaceChildren();
  $("area-label").textContent = isSenate()
    ? "Find a county"
    : "Select a district";
  $("area").innerHTML =
    (isSenate() ? '<option value="">Find a county…</option>' : "") +
    geography[layer]
      .map(
        (f) =>
          `<option value="${esc(f.id)}">${esc(f.name)}${layer === "districts" && !HOUSE.includes(f.id) ? " · outside coverage" : ""}</option>`,
      )
      .join("");
  for (const feature of geography[layer]) {
    const path = document.createElementNS(NS, "path");
    path.setAttribute("d", feature.path);
    path.dataset.id = feature.id;
    path.setAttribute("role", "button");
    path.setAttribute("tabindex", "0");
    path.addEventListener("click", () => {
      if (!dragged) choose(feature.id);
    });
    path.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        choose(feature.id);
      }
    });
    path.addEventListener("pointermove", (e) => tooltip(feature, e));
    path.addEventListener("pointerleave", () => {
      $("tooltip").hidden = true;
    });
    $("shapes").append(path);
    const label = document.createElementNS(NS, "text");
    label.setAttribute("x", feature.center[0]);
    label.setAttribute("y", feature.center[1]);
    label.textContent = isSenate()
      ? feature.name
      : String(Number(feature.id.slice(3)));
    $("map-labels").append(label);
  }
  setView([...geography.viewBox]);
}
function renderMap() {
  buildMap();
  const senate = isSenate();
  $("senate-tab").classList.toggle("active", senate);
  $("house-tab").classList.toggle("active", !senate);
  $("senate-tab").setAttribute("aria-selected", senate);
  $("house-tab").setAttribute("aria-selected", !senate);
  $("map-title").textContent = senate
    ? "Senate · County results"
    : "House · District results";
  $("map-description").textContent = senate
    ? "Color shows the leading candidate’s party, not a race call."
    : "Four tracked races. Other districts remain outside current coverage.";
  $("statewide").hidden = !senate;
  $("house-races").hidden = senate;
  document.querySelector(".outside-legend").hidden = senate;
  $("map-labels").style.display = state.labels || !senate ? "" : "none";
  $("area").value = senate ? state.county || "" : state.race;
  for (const path of $("shapes").children) {
    const id = path.dataset.id;
    const s = senate ? stats("NC-SEN", id) : stats(id);
    const outside = !senate && !HOUSE.includes(id);
    path.setAttribute("fill", outside ? "url(#outside-fill)" : fill(s));
    const selected = senate ? state.county === id : state.race === id;
    path.classList.toggle("selected", selected);
    path.setAttribute("aria-pressed", selected);
    path.setAttribute(
      "aria-label",
      `${id}: ${outside ? "outside coverage" : `${lead(s)}, ${fmt(s.total)} reported votes`}`,
    );
  }
  const withVotes = geography.counties.filter(
    (f) => stats("NC-SEN", f.id).total > 0,
  ).length;
  $("coverage-label").textContent = senate
    ? `${withVotes} of 100 counties have reported votes`
    : "14 districts shown · 4 races tracked";
  if (!senate)
    $("house-races").innerHTML = HOUSE.map((id) => {
      const s = stats(id),
        color = COLORS[s.leader] || "#a8b3c0";
      return `<button class="race-card ${state.race === id ? "active" : ""}" data-race="${id}"><strong>${id}</strong><span><i class="swatch" style="background:${color}"></i>${s.total ? (s.status === "tie" ? "Tied" : s.leader === "UNKNOWN" ? "Party unavailable" : `${s.leader === "DEM" ? "Democratic" : s.leader === "REP" ? "Republican" : "Other"} lead`) : "Awaiting votes"}</span><span>${fmt(s.total)} reported votes</span></button>`;
    }).join("");
}
function renderResults() {
  const senate = isSenate(),
    outside = !senate && !HOUSE.includes(state.race);
  const county =
    state.county && geography.counties.find((f) => f.id === state.county);
  const s = stats(state.race, senate ? state.county : null);
  $("result-scope").textContent = outside
    ? "OUTSIDE COVERAGE"
    : senate && county
      ? "COUNTY DETAIL"
      : senate
        ? "STATEWIDE"
        : "CONGRESSIONAL DISTRICT";
  $("result-title").textContent =
    county && senate
      ? county.name + " County"
      : senate
        ? "U.S. Senate"
        : state.race;
  $("result-subtitle").textContent =
    senate && county
      ? "U.S. Senate · North Carolina"
      : senate
        ? "North Carolina"
        : "U.S. House · North Carolina";
  $("total-label").textContent =
    METHODS[state.method] === "All reported votes"
      ? "Reported candidate votes"
      : `${METHODS[state.method]} candidate votes`;
  $("vote-total").textContent =
    outside || !snapshot?.publication ? "—" : fmt(s.total);
  $("lead-summary").textContent = outside
    ? "This district is not in the current collection scope."
    : !s.total
      ? "Awaiting reported votes"
      : s.status === "tie"
        ? "Candidates are tied in reported votes"
        : s.margin === null
          ? "One reported choice · no comparison available"
          : `${fmt(s.margin)} vote lead · ${(s.margin_share * 100).toFixed(1)} percentage points`;
  $("candidate-results").innerHTML = outside
    ? '<div class="candidate-empty">Current House coverage: NC-01, NC-03, NC-04 and NC-11. Select one on the map or below it.</div>'
    : s.candidates.length
      ? s.candidates
          .map(
            (c) =>
              `<div class="candidate"><div class="candidate-top"><div class="candidate-name">${esc(c.name)}<span class="candidate-party">${esc(partyName(c))}</span></div><div class="candidate-numbers"><strong>${c.share === null ? "—" : (c.share * 100).toFixed(1) + "%"}</strong><span>${fmt(c.votes)} votes</span></div></div><div class="candidate-bar"><i style="width:${(c.share || 0) * 100}%;background:${COLORS[c.group]}"></i></div></div>`,
          )
          .join("")
      : '<div class="candidate-empty">No candidate counts are available for this selection in the published snapshot.</div>';
  const allStats =
    senate && county
      ? race(state.race)?.counties[state.county]?.stats
      : race(state.race)?.stats;
  $("method-breakdown").innerHTML = outside
    ? ""
    : "<h3>Reported votes by method</h3>" +
      Object.entries(METHODS)
        .filter(([key]) => key !== "total_votes")
        .map(
          ([key, label]) =>
            `<div class="method-row"><span>${label}</span><strong>${fmt(allStats?.[key]?.total || 0)}</strong></div>`,
        )
        .join("");
  $("result-note").textContent =
    snapshot?.mode === "demo"
      ? "Simulated counts for interface testing. These are not election results."
      : "Results are unofficial. Candidate vote totals are not ballot turnout.";
}
function renderPublication() {
  const p = snapshot?.publication,
    demo = snapshot?.mode === "demo";
  $("publication-status").textContent = demo
    ? "SIMULATED DEMO"
    : !p
      ? "AWAITING PUBLICATION"
      : p.status === "test"
        ? "TEST PUBLICATION"
        : "UNOFFICIAL RESULTS";
  $("publication-status").className =
    "badge " + (demo || p?.status === "test" ? "test" : p ? "unofficial" : "");
  $("published-time").textContent = demo
    ? "Illustrative counts · not election results"
    : p
      ? `Published ${time(p.published_at)}`
      : "Waiting for a published snapshot";
  $("source-time").textContent = demo
    ? "Simulated data"
    : time(p?.source_modified_at);
  $("publication-id").textContent = p
    ? `${String(p.publication_id).slice(0, 8)}${demo ? "" : " · release " + p.release_id}`
    : "—";
  const notice = $("notice");
  notice.hidden = false;
  if (networkError) {
    notice.className = "notice error";
    notice.textContent = `${networkError}${snapshot ? " The last received snapshot remains displayed." : ""}`;
  } else if (demo) {
    notice.className = "notice demo";
    notice.textContent =
      "DEMO · All vote counts and candidate names are simulated. Maps use official boundaries. Connect PostgreSQL to view your published returns.";
  } else if (p?.status === "test") {
    notice.className = "notice demo";
    notice.textContent =
      "TEST PUBLICATION · Preview data for validation. These are not live election results.";
  } else if (!p) {
    notice.className = "notice";
    notice.textContent =
      "Awaiting the first published results snapshot. Counties remain grey until votes are available.";
  } else notice.hidden = true;
}
function render() {
  if (geography) {
    renderMap();
    renderResults();
  }
  renderPublication();
}
async function refresh() {
  if (busy || document.hidden) return;
  busy = true;
  $("refresh").disabled = true;
  try {
    const response = await fetch("/api/snapshot", {
      headers: etag ? { "If-None-Match": etag } : {},
      signal: AbortSignal.timeout(8000),
    });
    if (response.status !== 304) {
      if (!response.ok)
        throw new Error("Published results are temporarily unavailable.");
      const next = await response.json();
      if (next.map_version !== geography.version) {
        const mapResponse = await fetch(
          `/nc-map.json?v=${encodeURIComponent(next.map_version)}`,
        );
        if (!mapResponse.ok) throw new Error("The map could not be refreshed.");
        const updated = await mapResponse.json();
        if (updated.version !== next.map_version)
          throw new Error("Map and results versions do not match.");
        geography = updated;
        activeLayer = undefined;
      }
      snapshot = next;
      etag = response.headers.get("ETag");
    }
    networkError = "";
    $("connection").textContent = "Connected";
    $("connection").className = "connection ok";
    $("checked-time").textContent =
      "Dashboard checked " +
      new Intl.DateTimeFormat("en-US", {
        hour: "numeric",
        minute: "2-digit",
        second: "2-digit",
      }).format(new Date());
    render();
  } catch (error) {
    networkError = error.message || "The dashboard could not refresh.";
    $("connection").textContent = "Refresh failed";
    $("connection").className = "connection error";
    renderPublication();
  } finally {
    busy = false;
    $("refresh").disabled = false;
  }
}
$("senate-tab").onclick = () => {
  state.race = "NC-SEN";
  state.county = null;
  render();
};
$("house-tab").onclick = () => {
  state.race = HOUSE.includes(state.race) ? state.race : "NC-01";
  state.county = null;
  render();
};
$("statewide").onclick = () => {
  state.county = null;
  render();
};
$("method").onchange = (e) => {
  state.method = e.target.value;
  render();
};
$("labels").onchange = (e) => {
  state.labels = e.target.checked;
  renderMap();
};
$("area").onchange = (e) => {
  if (isSenate()) state.county = e.target.value || null;
  else state.race = e.target.value;
  render();
};
$("house-races").onclick = (e) => {
  const button = e.target.closest("[data-race]");
  if (button) {
    state.race = button.dataset.race;
    render();
  }
};
$("zoom-in").onclick = () => zoom(1.4);
$("zoom-out").onclick = () => zoom(1 / 1.4);
$("reset-map").onclick = () => setView([...geography.viewBox]);
$("refresh").onclick = refresh;
$("map").addEventListener(
  "wheel",
  (event) => {
    event.preventDefault();
    zoom(event.deltaY < 0 ? 1.15 : 1 / 1.15, point(event));
    $("tooltip").hidden = true;
  },
  { passive: false },
);
$("map").onpointerdown = (event) => {
  if (event.button !== 0) return;
  drag = {
    id: event.pointerId,
    x: event.clientX,
    y: event.clientY,
    point: point(event),
    view: [...view],
  };
  dragged = false;
};
$("map").addEventListener("pointermove", (event) => {
  if (!drag || drag.id !== event.pointerId) return;
  if (
    Math.hypot(event.clientX - drag.x, event.clientY - drag.y) > 4 &&
    !dragged
  ) {
    dragged = true;
    $("map").setPointerCapture(event.pointerId);
    $("map").classList.add("dragging");
  }
  if (dragged) {
    const p = point(event);
    setView([
      view[0] + drag.point.x - p.x,
      view[1] + drag.point.y - p.y,
      view[2],
      view[3],
    ]);
    $("tooltip").hidden = true;
  }
});
const endDrag = () => {
  drag = null;
  $("map").classList.remove("dragging");
  setTimeout(() => {
    dragged = false;
  }, 0);
};
$("map").onpointerup = endDrag;
$("map").onpointercancel = endDrag;
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refresh();
});
try {
  const response = await fetch("/nc-map.json");
  if (!response.ok)
    throw new Error(
      "Map assets are missing. Run scripts/fetch_nc_boundaries.py.",
    );
  geography = await response.json();
  render();
  await refresh();
  setInterval(refresh, 15000);
} catch (error) {
  networkError = error.message;
  renderPublication();
}

const $ = (id) => document.getElementById(id);
const ns = "http://www.w3.org/2000/svg";
const offices = {
  senate: "U.S. Senate",
  house: "U.S. House",
  governor: "Governor",
};
const methodNames = {
  total_votes: "All reported votes",
  election_day: "Election Day",
  early_voting: "Early in person",
  absentee_by_mail: "Absentee by mail",
  provisional: "Provisional",
  absentee_combined: "Absentee (combined)",
};
const colors = {
  DEM: "#2775c7",
  REP: "#dc5259",
  OTHER: "#ecc650",
  awaiting_votes: "#a2b1c4",
  no_election: "#f1f3f6",
  not_covered: "url(#uncovered)",
  tie: "url(#tie)",
  UNKNOWN: "url(#tie)",
  unavailable: "url(#unavailable)",
};
const descriptions = {
  awaiting_votes: "Covered · awaiting votes",
  no_election: "No election for this office",
  not_covered: "Election held · outside our coverage",
  tie: "Tied vote",
  UNKNOWN: "Leading candidate party unavailable",
  unavailable: "Published data unavailable",
  DEM: "Democratic lead",
  REP: "Republican lead",
  OTHER: "Other party lead",
};
const number = (n) => n.toLocaleString("en-US");
const date = (value) =>
  value ? new Date(value).toLocaleString() : "Unavailable";
const view = {
  office: "senate",
  scope: "US",
  race: null,
  county: null,
  method: "total_votes",
};
let data,
  geometry = {},
  currentFeatures = [],
  etag,
  fetching = false,
  stale = false,
  zoom = 1;
const element = (tag, text, className) => {
  const e = document.createElement(tag);
  if (text !== undefined) e.textContent = text;
  if (className) e.className = className;
  return e;
};
function readLocation() {
  let parts;
  try {
    parts = location.hash.slice(1).split("/").map(decodeURIComponent);
  } catch {
    parts = [];
  }
  const [office, scope, race, county] = parts;
  view.office = offices[office] ? office : "senate";
  view.scope =
    scope && (scope === "US" || data?.coverage[scope]) ? scope : "US";
  view.race = race || null;
  view.county = county || null;
  view.method = "total_votes";
}
function writeLocation() {
  const hash = [view.office, view.scope, view.race || "", view.county || ""]
    .map(encodeURIComponent)
    .join("/")
    .replace(/\/+$/, "");
  if (location.hash.slice(1) !== hash) history.pushState(null, "", "#" + hash);
}
function racesFor(code) {
  return (data.coverage[code] || []).filter((id) =>
    view.office === "senate"
      ? id.endsWith("-SEN")
      : view.office === "governor"
        ? id.endsWith("-GOV")
        : !/-SEN$|-GOV$/.test(id),
  );
}
function raceFor(code) {
  return view.office === "house"
    ? racesFor(code).includes(view.race)
      ? view.race
      : racesFor(code)[0]
    : code + (view.office === "senate" ? "-SEN" : "-GOV");
}
function isElection(code) {
  return (
    view.office === "house" ||
    data.calendar[view.office + "_states"].includes(code)
  );
}
function statusFor(code, race, county = null) {
  if (!isElection(code)) return { status: "no_election" };
  if (!(data.coverage[code] || []).includes(race))
    return { status: "not_covered" };
  const state = data.states[code];
  if (!state || state.availability !== "available")
    return { status: "unavailable" };
  const contest = state.races[race];
  if (county && state.county_detail === false) return { status: "unavailable" };
  const stats = county
    ? contest?.counties[county]?.stats[view.method]
    : contest?.stats[view.scope === "US" ? "total_votes" : view.method];
  if (!state.publication || !stats || stats.total == null || stats.total === 0)
    return { status: "awaiting_votes", stats };
  return {
    status: stats.status === "tie" ? "tie" : stats.leader || "UNKNOWN",
    stats,
  };
}
async function loadGeometry(code) {
  if (geometry[code]) return true;
  try {
    const response = await fetch(
      "/" + code.toLowerCase() + "-map.json?v=expansion2",
    );
    if (!response.ok) throw Error("Map unavailable");
    geometry[code] = await response.json();
    return true;
  } catch (error) {
    $("notice").textContent = "State map unavailable. Refresh to retry.";
    return false;
  }
}
async function selectState(code, race = null) {
  if (!data.coverage[code] || !(await loadGeometry(code))) return;
  view.scope = code;
  view.race = race;
  view.county = null;
  view.method = "total_votes";
  zoom = 1;
  writeLocation();
  render();
}
function selectedFeature(f) {
  if (view.scope === "US") return false;
  return view.office === "house"
    ? f.id === raceFor(view.scope)
    : f.id === view.county;
}
function featureStatus(f) {
  if (view.scope === "US") {
    const code = view.office === "house" ? f.state : f.id;
    const race =
      view.office === "house"
        ? f.id
        : code + (view.office === "senate" ? "-SEN" : "-GOV");
    return statusFor(code, race);
  }
  if (
    data.states[view.scope].county_detail === false &&
    view.office !== "house"
  )
    return statusFor(view.scope, raceFor(view.scope));
  return statusFor(
    view.scope,
    view.office === "house" ? f.id : raceFor(view.scope),
    view.office === "house" ? null : f.id,
  );
}
function activateFeature(f) {
  if (view.scope === "US") {
    if (view.office === "house" && !data.coverage[f.state]?.includes(f.id))
      return;
    selectState(
      view.office === "house" ? f.state : f.id,
      view.office === "house" ? f.id : null,
    );
  } else if (view.office === "house") {
    if (racesFor(view.scope).includes(f.id)) {
      view.race = f.id;
      view.county = null;
      writeLocation();
      render();
    }
  } else {
    if (data.states[view.scope].county_detail === false) return;
    view.county = f.id;
    writeLocation();
    render();
  }
}
function drawMap() {
  const geo = geometry[view.scope];
  currentFeatures =
    view.scope === "US"
      ? view.office === "house"
        ? geo.districts
        : geo.states
      : view.office === "house"
        ? geo.districts
        : data.states[view.scope].county_detail === false
          ? [geo.outline]
          : geo.counties;
  const [x, y, w, h] = geo.viewBox;
  $("map").setAttribute(
    "viewBox",
    [
      x + (w * (1 - 1 / zoom)) / 2,
      y + (h * (1 - 1 / zoom)) / 2,
      w / zoom,
      h / zoom,
    ].join(" "),
  );
  $("map").setAttribute(
    "aria-label",
    `${view.scope === "US" ? "United States" : data.states[view.scope].name} ${offices[view.office]} results map`,
  );
  const shapes = document.createDocumentFragment(),
    labels = document.createDocumentFragment();
  for (const f of currentFeatures) {
    const result = featureStatus(f),
      p = document.createElementNS(ns, "path");
    const clickable =
      view.scope === "US"
        ? view.office === "house"
          ? !!data.coverage[f.state]?.includes(f.id)
          : !!data.coverage[f.id]
        : view.office === "house"
          ? racesFor(view.scope).includes(f.id)
          : data.states[view.scope].county_detail !== false;
    p.setAttribute("d", f.path);
    p.setAttribute("fill", colors[result.status]);
    p.setAttribute(
      "class",
      "feature" +
        (clickable ? " clickable" : "") +
        (selectedFeature(f) ? " selected" : ""),
    );
    p.dataset.id = f.id;
    p.dataset.status = result.status;
    const description = `${f.name}: ${descriptions[result.status]}${result.stats?.total ? " · " + number(result.stats.total) + " votes" : ""}`;
    const title = document.createElementNS(ns, "title");
    title.textContent = description;
    p.append(title);
    p.setAttribute("aria-label", description);
    p.setAttribute("role", clickable ? "button" : "img");
    if (clickable) p.setAttribute("tabindex", "0");
    p.addEventListener("click", () => activateFeature(f));
    p.addEventListener("keydown", (e) => {
      if (clickable && (e.key === "Enter" || e.key === " ")) {
        e.preventDefault();
        activateFeature(f);
      }
    });
    p.addEventListener("pointermove", (e) => {
      const box = $("map-stage").getBoundingClientRect();
      $("tooltip").textContent = description;
      $("tooltip").hidden = false;
      $("tooltip").style.left =
        Math.min(e.clientX - box.left + 12, box.width - 200) + "px";
      $("tooltip").style.top = Math.max(0, e.clientY - box.top - 45) + "px";
    });
    p.addEventListener("pointerleave", () => {
      $("tooltip").hidden = true;
    });
    shapes.append(p);
    if (view.scope === "US" && view.office !== "house") {
      const t = document.createElementNS(ns, "text");
      t.setAttribute("x", f.center[0]);
      t.setAttribute("y", f.center[1]);
      t.setAttribute("class", "map-label");
      t.textContent = f.id;
      labels.append(t);
    }
  }
  $("shapes").replaceChildren(shapes);
  $("map-labels").replaceChildren(labels);
  $("tooltip").hidden = true;
}
function renderNationalPanel() {
  $("result-scope").textContent = "OUR COVERAGE";
  $("result-title").textContent = offices[view.office];
  $("result-subtitle").textContent = "United States · Reported leads only";
  const target = $("result-content");
  target.replaceChildren();
  let count = 0,
    waiting = 0,
    unavailable = 0;
  const leads = { DEM: 0, REP: 0, OTHER: 0 };
  for (const code of Object.keys(data.coverage))
    for (const race of racesFor(code)) {
      count++;
      const s = statusFor(code, race).status;
      if (s in leads) leads[s]++;
      else if (s === "unavailable") unavailable++;
      else waiting++;
    }
  target.append(
    element(
      "p",
      `${count} covered ${count === 1 ? "race" : "races"} · ${waiting} awaiting votes or tied${unavailable ? " · " + unavailable + " unavailable" : ""}`,
      "empty",
    ),
  );
  if (count) {
    const score = element("div", undefined, "score");
    for (const [party, label] of [
      ["DEM", "Democratic"],
      ["REP", "Republican"],
      ["OTHER", "Other"],
    ]) {
      const box = element("div");
      box.style.borderColor = colors[party];
      box.append(
        element("strong", String(leads[party])),
        element("span", label),
      );
      score.append(box);
    }
    target.append(score);
    target.append(
      element(
        "p",
        "Counts reflect only the races we cover. They do not indicate chamber control or elected winners.",
        "empty",
      ),
    );
  } else
    target.append(
      element(
        "p",
        "We are not collecting governor results yet. The map shows which states have an election.",
        "empty",
      ),
    );
  for (const [code, state] of Object.entries(data.states)) {
    const ids = racesFor(code);
    const b = element("button", state.name, "state-card");
    b.append(
      element(
        "span",
        ids.length
          ? `${ids.length} covered ${ids.length === 1 ? "race" : "races"} · ${state.availability === "unavailable" ? "Data unavailable" : state.publication ? (state.publication.status === "test" ? "Test publication" : "Unofficial publication") : "Awaiting first publication"}`
          : isElection(code)
            ? "Election held · outside our coverage"
            : "No election for this office",
      ),
    );
    b.addEventListener("click", () => selectState(code));
    target.append(b);
  }
}
function renderStatePanel() {
  const code = view.scope,
    state = data.states[code],
    race = raceFor(code),
    contest = state.races[race];
  $("result-scope").textContent = view.county
    ? "COUNTY RESULTS"
    : "STATE RESULTS";
  $("result-title").textContent =
    view.office === "house"
      ? race || offices[view.office]
      : offices[view.office];
  const county = contest?.counties[view.county];
  $("result-subtitle").textContent =
    (county ? county.name + " County · " : "") + state.name;
  const target = $("result-content");
  target.replaceChildren();
  const result = statusFor(code, race, view.county),
    stats = result.stats;
  if (result.status === "no_election") {
    target.append(
      element(
        "p",
        `${state.name} has no ${view.office === "governor" ? "governor" : "Senate"} election on November 3, 2026.`,
        "empty",
      ),
    );
    return;
  }
  if (result.status === "not_covered") {
    target.append(
      element(
        "p",
        "This office is outside our coverage in this state.",
        "empty",
      ),
    );
    return;
  }
  if (result.status === "unavailable") {
    target.append(
      element(
        "p",
        "Published results are unavailable. Retry shortly; this is not a zero-vote report.",
        "empty",
      ),
    );
    return;
  }
  if (!state.publication) {
    target.append(
      element(
        "p",
        "Coverage is enabled. Awaiting the first validated publication.",
        "empty",
      ),
    );
    return;
  }
  const total = element("div", undefined, "total");
  total.append(
    element("span", methodNames[view.method] + " · candidate votes"),
    element("strong", stats?.total == null ? "—" : number(stats.total)),
  );
  const leadText =
    result.status === "awaiting_votes"
      ? "Awaiting reported votes"
      : result.status === "tie"
        ? "The leading candidates are tied"
        : `${stats.candidates[0].name} leads${stats.margin !== null ? " by " + number(stats.margin) + " votes" : ""}`;
  total.append(element("p", leadText));
  target.append(total);
  for (const candidate of stats?.candidates || []) {
    const box = element("div", undefined, "candidate"),
      head = element("div", undefined, "candidate-head"),
      name = element("div", candidate.name, "candidate-name"),
      votes = element(
        "div",
        candidate.votes == null ? "—" : number(candidate.votes),
        "candidate-votes",
      );
    name.append(
      element("div", candidate.party || "Party unavailable", "party"),
    );
    votes.append(
      element(
        "span",
        candidate.share === null
          ? "—"
          : (candidate.share * 100).toFixed(1) + "%",
      ),
    );
    head.append(name, votes);
    const bar = element("div", undefined, "bar"),
      fill = element("i");
    fill.style.width = 100 * (candidate.share || 0) + "%";
    fill.style.background = colors[candidate.group];
    bar.append(fill);
    box.append(head, bar);
    target.append(box);
  }
  if (code === "KY")
    target.append(
      element(
        "p",
        "Kentucky combines absentee votes. Separate early, mail and provisional counts are unavailable. Precinct reporting status is not a percentage of votes counted.",
        "empty",
      ),
    );
  if (state.county_detail === false)
    target.append(
      element(
        "p",
        "Race totals only. County results and vote-method breakdowns are unavailable in this adapter.",
        "empty",
      ),
    );
}
function renderMetadata() {
  const national = view.scope === "US",
    state = national ? null : data.states[view.scope],
    publication = state?.publication,
    manual = state?.acquisition?.method === "manual",
    anyManual = Object.values(data.states).some(
      (s) => s.acquisition?.method === "manual",
    );
  const test =
    data.mode === "demo" ||
    (national
      ? Object.values(data.states).some((s) => s.publication?.status === "test")
      : publication?.status === "test");
  $("publication-status").textContent =
    data.mode === "demo"
      ? "SIMULATED DEMO"
      : test
        ? "TEST PUBLICATIONS"
        : publication
          ? manual ? "UNOFFICIAL · MANUAL SNAPSHOT" : "UNOFFICIAL RETURNS"
          : national
            ? "OFFICIAL-SOURCE RETURNS"
            : "AWAITING PUBLICATION";
  $("published-time").textContent = national
    ? "Each state publishes independently"
    : publication
      ? "Published " + date(publication.published_at)
      : "Awaiting a validated state release";
  $("notice").textContent = stale
    ? "Refresh failed. Showing the last successful snapshot; displayed results may be stale."
    : data.mode === "demo"
      ? "SIMULATED DEMO — All vote counts and candidate names are examples."
      : test
        ? "TEST DATA — These publications are for pipeline validation. Do not interpret them as live Election Day returns."
        : manual
          ? "Manually imported official results. Source report: " + date(publication?.source_modified_at) + ". Results update when a new file is imported. A party lead is not a race call."
          : "Unofficial returns from official state sources. A party lead is not a race call. Gray and patterned regions are explained in the legend." +
            (national && anyManual ? " Includes manually updated states; select a state for its report timestamp." : "");
  $("notice").classList.toggle("alert", stale || test);
  $("connection").textContent = stale
    ? "Refresh failed · stale view"
    : manual ? "Connected · results updated manually" : "Updated " + new Date().toLocaleTimeString();
  $("observed-time").textContent = national
    ? "Per-state timestamps"
    : date(publication?.first_observed_at);
  $("publication-id").textContent = national
    ? "Independent state releases"
    : publication?.publication_id || "None yet";
  $("source-link").textContent = national
    ? "Official state election agencies ↗"
    : state.name + " election results ↗";
  $("source-link").href = national
    ? "https://www.fec.gov/introduction-campaign-finance/election-results-and-voting-information/"
    : state.source_page;
}
function renderControls() {
  const national = view.scope === "US",
    state = data.states[view.scope];
  const aggregate =
    !national && state.county_detail === false && view.office !== "house";
  if (aggregate) view.county = null;
  $("page-title").textContent = national ? "United States" : state.name;
  document.title = `${national ? "United States" : state.name} · ${offices[view.office]} · 2026 Results`;
  for (const b of document.querySelectorAll("[data-office]"))
    b.setAttribute("aria-selected", String(b.dataset.office === view.office));
  $("scope").value = view.scope;
  $("national").hidden = national;
  $("overview").hidden = national || !view.county;
  $("map-title").textContent =
    `${offices[view.office]} · ${national ? "National results" : view.office === "house" ? "District results" : aggregate ? "State totals · county detail unavailable" : "County results"}`;
  $("map-description").textContent = national
    ? "Select a covered state to explore its returns."
    : "Color shows the leading candidate’s party, not a race call.";
  const methods = national ? ["total_votes"] : state.methods || ["total_votes"];
  if (!methods.includes(view.method)) view.method = "total_votes";
  $("method").replaceChildren(
    ...methods.map((m) => new Option(methodNames[m], m)),
  );
  $("method").value = view.method;
  $("method").disabled = national;
  const options = currentFeatures.filter((f) =>
    national
      ? view.office === "house"
        ? !!data.coverage[f.state]?.includes(f.id)
        : !!data.coverage[f.id]
      : view.office !== "house" || racesFor(view.scope).includes(f.id),
  );
  $("area").replaceChildren(
    new Option(
      national
        ? "Select a covered area…"
        : view.office === "house"
          ? "Select a district…"
          : "Select a county…",
      "",
    ),
    ...options
      .slice()
      .sort((a, b) => a.name.localeCompare(b.name))
      .map((f) => new Option(f.name, f.id)),
  );
  $("area").value = national
    ? ""
    : view.office === "house"
      ? raceFor(view.scope)
      : view.county || "";
  $("area").disabled = aggregate;
  $("coverage-footer").textContent =
    "Refreshes every 15 seconds · Coverage: " +
    Object.keys(data.coverage).join(", ");
  $("coverage-label").textContent = national
    ? `${Object.keys(data.coverage).length} states covered · ${Object.keys(data.coverage).flatMap(racesFor).length} races for this office`
    : `${state.name} · ${racesFor(view.scope).length} covered races for this office`;
  const list = $("race-list");
  list.replaceChildren();
  if (!national && view.office === "house")
    for (const id of racesFor(view.scope)) {
      const b = element(
        "button",
        id,
        id === raceFor(view.scope) ? "active" : "",
      );
      b.addEventListener("click", () => {
        view.race = id;
        view.county = null;
        writeLocation();
        render();
      });
      list.append(b);
    }
}
function render() {
  if (!data) return;
  drawMap();
  renderControls();
  renderMetadata();
  if (view.scope === "US") renderNationalPanel();
  else renderStatePanel();
}
async function refresh() {
  if (fetching) return;
  fetching = true;
  try {
    const response = await fetch("/api/national", {
      cache: "no-store",
      headers: etag ? { "If-None-Match": etag } : {},
      signal: AbortSignal.timeout(10000),
    });
    if (response.status !== 304) {
      if (!response.ok) throw Error("API unavailable");
      const fresh = await response.json();
      if (!fresh.states || !fresh.calendar || !fresh.coverage)
        throw Error("Invalid snapshot");
      data = fresh;
      etag = response.headers.get("etag");
    }
    stale = false;
    render();
  } catch (error) {
    stale = true;
    if (data) renderMetadata();
    else {
      $("notice").textContent =
        "Published results are unavailable. Retrying automatically. This is not a zero-vote report.";
      $("notice").classList.add("alert");
      $("connection").textContent = "Data unavailable";
    }
  } finally {
    fetching = false;
  }
}
async function start() {
  try {
    const response = await fetch("/us-map.json?v=expansion2");
    if (!response.ok) throw Error("Map unavailable");
    geometry.US = await response.json();
    await refresh();
    if (!data) throw Error("Results unavailable");
    $("scope").append(
      ...geometry.US.states
        .slice()
        .sort((a, b) => a.name.localeCompare(b.name))
        .map((f) => {
          const o = new Option(f.name, f.id);
          o.disabled = !data.coverage[f.id];
          return o;
        }),
    );
    readLocation();
    if (view.scope === "US" || (await loadGeometry(view.scope))) render();
  } catch (error) {
    $("notice").textContent = "Unable to load the dashboard. Refresh to retry.";
  }
  setInterval(refresh, 15000);
}
for (const b of document.querySelectorAll("[data-office]"))
  b.addEventListener("click", () => {
    view.office = b.dataset.office;
    view.race = null;
    view.county = null;
    zoom = 1;
    writeLocation();
    render();
  });
$("scope").addEventListener("change", () => {
  const code = $("scope").value;
  if (code === "US") {
    view.scope = "US";
    view.race = null;
    view.county = null;
    zoom = 1;
    writeLocation();
    render();
  } else selectState(code);
});
$("national").addEventListener("click", () => {
  view.scope = "US";
  view.race = null;
  view.county = null;
  zoom = 1;
  writeLocation();
  render();
});
$("overview").addEventListener("click", () => {
  view.county = null;
  writeLocation();
  render();
});
$("area").addEventListener("change", () => {
  const feature = currentFeatures.find((f) => f.id === $("area").value);
  if (feature) activateFeature(feature);
});
$("method").addEventListener("change", () => {
  view.method = $("method").value;
  render();
});
$("refresh").addEventListener("click", refresh);
for (const [id, change] of [
  ["zoom-in", 1.4],
  ["zoom-out", 1 / 1.4],
  ["reset-map", 0],
])
  $(id).addEventListener("click", () => {
    zoom = change ? Math.max(1, Math.min(5, zoom * change)) : 1;
    drawMap();
  });
async function restoreLocation() {
  readLocation();
  zoom = 1;
  if (view.scope === "US" || (await loadGeometry(view.scope))) render();
}
window.addEventListener("popstate", restoreLocation);
window.addEventListener("hashchange", restoreLocation);
start();

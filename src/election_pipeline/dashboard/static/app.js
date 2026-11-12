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
  post_election: "Post-election (late mail)",
  mail: "Mail ballots",
  in_person: "In person",
};
const colors = {
  DEM: "#2775c7",
  REP: "#dc5259",
  OTHER: "#ecc650",
  awaiting_votes: "#a2b1c4",
  no_election: "#f1f3f6",
  not_covered: "url(#uncovered)",
  awaiting_certified: "url(#awaiting-certified)",
  tie: "url(#tie)",
  UNKNOWN: "url(#tie)",
  unavailable: "url(#unavailable)",
  partial: "url(#partial)",
};
const descriptions = {
  awaiting_votes: "Covered · awaiting votes",
  no_election: "No election for this office",
  not_covered: "Election held · outside our coverage",
  awaiting_certified: "Election held · awaiting certified results",
  tie: "Tied vote",
  UNKNOWN: "Leading candidate party unavailable",
  unavailable: "Published data unavailable",
  partial: "Partial county coverage · statewide lead unavailable",
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
  details = {},
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
// No election-night source is collected; results come from the certified canvass.
function awaitingCertified(code) {
  return (data.calendar.awaiting_certified || []).includes(code);
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
    return {
      status: awaitingCertified(code) ? "awaiting_certified" : "not_covered",
    };
  const state = data.states[code];
  if (!state || state.availability !== "available")
    return { status: "unavailable" };
  const contest = state.races[race];
  if (county && state.county_detail === false) return { status: "unavailable" };
  // County detail arrives with the state's own snapshot; until then nothing is shown.
  if (county && !contest?.counties) return { status: "unavailable" };
  // The state hid a small count here for voter privacy; the county is left blank.
  if (county && contest?.county_privacy_masked && !contest.counties?.[county])
    return { status: "unavailable", masked: true };
  const feed = county ? state.county_feeds?.[county] : null;
  if (feed?.collection_status === "not_collected")
    return { status: "not_covered" };
  if (feed?.collection_status === "unavailable")
    return { status: "unavailable" };
  const stats = county
    ? contest?.counties?.[county]?.stats[view.method]
    : contest?.stats[view.scope === "US" ? "total_votes" : view.method];
  if (
    !county &&
    state.publication &&
    state.county_collection &&
    !state.county_collection.complete
  )
    return { status: "partial", stats };
  if (!state.publication || !stats || stats.total == null || stats.total === 0)
    return { status: "awaiting_votes", stats };
  return {
    status: stats.status === "tie" ? "tie" : stats.leader || "UNKNOWN",
    stats,
    staleCounty: feed?.collection_status === "stale",
  };
}
async function loadGeometry(code) {
  if (geometry[code]) return true;
  try {
    const response = await fetch("/" + code.toLowerCase() + "-map.json");
    if (!response.ok) throw Error("Map unavailable");
    geometry[code] = await response.json();
    return true;
  } catch (error) {
    $("notice").textContent = "State map unavailable. Refresh to retry.";
    return false;
  }
}
// The national snapshot omits county detail; a state's full snapshot is loaded when it is
// viewed, with its own ETag so unchanged polls are 304s.
async function loadState(code) {
  const cached = details[code];
  const response = await fetch("/api/states/" + encodeURIComponent(code), {
    cache: "no-store",
    headers: cached?.etag ? { "If-None-Match": cached.etag } : {},
    signal: AbortSignal.timeout(10000),
  });
  if (response.status !== 304) {
    if (!response.ok) throw Error("State unavailable");
    const state = await response.json();
    if (!state.races) throw Error("Invalid state snapshot");
    details[code] = { etag: response.headers.get("etag"), state };
  }
  if (details[code] && data.states[code]?.availability === "available")
    data.states[code] = { ...data.states[code], ...details[code].state };
}
async function loadDetail(code) {
  try {
    await loadState(code);
  } catch (error) {
    $("notice").textContent =
      "County detail is unavailable right now. Retrying automatically.";
  }
}
async function selectState(code, race = null) {
  if (!data.coverage[code] || !(await loadGeometry(code))) return;
  await loadDetail(code);
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
    if (result.staleCounty) p.dataset.stale = "true";
    const code =
      view.scope === "US"
        ? view.office === "house"
          ? f.state
          : f.id
        : view.scope;
    const firstChoice =
      data.states[code]?.vote_basis === "first_choice_listed_candidates";
    const partial = data.states[code]?.county_collection;
    const partialText =
      result.status === "partial"
        ? ` · ${partial.with_results}/${partial.expected} counties with accepted counts`
        : "";
    const description = `${f.name}: ${descriptions[result.status]}${partialText}${result.staleCounty ? " · stale county source" : ""}${firstChoice ? " · listed candidates, first choice only" : ""}${result.stats?.total ? " · " + number(result.stats.total) + " votes" : ""}`;
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
      // A state too small for its label has it off the coast, joined by a leader line.
      const [x, y] = f.callout || f.center;
      if (f.callout) {
        const leader = document.createElementNS(ns, "line");
        leader.setAttribute("x1", f.center[0]);
        leader.setAttribute("y1", f.center[1]);
        leader.setAttribute("x2", x - 3);
        leader.setAttribute("y2", y - 4);
        leader.setAttribute("class", "map-leader");
        labels.append(leader);
      }
      t.setAttribute("x", x);
      t.setAttribute("y", y);
      t.setAttribute("class", f.callout ? "map-label callout" : "map-label");
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
    unavailable = 0,
    partial = 0;
  const leads = { DEM: 0, REP: 0, OTHER: 0 };
  for (const code of Object.keys(data.coverage))
    for (const race of racesFor(code)) {
      count++;
      const s = statusFor(code, race).status;
      if (s in leads) leads[s]++;
      else if (s === "unavailable") unavailable++;
      else if (s === "partial") partial++;
      else waiting++;
    }
  target.append(
    element(
      "p",
      `${count} covered ${count === 1 ? "race" : "races"} · ${waiting} awaiting votes or tied${partial ? " · " + partial + " with partial county coverage" : ""}${unavailable ? " · " + unavailable + " unavailable" : ""}`,
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
        "Counts reflect only the races we cover. Partial county aggregates are excluded from party lead counts. These counts do not indicate chamber control or elected winners.",
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
          ? `${ids.length} covered ${ids.length === 1 ? "race" : "races"} · ${state.availability === "unavailable" ? "Data unavailable" : state.publication ? { test: "Test publication", official: "Official results" }[state.publication.status] || "Unofficial publication" : "Awaiting first publication"}`
          : isElection(code)
            ? descriptions[
                awaitingCertified(code) ? "awaiting_certified" : "not_covered"
              ]
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
  const county = contest?.counties?.[view.county];
  const feed = state.county_feeds?.[view.county];
  const partial = state.county_collection;
  if (!view.county && partial)
    $("result-scope").textContent = "COLLECTED COUNTY TOTALS";
  // A town (Vermont) is named alone; a county is named as one.
  const unit = state.local_unit === "town" ? "" : " County";
  $("result-subtitle").textContent =
    (county || feed ? (county?.name || feed.name) + unit + " · " : "") +
    state.name;
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
  if (result.status === "awaiting_certified") {
    target.append(
      element(
        "p",
        "This state's election-night results are not collected here. Its results will be added from the state's certified canvass.",
        "empty",
      ),
    );
    return;
  }
  if (result.status === "not_covered") {
    target.append(
      element(
        "p",
        feed?.collection_status === "not_collected"
          ? "This county source has not been collected. Its votes are unknown and excluded from the displayed aggregate."
          : "This office is outside our coverage in this state.",
        "empty",
      ),
    );
    return;
  }
  if (result.status === "unavailable") {
    target.append(
      element(
        "p",
        result.masked
          ? `${state.name} hid a small vote count in this county for voter privacy, so its county result for this race is not shown. The statewide total includes it.`
          : feed?.collection_status === "unavailable"
            ? "This county has no accepted report. Its votes are unknown and excluded from the displayed aggregate."
            : "Published results are unavailable. Retry shortly; this is not a zero-vote report.",
        "empty",
      ),
    );
    if (feed?.collection_status === "unavailable") {
      target.append(
        element(
          "p",
          "Last attempted check: " +
            date(feed.last_attempt_at) +
            ". " +
            (feed.issue === "review_required"
              ? "Changed scanned report requires review."
              : "County source check failed."),
          "empty",
        ),
      );
      if (feed.source_url) {
        const link = element("a", "Original county report ↗", "text-button");
        link.href = feed.source_url;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        target.append(link);
      }
    }
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
  const firstChoice = state.vote_basis === "first_choice_listed_candidates";
  const countyBasis = state.vote_basis === "covered_counties_named_candidates";
  if (partial && !view.county)
    target.append(
      element(
        "p",
        `${partial.with_results} of ${partial.expected} counties have accepted counts; ${partial.current} sources currently confirmed, ${partial.manual || 0} manual snapshots, ${partial.stale - (partial.manual || 0)} stale, ${partial.unavailable} unavailable. This is a partial county aggregate, not a statewide total.`,
        "empty",
      ),
    );
  if (contest?.county_privacy_masked && !view.county)
    target.append(
      element(
        "p",
        `${state.name} hides some small county counts for voter privacy. Counties with a hidden count are left out of this race's county detail, so they add up to less than the statewide total shown here.`,
        "empty",
      ),
    );
  if (countyBasis)
    target.append(
      element(
        "p",
        "Named Senate candidates only. Percentages exclude write-ins; reported write-in tallies are shown separately for each county. Precinct reporting is not the percentage of all ballots counted.",
        "empty",
      ),
    );
  if (feed) {
    target.append(
      element(
        "p",
        (feed.acquisition === "manual"
          ? "MANUAL SNAPSHOT · A new reviewed import is required to update these counts. "
          : feed.collection_status === "stale"
            ? "STALE SOURCE · Last accepted counts retained. "
            : "") +
          (feed.acquisition === "manual"
            ? "Imported report observed: "
            : "Last successful check: ") +
          date(feed.last_success_at) +
          ". Report: " +
          (feed.report_generated_at
            ? date(feed.report_generated_at)
            : feed.report_date || "time unavailable") +
          ".",
        "empty",
      ),
    );
    if (feed.issue === "review_required")
      target.append(
        element(
          "p",
          "Changed scanned report requires review; these counts are from the last accepted file.",
          "empty",
        ),
      );
    if (feed.source_note)
      target.append(element("p", feed.source_note, "empty"));
    const scope = [];
    if (feed.absentee_scope === "excluded")
      scope.push("Absentee ballots excluded");
    else if (feed.absentee_scope === "included")
      scope.push("Absentee votes included");
    if (feed.affidavit_scope === "excluded")
      scope.push("Affidavit ballots excluded");
    if (scope.length) target.append(element("p", scope.join(" · "), "empty"));
    target.append(
      element(
        "p",
        feed.write_in_votes == null
          ? "Write-in tally not supplied."
          : `${feed.write_in_label || "Write-ins"}: ${number(feed.write_in_votes)} · excluded from named-candidate percentages.`,
        "empty",
      ),
    );
    if (feed.source_url) {
      const link = element("a", "Original county report ↗", "text-button");
      link.href = feed.source_url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      target.append(link);
    }
  }
  if (firstChoice)
    target.append(
      element(
        "p",
        "First-choice votes · Round 1 · CSV-listed candidates. Later ranked-choice tabulations are not included. Percentages use listed candidate votes; unlisted vote categories are excluded.",
        "empty",
      ),
    );
  total.append(
    element(
      "span",
      firstChoice
        ? "Listed first-choice candidate votes"
        : countyBasis
          ? "Named candidate votes · collected counties"
          : methodNames[view.method] + " · candidate votes",
    ),
    element("strong", stats?.total == null ? "—" : number(stats.total)),
  );
  const leadText =
    result.status === "awaiting_votes"
      ? "Awaiting reported votes"
      : result.status === "tie"
        ? "The leading candidates are tied"
        : !stats?.total
          ? "No reported votes among the collected named candidates"
          : `${stats.candidates[0].name} leads${firstChoice ? " among listed candidates in first-choice votes" : countyBasis && !view.county ? " within the collected counties" : ""}${stats.margin !== null ? " by " + number(stats.margin) + " votes" : ""}`;
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
  if (state.method_note)
    target.append(element("p", state.method_note, "empty"));
  if (state.county_detail === false)
    target.append(
      element(
        "p",
        (state.methods || []).length > 1
          ? "Race totals only. County results are unavailable in this adapter."
          : "Race totals only. County results and vote-method breakdowns are unavailable in this adapter.",
        "empty",
      ),
    );
}
function renderMetadata() {
  const national = view.scope === "US",
    state = national ? null : data.states[view.scope],
    publication = state?.publication,
    manual = state?.acquisition?.method === "manual",
    official = publication?.status === "official",
    anyManual = Object.values(data.states).some(
      (s) => s.acquisition?.method === "manual",
    ),
    anyOfficial = Object.values(data.states).some(
      (s) => s.publication?.status === "official",
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
          ? official
            ? "OFFICIAL RESULTS"
            : manual
              ? "UNOFFICIAL · MANUAL SNAPSHOT"
              : "UNOFFICIAL RETURNS"
          : national
            ? "OFFICIAL-SOURCE RETURNS"
            : "AWAITING PUBLICATION";
  $("published-time").textContent = national
    ? "Each state publishes independently"
    : publication
      ? "Published " +
        date(publication.published_at) +
        (publication.votes_changed_at
          ? " · votes last changed " + date(publication.votes_changed_at)
          : "")
      : "Awaiting a validated state release";
  $("notice").textContent = stale
    ? "Refresh failed. Showing the last successful snapshot; displayed results may be stale."
    : data.mode === "demo"
      ? "SIMULATED DEMO — All vote counts and candidate names are examples."
      : test
        ? "TEST DATA — These publications are for pipeline validation. Do not interpret them as live Election Day returns."
        : official
          ? "Official results as certified by the state" +
            (publication.attestation?.certified_on
              ? " on " + publication.attestation.certified_on
              : "") +
            ". A party lead is not a race call."
          : manual
            ? "Manually imported official results. Source report: " +
              date(publication?.source_modified_at) +
              ". Results update when a new file is imported. A party lead is not a race call."
            : "Unofficial returns from official election sources. A party lead is not a race call. Gray and patterned regions are explained in the legend." +
              (national && anyManual
                ? " Includes manually updated states; select a state for its report timestamp."
                : "") +
              (national && anyOfficial
                ? " States whose results are certified show their official results."
                : "");
  $("results-note").textContent =
    "Reported candidate votes are not ballot turnout. " +
    (official
      ? "These are the state's certified official results."
      : "These are unofficial returns; no projections or race calls.");
  if (
    (national &&
      Object.values(data.states).some(
        (s) => s.vote_basis === "first_choice_listed_candidates",
      )) ||
    state?.vote_basis === "first_choice_listed_candidates"
  )
    $("notice").textContent +=
      " Alaska shows first-choice votes for CSV-listed candidates. Later ranked-choice results and unlisted vote categories are not included.";
  if (
    (national &&
      Object.values(data.states).some(
        (s) => s.county_collection && !s.county_collection.complete,
      )) ||
    state?.county_collection
  )
    $("notice").textContent +=
      " Mississippi has partial county collection. The national map does not assign it a statewide party lead; select the state to inspect collected counties and source timestamps.";
  $("notice").classList.toggle("alert", stale || test);
  $("connection").textContent = stale
    ? "Refresh failed · stale view"
    : manual
      ? "Connected · results updated manually"
      : "Updated " + new Date().toLocaleTimeString();
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
  $("overview").textContent = state?.county_collection
    ? "Collected county totals"
    : "Statewide results";
  $("map-title").textContent =
    `${offices[view.office]} · ${national ? "National results" : view.office === "house" ? "District results" : aggregate ? "State totals · county detail unavailable" : "County results"}`;
  $("map-description").textContent = national
    ? "Select a covered state to explore its returns."
    : state.county_collection
      ? "County colors show named-candidate leads in collected reports. Patterned counties are outside collection or unavailable; stale sources have dashed borders."
      : state.vote_basis === "first_choice_listed_candidates"
        ? "Color shows the first-choice leader among CSV-listed candidates. Later ranked-choice results are not included."
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
          : data.states[view.scope]?.local_unit === "town"
            ? "Select a town…"
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
    : state.county_collection && view.office === "senate"
      ? `${state.name} · ${state.county_collection.with_results}/${state.county_collection.expected} counties with accepted counts`
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
    if (view.scope !== "US") await loadState(view.scope);
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
    const response = await fetch("/us-map.json");
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
    if (view.scope !== "US") await loadDetail(view.scope);
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
  if (view.scope !== "US") await loadDetail(view.scope);
  if (view.scope === "US" || (await loadGeometry(view.scope))) render();
}
window.addEventListener("popstate", restoreLocation);
window.addEventListener("hashchange", restoreLocation);
start();

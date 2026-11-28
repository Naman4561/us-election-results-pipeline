const { JSDOM } = require("jsdom");
const fs = require("fs");
const assert = require("node:assert/strict");
const path = require("node:path");
const { execFileSync } = require("node:child_process");
const base = path.join(__dirname, "../src/election_pipeline/dashboard/static/");
const { test } = require("node:test");

// The demo payloads of every state, as the page would fetch them (one slow Python call).
let demo;
const demoPayload = () => {
  demo ||= execFileSync(
    process.env.PYTHON || "python",
    [
      "-c",
      "import json; from fastapi.testclient import TestClient; from election_pipeline.dashboard.server import create_app; c = TestClient(create_app(demo=True)); n = c.get('/api/national').json(); print(json.dumps({'national': n, 'states': {k: c.get('/api/states/' + k).json() for k in n['states']}}))",
    ],
    { encoding: "utf8", maxBuffer: 10 * 1024 * 1024 },
  );
  return JSON.parse(demo);
};
// The page, loaded over those payloads (which the test may change before a refresh).
async function loadPage(t, payload) {
  const dom = new JSDOM(fs.readFileSync(base + "index.html", "utf8"), {
    runScripts: "outside-only",
    url: "http://localhost/",
  });
  t.after(() => dom.window.close());
  const w = dom.window;
  w.AbortSignal = AbortSignal;
  w.fetch = async (url) => {
    const body =
      url === "/api/national"
        ? payload.national
        : url.startsWith("/api/states/")
          ? payload.states[decodeURIComponent(url.slice(12))]
          : JSON.parse(fs.readFileSync(base + url.split("?")[0], "utf8"));
    return {
      ok: true,
      status: 200,
      json: async () => body,
      headers: { get: () => null },
    };
  };
  w.eval(fs.readFileSync(base + "app.js", "utf8"));
  const settle = () => new Promise((r) => setTimeout(r, 25));
  await settle();
  const d = w.document;
  const click = (s) => {
    assert(d.querySelector(s), s);
    d.querySelector(s).dispatchEvent(
      new w.MouseEvent("click", { bubbles: true }),
    );
  };
  return { w, d, click, settle };
}

// A seat held more than once (a special election beside the regular race, a runoff beside the
// November one) is shown as tabs, like the rounds of a ranked-choice race.
test("a seat with a special election or a runoff is shown as tabs", async (t) => {
  const payload = demoPayload();
  const { w, d, click, settle } = await loadPage(t, payload);
  const tabs = () =>
    [...d.querySelectorAll(".variant-tabs button")].map((b) => [
      b.textContent,
      b.getAttribute("aria-selected"),
    ]);
  // A state with a Senate and a House race, none ranked-choice (whose panel shows rounds).
  const code = Object.keys(payload.national.coverage).find(
    (c) =>
      payload.national.coverage[c].includes(c + "-SEN") &&
      payload.national.coverage[c].some((id) => /-(AL|[0-9]{2})$/.test(id)) &&
      !Object.values(payload.states[c].races).some((r) => r.ranked_choice),
  );
  assert(code, "a demo state with a Senate race");
  const senate = code + "-SEN";
  const house = payload.national.coverage[code].find(
    (id) => !/-SEN$|-GOV$/.test(id),
  );
  assert(house, "a demo state with a House race");

  // Without a special or runoff there are no tabs.
  click(`path[data-id="${code}"]`);
  await settle();
  assert.equal(d.querySelector("h1").textContent.length > 0, true);
  assert.deepEqual(tabs(), []);

  // The state's special Senate election appears in its payload: two tabs, the regular race first.
  const addRace = (id, seat, tab, baseTab, total) => {
    for (const races of [
      payload.national.states[code].races,
      payload.states[code].races,
    ]) {
      races[id] = JSON.parse(JSON.stringify(races[seat]));
      races[id].variant = { seat, tab };
      races[seat].variant = { seat, tab: baseTab };
      races[id].stats.total_votes.total = total;
    }
    payload.national.coverage[code].push(id);
  };
  addRace(senate + "-S", senate, "Special", "Regular", 424242);
  click("#refresh");
  await settle();
  assert.deepEqual(tabs(), [
    ["Regular", "true"],
    ["Special", "false"],
  ]);
  assert(!d.querySelector("#result-content").textContent.includes("424,242"));

  // Choosing the special shows that race, and the address holds the choice.
  click(`.variant-tabs [data-race="${senate}-S"]`);
  await settle();
  assert.deepEqual(tabs(), [
    ["Regular", "false"],
    ["Special", "true"],
  ]);
  assert(d.querySelector("#result-content").textContent.includes("424,242"));
  assert(w.location.hash.includes(senate + "-S"));
  click(`.variant-tabs [data-race="${senate}"]`);
  await settle();
  assert(!d.querySelector("#result-content").textContent.includes("424,242"));

  // A runoff of a House district is its tab; the district list still lists the district once.
  click('[data-office="house"]');
  await settle();
  const listed = d.querySelectorAll("#race-list button").length;
  assert(listed >= 1);
  addRace(house + "-R", house, "Runoff", "General", 717171);
  click("#refresh");
  await settle();
  assert.equal(
    d.querySelectorAll("#race-list button").length,
    listed,
    "a runoff is a tab of its district, not another district",
  );
  const active = [...d.querySelectorAll("#race-list button")].find((b) =>
    b.classList.contains("active"),
  );
  assert(active);
  click(
    `#race-list button:nth-child(${[...d.querySelectorAll("#race-list button")].findIndex((b) => b.textContent === house) + 1})`,
  );
  await settle();
  assert.deepEqual(tabs(), [
    ["General", "true"],
    ["Runoff", "false"],
  ]);
  click(`.variant-tabs [data-race="${house}-R"]`);
  await settle();
  assert(
    d.querySelector("#result-content").textContent.includes("717,171"),
    JSON.stringify({
      tabs: tabs(),
      hash: w.location.hash,
      content: d.querySelector("#result-content").textContent.slice(0, 300),
    }),
  );
  assert.equal(
    d.querySelectorAll("#race-list button").length,
    listed,
    "the district list is unchanged while its runoff is shown",
  );
  // The seat, not the runoff, is the selected district in the list.
  assert.equal(
    [...d.querySelectorAll("#race-list button")]
      .filter((b) => b.classList.contains("active"))
      .map((b) => b.textContent)
      .join(),
    house,
  );
});

// A House seat nobody opposed (Florida's District 10) is listed as a seat: no ballot, no votes.
// The demo payloads cannot cover every Florida race, so a district of a demo state is taken off
// its ballot here; the definition's real seat is checked in tests/test_uncontested.py.
test("an uncontested seat is counted, drawn and explained", async (t) => {
  const payload = demoPayload();
  const district = (id) => /-(AL|[0-9]{2})$/.test(id);
  const code = Object.keys(payload.national.coverage).find(
    (c) =>
      !Object.values(payload.states[c].races).some((r) => r.ranked_choice) &&
      payload.national.coverage[c].filter(district).length >= 2,
  );
  assert(code, "a demo state with two House districts");
  const id = payload.national.coverage[code].find(district);
  const other = payload.national.coverage[code].find(
    (c) => district(c) && c !== id,
  );
  const seat = {
    winner: "Pat Example",
    party: "DEM",
    basis: "Unopposed, so not on the ballot.",
    source: "https://example.org/listing",
  };
  for (const states of [payload.national.states, payload.states]) {
    delete states[code].races[id];
    states[code].uncontested = { [id]: seat };
  }
  payload.national.coverage[code] = payload.national.coverage[code].filter(
    (c) => c !== id,
  );
  const { d, click, settle } = await loadPage(t, payload);
  click('[data-office="house"]');
  await settle();
  const feature = d.querySelector(`path[data-id="${id}"]`);
  assert(feature, "the district is on the map");
  // Coloured by its winner's party, marked, and named in its tooltip.
  assert.equal(feature.dataset.status, "DEM");
  assert.equal(feature.dataset.uncontested, "true");
  const tip = feature.querySelector("title").textContent;
  assert(tip.includes("Pat Example") && tip.includes("unopposed"), tip);
  // The count includes it and says so.
  const label = d.querySelector("#coverage-label").textContent;
  assert(label.includes("(1 uncontested)"), label);
  // Opening it explains it, links the official source, and lists it among the state's seats.
  click(`path[data-id="${id}"]`);
  await settle();
  assert.equal(
    d.querySelector("h1").textContent,
    payload.national.states[code].name,
  );
  const panel = d.querySelector("#result-content");
  assert(panel.textContent.includes("Pat Example (DEM) · unopposed"));
  assert(panel.textContent.includes("No votes were cast or counted"));
  assert.equal(panel.querySelector("a").href, seat.source);
  const listed = [...d.querySelectorAll("#race-list button")].map(
    (b) => b.textContent,
  );
  assert(listed.includes(`${id} · unopposed`), listed.join());
  assert(listed.includes(other), listed.join());
  // A contested district of the same state is unchanged: no marker, its own results.
  click(`path[data-id="${other}"]`);
  await settle();
  assert.equal(
    d.querySelector(`path[data-id="${other}"]`).dataset.uncontested,
    undefined,
  );
  assert(!d.querySelector("#result-content").textContent.includes("unopposed"));
});

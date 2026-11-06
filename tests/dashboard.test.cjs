const { JSDOM } = require("jsdom");
const fs = require("fs");
const assert = require("node:assert/strict");
const path = require("node:path");
const { execFileSync } = require("node:child_process");
const base = path.join(__dirname, "../src/election_pipeline/dashboard/static/");
const { test } = require("node:test");
test("national/state map navigation and coverage statuses", async (t) => {
  const payload = JSON.parse(
    execFileSync(
      process.env.PYTHON || "python",
      [
        "-c",
        "import json; from fastapi.testclient import TestClient; from election_pipeline.dashboard.server import create_app; c = TestClient(create_app(demo=True)); n = c.get('/api/national').json(); print(json.dumps({'national': n, 'states': {k: c.get('/api/states/' + k).json() for k in n['states']}}))",
      ],
      { encoding: "utf8", maxBuffer: 10 * 1024 * 1024 },
    ),
  );
  const dom = new JSDOM(fs.readFileSync(base + "index.html", "utf8"), {
    runScripts: "outside-only",
    url: "http://localhost/",
  });
  t.after(() => dom.window.close());
  const w = dom.window;
  let calls = 0;
  w.AbortSignal = AbortSignal;
  w.fetch = async (url) => {
    calls++;
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
  const status = (id) =>
    d.querySelector(`path[data-id="${id}"]`).dataset.status;
  assert.equal(d.querySelectorAll("path.feature").length, 50);
  assert.equal(status("AZ"), "no_election");
  assert.equal(status("ME"), "not_covered");
  assert.equal(status("MS"), "partial");
  assert(
    d.querySelector('path[data-id="MS"] title').textContent.includes("13/82"),
  );
  click('path[data-id="MS"]');
  await settle();
  assert.equal(d.querySelector("h1").textContent, "Mississippi");
  assert.equal(d.querySelectorAll("path.feature").length, 82);
  assert.equal(
    d.querySelector("#result-scope").textContent,
    "COLLECTED COUNTY TOTALS",
  );
  assert(
    d
      .querySelector("#result-content")
      .textContent.includes("not a statewide total"),
  );
  const msCalls = calls;
  click('path[data-id="JACKSON"]');
  assert.equal(status("JACKSON"), "not_covered");
  assert(d.querySelector("#result-subtitle").textContent.includes("Jackson"));
  assert(
    d
      .querySelector("#result-content")
      .textContent.includes("votes are unknown"),
  );
  click('path[data-id="HINDS"]');
  assert(d.querySelector(".candidate-votes"));
  assert(
    d
      .querySelector("#result-content")
      .textContent.includes("Last successful check"),
  );
  payload.states.MS.county_feeds.HINDS.collection_status = "stale";
  payload.states.MS.county_feeds.HINDS.issue = "review_required";
  click("#refresh");
  await settle();
  assert.equal(d.querySelector('path[data-id="HINDS"]').dataset.stale, "true");
  assert(
    d.querySelector("#result-content").textContent.includes("STALE SOURCE"),
  );
  assert(
    d.querySelector("#result-content").textContent.includes("requires review"),
  );
  // One refresh polls the national summary and the open state; county clicks are cached.
  assert.equal(calls, msCalls + 2, "Mississippi county clicks use cached data");
  payload.states.MS.county_feeds.HINDS.issue = "manual_snapshot";
  payload.states.MS.county_feeds.HINDS.acquisition = "manual";
  payload.states.MS.county_feeds.HINDS.source_note =
    "Report generation time is unknown.";
  click("#refresh");
  await settle();
  assert(
    d.querySelector("#result-content").textContent.includes("MANUAL SNAPSHOT"),
  );
  assert(
    d
      .querySelector("#result-content")
      .textContent.includes("Imported report observed"),
  );
  assert(
    d
      .querySelector("#result-content")
      .textContent.includes("generation time is unknown"),
  );
  delete payload.states.MS.county_feeds.HINDS.acquisition;
  delete payload.states.MS.county_feeds.HINDS.source_note;
  payload.states.MS.county_feeds.HINDS.issue = "source_check_failed";
  delete payload.states.MS.races["MS-SEN"].counties.HINDS;
  payload.states.MS.county_feeds.HINDS.collection_status = "unavailable";
  click("#refresh");
  await settle();
  assert.equal(status("HINDS"), "unavailable");
  assert(!d.querySelector(".candidate-votes"));
  assert(
    d
      .querySelector("#result-content")
      .textContent.includes("no accepted report"),
  );
  click('[data-office="governor"]');
  assert.equal(d.querySelector("h1").textContent, "Mississippi");
  assert(
    d
      .querySelector("#result-content")
      .textContent.includes("has no governor election"),
  );
  click("#national");
  click('[data-office="senate"]');
  assert(
    d
      .querySelector('path[data-id="AK"] title')
      .textContent.includes("first choice only"),
  );
  assert(
    d
      .querySelector("#notice")
      .textContent.includes("Alaska shows first-choice votes"),
  );
  click('path[data-id="AK"]');
  await settle();
  assert.equal(d.querySelector("h1").textContent, "Alaska");
  assert.equal(d.querySelectorAll("path.feature").length, 1);
  assert(
    d
      .querySelector("#result-content")
      .textContent.includes("CSV-listed candidates"),
  );
  assert(d.querySelector("#result-content").textContent.includes("Round 1"));
  const alaskaCalls = calls;
  click('[data-office="governor"]');
  assert.equal(d.querySelector("h1").textContent, "Alaska");
  click('[data-office="house"]');
  assert(d.querySelector('path[data-id="AK-AL"]'));
  assert.equal(calls, alaskaCalls, "Alaska tabs use cached results");
  click("#national");
  click('[data-office="senate"]');
  click('path[data-id="KY"]');
  await settle();
  assert.equal(d.querySelector("h1").textContent, "Kentucky");
  assert.equal(d.querySelectorAll("path.feature").length, 120);
  click('[data-office="governor"]');
  assert.equal(d.querySelector("h1").textContent, "Kentucky");
  assert(
    d
      .querySelector("#result-content")
      .textContent.includes("has no governor election"),
  );
  click('[data-office="house"]');
  assert.equal(d.querySelectorAll("path.feature").length, 6);
  click("#national");
  assert.equal(d.querySelectorAll("path.feature").length, 435);
  click('path[data-id="NC-04"]');
  await settle();
  assert.equal(d.querySelector("#result-title").textContent, "NC-04");
  const before = calls;
  click('[data-office="senate"]');
  const area = d.querySelector("#area");
  area.value = "WAKE";
  area.dispatchEvent(new w.Event("change"));
  assert(d.querySelector("#result-subtitle").textContent.includes("Wake"));
  assert.equal(calls, before, "Tabs and county clicks must not request data");
  click('[data-office="governor"]');
  assert.equal(d.querySelector("h1").textContent, "North Carolina");
  click("#national");
  assert.equal(status("NC"), "no_election");
  assert.equal(status("AZ"), "not_covered");
  click('[data-office="senate"]');
  click('path[data-id="MN"]');
  await settle();
  assert.equal(d.querySelectorAll("path.feature").length, 87);
  click('[data-office="governor"]');
  assert.equal(d.querySelector("#result-title").textContent, "Governor");
  assert.equal(d.querySelectorAll("path.feature").length, 87);
  click("#national");
  click('path[data-id="NE"]');
  await settle();
  assert.equal(d.querySelectorAll("path.feature").length, 1);
  assert.equal(d.querySelector("#area").disabled, true);
  assert(
    d.querySelector("#result-content").textContent.includes("Race totals only"),
  );
  click('[data-office="house"]');
  assert.equal(d.querySelectorAll("path.feature").length, 3);
  click('path[data-id="NE-02"]');
  assert.equal(d.querySelector("#result-title").textContent, "NE-02");
  click("#national");
  click('path[data-id="TX-35"]');
  await settle();
  assert.equal(d.querySelector("#result-title").textContent, "TX-35");
  assert.equal(d.querySelectorAll("path.feature").length, 38);
  const expandedCalls = calls;
  click('path[data-id="TX-01"]');
  assert.equal(d.querySelector("#result-title").textContent, "TX-01");
  assert.equal(
    calls,
    expandedCalls,
    "Newly covered districts use cached results",
  );
  click('[data-office="senate"]');
  for (const stats of Object.values(payload.states.TX.races["TX-SEN"].stats)) {
    Object.assign(stats, {
      total: null,
      status: "unreported",
      leader: null,
      margin: null,
    });
    for (const c of stats.candidates)
      Object.assign(c, { votes: null, share: null });
  }
  click("#refresh");
  await settle();
  assert.equal(d.querySelectorAll("path.feature").length, 1);
  assert.equal(d.querySelector(".total strong").textContent, "—");
  assert.equal(d.querySelector(".candidate-votes").firstChild.textContent, "—");
  assert.equal(status("TX"), "awaiting_votes");
  click("#national");
  for (const [code, name, counties, districts] of [
    ["GA", "Georgia", 1, 14],
    ["SC", "South Carolina", 46, 7],
    ["KS", "Kansas", 105, 4],
    ["OH", "Ohio", 88, 15],
  ]) {
    click('[data-office="senate"]');
    click(`path[data-id="${code}"]`);
    await settle();
    assert.equal(d.querySelector("h1").textContent, name);
    assert.equal(d.querySelectorAll("path.feature").length, counties);
    const stateCalls = calls;
    click('[data-office="governor"]');
    assert.equal(d.querySelector("h1").textContent, name);
    click('[data-office="house"]');
    assert.equal(d.querySelectorAll("path.feature").length, districts);
    click(`path[data-id="${code}-01"]`);
    assert.equal(d.querySelector("#result-title").textContent, `${code}-01`);
    assert.equal(
      calls,
      stateCalls,
      "State tabs and race clicks use cached results",
    );
    click("#national");
  }
  click('[data-office="senate"]');
  const stats = payload.national.states.NC.races["NC-SEN"].stats.total_votes;
  for (const leader of ["DEM", "REP", "OTHER", "UNKNOWN"]) {
    Object.assign(stats, { total: 100, status: "leading", leader });
    click("#refresh");
    await settle();
    assert.equal(status("NC"), leader);
  }
  Object.assign(stats, { total: 0, status: "unreported", leader: null });
  click("#refresh");
  await settle();
  assert.equal(status("NC"), "awaiting_votes");
  Object.assign(stats, { total: 100, status: "tie", leader: null });
  click("#refresh");
  await settle();
  assert.equal(status("NC"), "tie");
  payload.national.states.NC.availability = "unavailable";
  click("#refresh");
  await settle();
  assert.equal(status("NC"), "unavailable");
  payload.national.mode = "published";
  for (const s of [
    ...Object.values(payload.national.states),
    ...Object.values(payload.states),
  ])
    if (s.publication) s.publication.status = "unofficial";
  for (const s of [payload.national.states.OH, payload.states.OH]) {
    s.acquisition = { method: "manual" };
    s.publication.source_modified_at = "2026-11-04T02:42:36Z";
  }
  click("#refresh");
  await settle();
  assert(
    d.querySelector("#notice").textContent.includes("manually updated states"),
  );
  click('path[data-id="OH"]');
  await settle();
  assert(
    d
      .querySelector("#publication-status")
      .textContent.includes("MANUAL SNAPSHOT"),
  );
  assert(
    d.querySelector("#notice").textContent.includes("new file is imported"),
  );
  assert(
    d.querySelector("#connection").textContent.includes("updated manually"),
  );
  w.fetch = async () => {
    throw Error("Network offline");
  };
  click("#refresh");
  await settle();
  assert(d.querySelector("#notice").textContent.includes("may be stale"));
  console.log(
    "PASS: national/state tabs, 435 districts, map statuses, county navigation, no click-time API calls",
  );
  dom.window.close();
});

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
        "from fastapi.testclient import TestClient; from election_pipeline.dashboard.server import create_app; print(TestClient(create_app(demo=True)).get('/api/national').text)",
      ],
      { encoding: "utf8" },
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
    const body = url.startsWith("/api/")
      ? payload
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
  assert.equal(status("CA"), "no_election");
  assert.equal(status("GA"), "not_covered");
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
  assert.equal(status("CA"), "not_covered");
  click('[data-office="senate"]');
  click('path[data-id="MN"]');
  await settle();
  assert.equal(d.querySelectorAll("path.feature").length, 87);
  click('[data-office="governor"]');
  assert(
    d
      .querySelector("#result-content")
      .textContent.includes("outside our coverage"),
  );
  click("#national");
  click('path[data-id="NE"]');
  await settle();
  assert.equal(d.querySelectorAll("path.feature").length, 1);
  assert.equal(d.querySelector("#area").disabled, true);
  assert(
    d.querySelector("#result-content").textContent.includes("Race totals only"),
  );
  click('[data-office="house"]');
  assert(
    d
      .querySelector("#result-content")
      .textContent.includes("outside our coverage"),
  );
  click("#national");
  click('path[data-id="TX-35"]');
  await settle();
  assert.equal(d.querySelector("#result-title").textContent, "TX-35");
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
  const stats = payload.states.NC.races["NC-SEN"].stats.total_votes;
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
  payload.states.NC.availability = "unavailable";
  click("#refresh");
  await settle();
  assert.equal(status("NC"), "unavailable");
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

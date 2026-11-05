const { JSDOM } = require("jsdom");
const fs = require("fs");
const assert = require("node:assert/strict");
const path = require("node:path");
const { execFileSync } = require("node:child_process");
const { test } = require("node:test");

const base = path.join(__dirname, "../src/election_pipeline/dashboard/static/");
const demo = JSON.parse(
  execFileSync(
    process.env.PYTHON || "python",
    [
      "-c",
      "from fastapi.testclient import TestClient; from election_pipeline.dashboard.server import create_app; print(TestClient(create_app(demo=True)).get('/api/analysis/nc').text)",
    ],
    { encoding: "utf8", maxBuffer: 10 * 1024 * 1024 },
  ),
);

async function open(payload, hash = "") {
  const dom = new JSDOM(fs.readFileSync(base + "analysis.html", "utf8"), {
    runScripts: "outside-only",
    url: "http://localhost/analysis.html" + hash,
  });
  const w = dom.window;
  w.fetch = async () => ({ ok: true, status: 200, json: async () => payload });
  w.setInterval = () => 0;
  w.eval(fs.readFileSync(base + "analysis.js", "utf8"));
  await new Promise((r) => setTimeout(r, 25));
  return dom;
}

test("analysis page renders races, methods, history, counties and ballots", async (t) => {
  const dom = await open(demo, "#race=NC-05&type=CIVILIAN");
  t.after(() => dom.window.close());
  const d = dom.window.document;
  assert.equal(d.getElementById("mode").textContent, "DEMO DATA");
  assert.match(d.getElementById("notice").textContent, /not election results/);
  assert.equal(d.querySelectorAll("#races tbody tr").length, 15);
  assert.equal(
    d.getElementById("race-title").textContent,
    "U.S. House · NC-05",
  );
  assert.equal(
    d.querySelector('#races tr[aria-selected="true"] td').textContent,
    "U.S. House · NC-05",
  );
  assert.equal(d.querySelectorAll("#by-method .row").length, 4);
  assert.ok(d.querySelector("#history svg.history polyline"));
  assert.equal(d.getElementById("request-type").value, "CIVILIAN");
  assert.equal(
    d
      .querySelector(".card[aria-current='true'] span")
      .textContent.split(" ·")[0],
    "CIVILIAN",
  );

  d.getElementById("race").value = "NC-SEN";
  d.getElementById("race").dispatchEvent(new dom.window.Event("change"));
  assert.equal(d.getElementById("race-title").textContent, "U.S. Senate");
  assert.match(dom.window.location.hash, /race=NC-SEN/);
  const senate = demo.results.races.find((r) => r.id === "NC-SEN");
  assert.equal(
    d.querySelectorAll("#counties tbody tr").length,
    senate.counties.length,
  );

  const first = () =>
    d.querySelector("#counties tbody td").textContent.replace("◆ ", "");
  const nameHeader = () => d.querySelector("#counties thead th button");
  nameHeader().click();
  const ascending = first();
  nameHeader().click();
  assert.notEqual(first(), ascending);
});

test("published text is never interpreted as markup", async (t) => {
  const payload = structuredClone(demo);
  payload.mode = "published";
  const hostile = '<img src=x onerror="window.pwned=1">';
  payload.results.races[0].candidates[0].name = hostile;
  payload.notes = [hostile];
  const dom = await open(payload);
  t.after(() => dom.window.close());
  const d = dom.window.document;
  assert.equal(d.querySelector("#candidates img"), null);
  assert.equal(d.querySelector("#notes img"), null);
  assert.equal(d.querySelector("#candidates b").textContent, hostile);
  assert.equal(dom.window.pwned, undefined);
});

test("missing publications are explicit, not zero", async (t) => {
  const dom = await open({
    ...demo,
    mode: "published",
    results: null,
    history: null,
    demographics: null,
  });
  t.after(() => dom.window.close());
  const d = dom.window.document;
  assert.match(
    d.getElementById("notice").textContent,
    /No North Carolina results/,
  );
  assert.match(
    d.getElementById("ballot-totals").textContent,
    /No published early\/absentee/,
  );
  assert.match(
    d.getElementById("provenance").textContent,
    /No published snapshot/,
  );
});

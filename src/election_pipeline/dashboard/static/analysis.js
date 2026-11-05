// North Carolina analysis page. Data reaches the DOM only via textContent/attributes.
const $ = (id) => document.getElementById(id);
const SVG = "http://www.w3.org/2000/svg";
const METHODS = {
  election_day: "Election Day",
  early_voting: "Early in person",
  absentee_by_mail: "Absentee by mail",
  provisional: "Provisional",
};
const METHOD_COLORS = {
  election_day: "#4f6d8f",
  early_voting: "#8fb3d9",
  absentee_by_mail: "#d9a441",
  provisional: "#b7c3d1",
};
const PARTY_COLORS = {
  DEM: "#2775c7",
  REP: "#dc5259",
  LIB: "#ecc650",
  GRE: "#3f9f6b",
  UNA: "#8f7fb5",
};
const OTHER_COLORS = ["#8a99ad", "#b48ead", "#6fa8a0", "#c9876b"];
const DIMENSION_NAMES = {
  party_registration: "Party registration",
  race: "Race",
  sex: "Sex",
  ethnicity: "Ethnicity",
};
const state = {
  data: null,
  race: null,
  requestType: "ALL",
  sort: ["total", -1],
};

const number = (n) => (n == null ? "—" : n.toLocaleString("en-US"));
const percent = (n, digits = 1) =>
  n == null || !Number.isFinite(n) ? "—" : (n * 100).toFixed(digits) + "%";
// Close races keep two decimals so a narrow lead never displays as 0.0%.
const margin = (race) =>
  race.margin_share == null
    ? "—"
    : percent(race.margin_share, race.margin_share < 0.01 ? 2 : 1) +
      " · " +
      number(race.margin);
const when = (value) =>
  value
    ? new Date(value).toLocaleString("en-US", {
        month: "short",
        day: "numeric",
        hour: "numeric",
        minute: "2-digit",
        timeZoneName: "short",
      })
    : "—";

function el(tag, attributes = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attributes)) {
    if (key === "text") node.textContent = value;
    else if (key === "style")
      for (const [name, v] of Object.entries(value))
        node.style.setProperty(name, v);
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (value !== null && value !== undefined)
      node.setAttribute(key, value);
  }
  for (const child of [].concat(children)) if (child) node.append(child);
  return node;
}

function svg(tag, attributes = {}, children = []) {
  const node = document.createElementNS(SVG, tag);
  for (const [key, value] of Object.entries(attributes)) {
    if (key === "text") node.textContent = value;
    else node.setAttribute(key, value);
  }
  for (const child of [].concat(children)) if (child) node.append(child);
  return node;
}

function partyColor(party, index = 0) {
  return PARTY_COLORS[party] || OTHER_COLORS[index % OTHER_COLORS.length];
}

function candidateColors(race) {
  const colors = {};
  race.candidates.forEach((c, i) => (colors[c.name] = partyColor(c.party, i)));
  return colors;
}

function chip(party, index) {
  return el("span", {
    class: "chip",
    text: party || "—",
    style: { background: partyColor(party, index) },
  });
}

function stacked(parts) {
  // parts: [{value, color, label}] -> 100% bar; empty when total is zero.
  const total = parts.reduce((sum, p) => sum + p.value, 0);
  return el(
    "div",
    { class: "track", role: "img" },
    total
      ? parts.map((p) =>
          el("i", {
            title: `${p.label}: ${number(p.value)} (${percent(p.value / total)})`,
            style: {
              width: (100 * p.value) / total + "%",
              background: p.color,
            },
          }),
        )
      : [],
  );
}

function legend(items) {
  return el(
    "div",
    { class: "legend" },
    items.map(([label, color]) =>
      el("span", { text: label, style: { "--swatch": color } }),
    ),
  );
}

function selectedRace() {
  return state.data.results.races.find((r) => r.id === state.race);
}

function renderRaces() {
  const table = $("races");
  table.replaceChildren(
    el("thead", {}, [
      el("tr", {}, [
        el("th", { text: "Race" }),
        el("th", { text: "Votes" }),
        el("th", { text: "Leader" }),
        el("th", { text: "Margin" }),
        ...Object.values(METHODS).map((name) => el("th", { text: name })),
      ]),
    ]),
    el(
      "tbody",
      {},
      state.data.results.races.map((race) => {
        const lead = race.candidates[0];
        const total = race.totals.total_votes;
        return el(
          "tr",
          {
            class: "selectable",
            tabindex: "0",
            "aria-selected": String(race.id === state.race),
            onclick: () => select(race.id),
            onkeydown: (e) => e.key === "Enter" && select(race.id),
          },
          [
            el("td", { text: race.name }),
            el("td", { text: number(total) }),
            el("td", {}, total ? [chip(lead.party), " ", lead.name] : ["—"]),
            el("td", {
              text: margin(race),
              title:
                race.margin == null ? null : number(race.margin) + " votes",
            }),
            ...Object.keys(METHODS).map((m) =>
              el("td", {
                text: percent(total ? race.totals[m] / total : null),
              }),
            ),
          ],
        );
      }),
    ),
  );
}

function renderCandidates(race) {
  $("race-title").textContent = race.name;
  $("candidates").replaceChildren(
    ...race.candidates.map((c, i) =>
      el("div", { class: "row" }, [
        el("div", { class: "name" }, [
          el("b", { text: c.name }),
          el("span", { text: c.party || "No party" }),
        ]),
        stacked([
          {
            value: c.votes.total_votes,
            color: partyColor(c.party, i),
            label: c.name,
          },
          {
            value: race.totals.total_votes - c.votes.total_votes,
            color: "transparent",
            label: "Others",
          },
        ]),
        el("div", { class: "figure" }, [
          el("b", { text: percent(c.share) }),
          el("small", { text: number(c.votes.total_votes) + " votes" }),
        ]),
      ]),
    ),
  );
}

function renderMethods(race) {
  const colors = candidateColors(race);
  $("by-method").replaceChildren(
    legend(race.candidates.map((c) => [c.name, colors[c.name]])),
    el(
      "div",
      { class: "bars" },
      Object.entries(METHODS).map(([method, name]) =>
        el("div", { class: "row" }, [
          el("div", { class: "label" }, [el("b", { text: name })]),
          stacked(
            race.candidates.map((c) => ({
              value: c.votes[method],
              color: colors[c.name],
              label: c.name,
            })),
          ),
          el("div", { class: "figure" }, [
            el("b", { text: number(race.totals[method]) }),
            el("small", {
              text: race.totals.total_votes
                ? percent(race.totals[method] / race.totals.total_votes) +
                  " of votes"
                : "—",
            }),
          ]),
        ]),
      ),
    ),
  );
  $("by-candidate").replaceChildren(
    legend(
      Object.entries(METHODS).map(([m, name]) => [name, METHOD_COLORS[m]]),
    ),
    el(
      "div",
      { class: "bars" },
      race.candidates.map((c) =>
        el("div", { class: "row" }, [
          el("div", { class: "label" }, [
            el("b", { text: c.name }),
            el("span", { text: " " + (c.party || "") }),
          ]),
          stacked(
            Object.entries(METHODS).map(([m, name]) => ({
              value: c.votes[m],
              color: METHOD_COLORS[m],
              label: name,
            })),
          ),
          el("div", { class: "figure" }, [
            el("b", {
              text: c.votes.total_votes
                ? percent(c.votes.early_voting / c.votes.total_votes)
                : "—",
            }),
            el("small", { text: "early in person" }),
          ]),
        ]),
      ),
    ),
  );
}

function renderHistory(race) {
  const history = state.data.history;
  const points = (history?.points || []).filter((p) => p.races[race.id]);
  const target = $("history");
  $("history-hint").textContent =
    "One point per published snapshot. A snapshot is published only when the official file " +
    "changes and passes validation." +
    (history?.truncated
      ? " Only the most recent 500 snapshots are shown."
      : "");
  if (points.length < 2) {
    target.replaceChildren(
      el("p", {
        class: "hint",
        text: points.length
          ? "Only one published snapshot so far; the history chart appears after the next one."
          : "No published snapshots yet.",
      }),
    );
    return;
  }
  const colors = candidateColors(race);
  const width = 960,
    height = 280,
    left = 70,
    right = 150,
    top = 14,
    bottom = 32;
  const times = points.map((p) => new Date(p.published_at).getTime());
  const t0 = Math.min(...times),
    t1 = Math.max(...times);
  const max = Math.max(
    1,
    ...points.flatMap((p) => Object.values(p.races[race.id].candidates)),
  );
  const x = (t) =>
    left + ((t - t0) / Math.max(1, t1 - t0)) * (width - left - right);
  const y = (v) => top + (1 - v / max) * (height - top - bottom);
  const children = [];
  for (const fraction of [0, 0.25, 0.5, 0.75, 1]) {
    const value = max * fraction;
    children.push(
      svg("line", {
        x1: left,
        x2: width - right,
        y1: y(value),
        y2: y(value),
        stroke: "#e1e7ee",
      }),
      svg("text", {
        x: left - 8,
        y: y(value) + 4,
        "text-anchor": "end",
        text: number(Math.round(value)),
      }),
    );
  }
  children.push(
    svg("text", { x: left, y: height - 8, text: when(points[0].published_at) }),
    svg("text", {
      x: width - right,
      y: height - 8,
      "text-anchor": "end",
      text: when(points.at(-1).published_at),
    }),
  );
  // End labels are spread apart so close candidates stay readable.
  const labels = race.candidates
    .map((c) => ({
      c,
      y: y(points.at(-1).races[race.id].candidates[c.name] ?? 0),
    }))
    .sort((a, b) => a.y - b.y);
  labels.forEach((label, i) => {
    label.y = Math.max(label.y, top + 8, i ? labels[i - 1].y + 14 : -Infinity);
  });
  for (let i = labels.length - 1; i >= 0; i--) {
    const limit =
      i === labels.length - 1 ? height - bottom : labels[i + 1].y - 14;
    labels[i].y = Math.min(labels[i].y, limit);
  }
  for (const c of race.candidates) {
    const coords = points.map((p, i) => [
      x(times[i]),
      y(p.races[race.id].candidates[c.name] ?? 0),
    ]);
    children.push(
      svg("polyline", {
        points: coords.map((xy) => xy.join(",")).join(" "),
        fill: "none",
        stroke: colors[c.name],
        "stroke-width": 2.5,
      }),
      ...coords.map(([cx, cy], i) =>
        svg("circle", { cx, cy, r: 3, fill: colors[c.name] }, [
          svg("title", {
            text: `${c.name} · ${when(points[i].published_at)} · ${number(points[i].races[race.id].candidates[c.name] ?? 0)}`,
          }),
        ]),
      ),
      svg("text", {
        x: width - right + 8,
        y: labels.find((l) => l.c === c).y + 4,
        text: c.name,
        style: `fill:${colors[c.name]}`,
      }),
    );
  }
  const changes = points
    .slice(-8)
    .reverse()
    .map((p) => {
      const index = points.indexOf(p);
      const previous =
        index > 0 ? points[index - 1].races[race.id].total : null;
      const total = p.races[race.id].total;
      return el("tr", {}, [
        el("td", { text: when(p.published_at) }),
        el("td", { text: when(p.source_modified_at) }),
        el("td", { text: number(total) }),
        el("td", {
          text:
            previous == null
              ? "—"
              : (total - previous >= 0 ? "+" : "") + number(total - previous),
        }),
        el("td", {
          text: percent(
            race.candidates[0] && total
              ? (p.races[race.id].candidates[race.candidates[0].name] ?? 0) /
                  total
              : null,
          ),
        }),
      ]);
    });
  target.replaceChildren(
    svg(
      "svg",
      {
        class: "history",
        viewBox: `0 0 ${width} ${height}`,
        role: "img",
        "aria-label": `Vote totals over time for ${race.name}`,
      },
      children,
    ),
    el("div", { class: "table-wrap" }, [
      el("table", { class: "data" }, [
        el("thead", {}, [
          el("tr", {}, [
            el("th", { text: "Published" }),
            el("th", { text: "Official file modified" }),
            el("th", { text: "Race votes" }),
            el("th", { text: "Change" }),
            el("th", { text: `${race.candidates[0]?.name || "Leader"} share` }),
          ]),
        ]),
        el("tbody", {}, changes),
      ]),
    ]),
  );
}

function countyRows(race) {
  return race.counties.map((county) => {
    const choices = Object.entries(county.votes);
    const sum = (m) => choices.reduce((s, [, v]) => s + v[m], 0);
    const leader = (m) => {
      const ranked = choices
        .filter(([, v]) => v[m] > 0)
        .sort((a, b) => b[1][m] - a[1][m]);
      if (!ranked.length) return null;
      if (ranked[1] && ranked[1][1][m] === ranked[0][1][m]) return "Tie";
      return ranked[0][0];
    };
    const total = sum("total_votes");
    const leaders = [
      "total_votes",
      "election_day",
      "early_voting",
      "absentee_by_mail",
    ].map(leader);
    return {
      name: county.name,
      total,
      election_day: total ? sum("election_day") / total : null,
      early_voting: total ? sum("early_voting") / total : null,
      absentee_by_mail: total ? sum("absentee_by_mail") / total : null,
      leaders,
      split: new Set(leaders.filter(Boolean)).size > 1,
    };
  });
}

function renderCounties(race) {
  const colors = candidateColors(race);
  const columns = [
    ["name", "County"],
    ["total", "Votes"],
    ["election_day", "Election Day"],
    ["early_voting", "Early"],
    ["absentee_by_mail", "Mail"],
    ["leader", "Leader"],
    ["leader_election_day", "Election Day leader"],
    ["leader_early_voting", "Early leader"],
    ["leader_absentee_by_mail", "Mail leader"],
  ];
  const rows = countyRows(race);
  const [key, direction] = state.sort;
  const value = (row) =>
    key.startsWith("leader")
      ? row.leaders[
          [
            "leader",
            "leader_election_day",
            "leader_early_voting",
            "leader_absentee_by_mail",
          ].indexOf(key)
        ] || ""
      : row[key];
  rows.sort((a, b) => {
    const [p, q] = [value(a), value(b)];
    return (
      (typeof p === "string" ? p.localeCompare(q) : (p ?? -1) - (q ?? -1)) *
      direction
    );
  });
  const leaderCell = (name) =>
    el(
      "td",
      {},
      name
        ? [
            el("span", {
              class: "chip",
              text: name === "Tie" ? "Tie" : name.split(" ").at(-1),
              title: name,
              style: { background: colors[name] || "#8a99ad" },
            }),
          ]
        : ["—"],
    );
  $("counties").replaceChildren(
    el("thead", {}, [
      el(
        "tr",
        {},
        columns.map(([id, label]) =>
          el(
            "th",
            {
              "aria-sort":
                id === key
                  ? direction > 0
                    ? "ascending"
                    : "descending"
                  : null,
            },
            [
              el("button", {
                text: label + (id === key ? (direction > 0 ? " ▲" : " ▼") : ""),
                onclick: () => {
                  state.sort = [
                    id,
                    id === key ? -direction : id === "name" ? 1 : -1,
                  ];
                  renderCounties(race);
                },
              }),
            ],
          ),
        ),
      ),
    ]),
    el(
      "tbody",
      {},
      rows.map((row) =>
        el("tr", {}, [
          el("td", { text: (row.split ? "◆ " : "") + row.name }),
          el("td", { text: number(row.total) }),
          el("td", { text: percent(row.election_day) }),
          el("td", { text: percent(row.early_voting) }),
          el("td", { text: percent(row.absentee_by_mail) }),
          ...row.leaders.map(leaderCell),
        ]),
      ),
    ),
  );
}

function renderBallots() {
  const demo = state.data.demographics;
  const select = $("request-type");
  if (!demo) {
    $("ballot-totals").replaceChildren(
      el("p", {
        class: "hint",
        text: "No published early/absentee ballot snapshot.",
      }),
    );
    $("dimensions").replaceChildren();
    $("ballot-counties").replaceChildren();
    select.replaceChildren();
    return;
  }
  const types = Object.keys(demo.request_types);
  if (!select.options.length) {
    select.append(
      el("option", { value: "ALL", text: "All request types" }),
      ...types.map((t) => el("option", { value: t, text: t })),
    );
    select.value = state.requestType;
  }
  const chosen = state.requestType === "ALL" ? types : [state.requestType];
  const total = chosen.reduce((s, t) => s + demo.request_types[t], 0);
  $("ballot-totals").replaceChildren(
    el("div", { class: "card" }, [
      el("b", { text: number(demo.total) }),
      el("span", { text: "All early and absentee ballots" }),
    ]),
    ...types.map((t) =>
      el(
        "div",
        { class: "card", "aria-current": String(state.requestType === t) },
        [
          el("b", { text: number(demo.request_types[t]) }),
          el("span", {
            text: `${t} · ${percent(demo.total ? demo.request_types[t] / demo.total : null)}`,
          }),
        ],
      ),
    ),
  );
  $("dimensions").replaceChildren(
    ...Object.entries(DIMENSION_NAMES).map(([dimension, name]) => {
      const values = {};
      for (const t of chosen)
        for (const [k, v] of Object.entries(
          demo.dimensions[dimension][t] || {},
        ))
          values[k] = (values[k] || 0) + v;
      const ordered = Object.entries(values)
        .filter(([, v]) => v > 0)
        .sort((a, b) => b[1] - a[1]);
      return el("div", { class: "dimension" }, [
        el("h3", { text: name }),
        el(
          "div",
          { class: "bars" },
          ordered.length
            ? ordered.map(([label, v], i) =>
                el("div", { class: "row" }, [
                  el("div", { class: "label" }, [el("b", { text: label })]),
                  stacked([
                    {
                      value: v,
                      color:
                        dimension === "party_registration"
                          ? partyColor(label, i)
                          : "#4f6d8f",
                      label,
                    },
                    { value: total - v, color: "transparent", label: "Others" },
                  ]),
                  el("div", { class: "figure" }, [
                    el("b", { text: percent(total ? v / total : null) }),
                    el("small", { text: number(v) }),
                  ]),
                ]),
              )
            : [el("p", { class: "hint", text: "No ballots of this type." })],
        ),
      ]);
    }),
  );
  const counties = demo.counties
    .map((c) => ({
      name: c.name,
      ballots: chosen.reduce((s, t) => s + (c.ballots[t] || 0), 0),
    }))
    .sort((a, b) => b.ballots - a.ballots)
    .slice(0, 15);
  $("ballot-counties").replaceChildren(
    el("thead", {}, [
      el("tr", {}, [
        el("th", { text: "County" }),
        el("th", { text: "Ballots" }),
        el("th", { text: "Share of state" }),
      ]),
    ]),
    el(
      "tbody",
      {},
      counties.length
        ? counties.map((c) =>
            el("tr", {}, [
              el("td", { text: c.name }),
              el("td", { text: number(c.ballots) }),
              el("td", { text: percent(total ? c.ballots / total : null) }),
            ]),
          )
        : [
            el("tr", {}, [
              el("td", {
                text: "County detail is not available in this snapshot.",
                colspan: "3",
              }),
            ]),
          ],
    ),
  );
}

function renderSources() {
  const { results, demographics, notes, mode } = state.data;
  const rows = [];
  const add = (label, value) =>
    rows.push(el("dt", { text: label }), el("dd", {}, [value]));
  const link = (url) =>
    url ? el("a", { href: url, text: url, rel: "noopener" }) : "—";
  for (const [label, pub] of [
    ["Results", results?.publication],
    ["Early/absentee ballots", demographics?.publication],
  ]) {
    if (!pub) {
      add(label, "No published snapshot");
      continue;
    }
    add(`${label} publication`, `${pub.publication_id} (${pub.status})`);
    add(`${label} published`, when(pub.published_at));
    add(`${label} official file modified`, when(pub.source_modified_at));
    add(`${label} file`, link(pub.source_url));
    add(`${label} SHA-256`, pub.raw_sha256 || "—");
  }
  $("provenance").replaceChildren(...rows);
  $("notes").replaceChildren(
    ...(mode === "demo"
      ? ["SYNTHETIC DEMO DATA. These are not election results."]
      : []
    )
      .concat(notes)
      .map((n) => el("li", { text: n })),
  );
}

function select(race) {
  state.race = race;
  history.replaceState(
    null,
    "",
    `#race=${encodeURIComponent(race)}&type=${encodeURIComponent(state.requestType)}`,
  );
  $("race").value = race;
  const current = selectedRace();
  renderRaces();
  renderCandidates(current);
  renderMethods(current);
  renderHistory(current);
  renderCounties(current);
}

function render() {
  const { data } = state;
  $("mode").textContent =
    data.mode === "demo"
      ? "DEMO DATA"
      : (data.results?.publication.status || "awaiting").toUpperCase();
  $("published").textContent = data.results
    ? "Published " + when(data.results.publication.published_at)
    : "No published results yet";
  const notice = $("notice");
  notice.classList.toggle("alert", data.mode === "demo");
  notice.textContent =
    data.mode === "demo"
      ? "Synthetic demo data for layout only. These are not election results."
      : data.results
        ? "Unofficial returns from published snapshots. A lead is not a race call."
        : "No North Carolina results have been published yet.";
  renderSources();
  renderBallots();
  if (!data.results) return;
  const params = new URLSearchParams(location.hash.slice(1));
  const races = data.results.races.map((r) => r.id);
  $("race").replaceChildren(
    ...data.results.races.map((r) =>
      el("option", { value: r.id, text: r.name }),
    ),
  );
  select(
    races.includes(params.get("race"))
      ? params.get("race")
      : state.race && races.includes(state.race)
        ? state.race
        : races[0],
  );
}

async function load() {
  try {
    const response = await fetch("/api/analysis/nc", { cache: "no-cache" });
    if (!response.ok) throw new Error(String(response.status));
    state.data = await response.json();
    $("connection").textContent =
      "Updated " +
      new Date().toLocaleTimeString("en-US", {
        hour: "numeric",
        minute: "2-digit",
      });
    render();
  } catch {
    $("connection").textContent = "Offline";
    $("notice").textContent =
      "Published analysis is unavailable. Showing the last loaded data, if any.";
    $("notice").classList.add("alert");
  }
}

const params = new URLSearchParams(location.hash.slice(1));
state.requestType = params.get("type") || "ALL";
$("race").addEventListener("change", (e) => select(e.target.value));
$("request-type").addEventListener("change", (e) => {
  state.requestType = e.target.value;
  if (state.race) select(state.race);
  renderBallots();
});
load();
setInterval(load, 60_000);

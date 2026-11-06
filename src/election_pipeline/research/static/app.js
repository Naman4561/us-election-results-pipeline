"use strict";

const byId = id => document.getElementById(id);
const format = new Intl.NumberFormat(undefined, { maximumSignificantDigits: 10 });
const state = {
  versions: [], groups: new Map(), dataset: null, version: null, filters: [],
  offset: 0, versionRequest: 0, queryRequest: 0
};
const operations = {
  eq: "Equals", ne: "Does not equal", contains: "Contains", gte: "At least",
  lte: "At most", missing: "Is missing", present: "Is present"
};

function element(tag, content, className) {
  const node = document.createElement(tag);
  if (content !== undefined) node.textContent = content;
  if (className) node.className = className;
  return node;
}

function label(value) {
  const words = value.replaceAll("_", " ");
  return words[0].toUpperCase() + words.slice(1);
}

function options(select, first, entries) {
  const previous = select.value;
  select.replaceChildren();
  if (first !== null) select.add(new Option(first, ""));
  for (const [value, title] of entries) select.add(new Option(title, value));
  if ([...select.options].some(option => option.value === previous)) select.value = previous;
}

function errorMessage(message = "") {
  byId("error").textContent = message;
  byId("error").hidden = !message;
}

async function api(path, payload) {
  const response = await fetch(path, payload === undefined ? { cache: "no-store" } : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload)
  });
  const value = await response.json();
  if (!response.ok) {
    throw new Error(typeof value.detail === "string" ? value.detail : `Request failed (${response.status})`);
  }
  return value;
}

function renderCatalog() {
  const search = byId("search").value.trim().toLowerCase();
  const year = byId("year").value;
  const fields = ["kind", "status", "jurisdiction"];
  state.groups = new Map();
  for (const version of state.versions) {
    if (fields.some(field => byId(field).value && byId(field).value !== version[field])) continue;
    if (year && !version.period_start.startsWith(year + "-")) continue;
    const index = [version.title, version.dataset_id, version.publisher, version.grain,
      version.period_start, version.period_end, version.election_date, version.geography,
      ...version.tags].join(" ").toLowerCase();
    if (search && !index.includes(search)) continue;
    if (!state.groups.has(version.dataset_id)) state.groups.set(version.dataset_id, []);
    state.groups.get(version.dataset_id).push(version);
  }

  const datasets = state.groups.size;
  const versions = state.versions.length;
  byId("catalog-count").textContent =
    `${datasets} dataset${datasets === 1 ? "" : "s"} · ${versions} stored version${versions === 1 ? "" : "s"}`;
  byId("dataset-list").replaceChildren();
  for (const [dataset, versions] of state.groups) {
    const newest = versions[0];
    const button = element("button", undefined, "dataset-button");
    button.type = "button";
    button.dataset.datasetId = dataset;
    button.setAttribute("aria-pressed", String(dataset === state.dataset));
    button.append(element("span", newest.title));
    button.append(element("span",
      `${newest.jurisdiction} · ${label(newest.kind)} · ${versions.length} version${versions.length === 1 ? "" : "s"}`,
      "muted"
    ));
    button.addEventListener("click", () => selectDataset(dataset));
    const row = element("li");
    row.append(button);
    byId("dataset-list").append(row);
  }

  if (state.dataset !== null) {
    if (state.groups.has(state.dataset)) selectDataset(state.dataset);
    else clearSelection();
  }
}

async function refresh() {
  byId("refresh").disabled = true;
  try {
    const data = await api("/api/catalog");
    state.versions = data.versions;
    options(byId("kind"), "All types", data.kinds.map(value => [value, label(value)]));
    options(byId("status"), "All statuses", data.statuses.map(value => [value, label(value)]));
    const locations = [...new Set(state.versions.map(version => version.jurisdiction))].sort();
    options(byId("jurisdiction"), "All areas", locations.map(value => [value, value]));
    const years = [...new Set(state.versions.map(version => version.period_start.slice(0, 4)))];
    options(byId("year"), "All years", years.sort().reverse().map(value => [value, value]));
    errorMessage();
    renderCatalog();
  } catch (error) {
    byId("catalog-count").textContent = "Catalog unavailable";
    errorMessage(error.message);
  } finally {
    byId("refresh").disabled = false;
  }
}

function clearSelection() {
  ++state.versionRequest;
  ++state.queryRequest;
  state.dataset = null;
  state.version = null;
  byId("version").replaceChildren();
  byId("selected").hidden = true;
  byId("empty").hidden = false;
}

function selectDataset(dataset) {
  const versions = state.groups.get(dataset);
  const sameDataset = state.dataset === dataset;
  state.dataset = dataset;
  for (const button of byId("dataset-list").querySelectorAll("button")) {
    button.setAttribute("aria-pressed", String(button.dataset.datasetId === dataset));
  }
  options(byId("version"), null, versions.map(version => [
    String(version.id),
    `#${version.id} · ${label(version.status)} · ${version.period_start} · ${version.source_as_of || version.imported_at}`
  ]));
  if (!sameDataset) byId("version").value = String(versions[0].id);
  const versionId = Number(byId("version").value);
  if (state.version?.id !== versionId) loadVersion(versionId);
}

function detail(name, value) {
  if (value === undefined || value === null || value === "") return;
  const dd = element("dd");
  if (typeof value === "string") dd.textContent = value;
  else dd.append(value);
  byId("metadata").append(element("dt", name), dd);
}

function renderVersion(version) {
  byId("title").textContent = version.title;
  byId("grain").textContent = version.grain;
  byId("badges").replaceChildren(
    element("span", label(version.kind), "badge"),
    element("span", label(version.status), "badge status"),
    element("span", `${version.jurisdiction} · ${label(version.geography)}`, "badge")
  );
  byId("provenance").open = !version.columns.length;
  byId("metadata").replaceChildren();
  detail("Dataset ID", version.dataset_id);
  detail("Publisher", version.publisher);
  const source = element("a", "Open source");
  source.href = version.source_url;
  source.target = "_blank";
  source.rel = "noopener noreferrer";
  detail("Source", source);
  detail("Data period", version.period_end
    ? `${version.period_start} through ${version.period_end}` : version.period_start);
  detail("Election date", version.election_date);
  detail("Source timestamp", version.source_as_of);
  detail("Imported", version.imported_at);
  detail("Upstream SHA-256", version.parent_sha256);
  detail("Tags", version.tags.join(", "));
  byId("notes").textContent = version.notes || "";
  byId("files").replaceChildren();
  for (const file of version.files) {
    const link = element("a", file.filename);
    link.href = `/api/versions/${version.id}/files/${file.position}`;
    link.download = file.filename;
    const row = element("li");
    row.append(link, element("span", ` · SHA-256 ${file.sha256}`, "muted"));
    byId("files").append(row);
  }
}

async function loadVersion(id) {
  const request = ++state.versionRequest;
  ++state.queryRequest;
  state.version = null;
  errorMessage();
  byId("explorer").hidden = true;
  byId("archive-only").hidden = true;
  try {
    const version = await api(`/api/versions/${id}`);
    if (request !== state.versionRequest) return;
    state.version = version;
    state.filters = [];
    state.offset = 0;
    byId("filter-list").replaceChildren();
    updateFilterActions();
    renderVersion(version);
    byId("selected").hidden = false;
    byId("empty").hidden = true;
    byId("explorer").hidden = !version.columns.length;
    byId("archive-only").hidden = Boolean(version.columns.length);
    if (version.columns.length) {
      const columns = version.columns.map(column => [column.name, column.name]);
      options(byId("sort"), "Original row order", columns);
      options(byId("statistic"), "Choose a column", columns);
      byId("sort").value = "";
      byId("direction").value = "asc";
      byId("statistic").value = "";
      byId("rows").replaceChildren();
      byId("headers").replaceChildren();
      byId("metrics").replaceChildren();
      byId("frequencies").hidden = true;
      await runQuery(true);
    }
  } catch (error) {
    if (request === state.versionRequest) errorMessage(error.message);
  }
}

function field(title, control) {
  const wrapper = element("label", title);
  wrapper.append(control);
  return wrapper;
}

function addFilter() {
  const row = element("div", undefined, "filter-row");
  const column = element("select");
  const operation = element("select");
  const value = element("input");
  value.placeholder = "Value";
  const remove = element("button", "Remove");
  remove.type = "button";
  options(column, null, state.version.columns.map(item => [item.name, item.name]));
  function updateOperation() {
    const kind = state.version.columns.find(item => item.name === column.value).type;
    options(operation, null, Object.entries(operations).filter(
      ([key]) => key !== "contains" || kind === "text"
    ));
    updateValue();
  }
  function updateValue() {
    value.disabled = ["missing", "present"].includes(operation.value);
    value.required = !value.disabled;
  }
  column.addEventListener("change", updateOperation);
  operation.addEventListener("change", updateValue);
  remove.addEventListener("click", () => { row.remove(); updateFilterActions(); });
  row.append(field("Column", column), field("Operator", operation), field("Value", value), remove);
  byId("filter-list").append(row);
  updateOperation();
  updateFilterActions();
  column.focus();
}

function updateFilterActions() {
  const count = byId("filter-list").childElementCount;
  byId("filter-actions").hidden = !count && !state.filters.length;
  byId("add-filter").disabled = count >= 12;
}

function sortBy(name) {
  const current = byId("sort").value;
  byId("direction").value = current === name && byId("direction").value === "asc" ? "desc" : "asc";
  byId("sort").value = name;
  runQuery(true);
}

function renderRows(rows) {
  const selected = byId("sort").value;
  const direction = byId("direction").value;
  const header = element("tr");
  for (const column of state.version.columns) {
    const cell = element("th");
    cell.scope = "col";
    const sorted = column.name === selected;
    cell.setAttribute("aria-sort", sorted ? (direction === "asc" ? "ascending" : "descending") : "none");
    const marker = sorted ? (direction === "asc" ? " ↑" : " ↓") : "";
    const button = element("button", column.name + marker);
    button.type = "button";
    button.addEventListener("click", () => sortBy(column.name));
    cell.append(button, element("small", column.type));
    header.append(cell);
  }
  byId("headers").replaceChildren(header);
  const body = document.createDocumentFragment();
  for (const values of rows) {
    const row = element("tr");
    values.forEach(value => row.append(element(
      "td", value === null ? "—" : String(value),
      value === null ? "missing" : typeof value === "number" ? "number" : undefined
    )));
    body.append(row);
  }
  if (!rows.length) {
    const row = element("tr");
    const cell = element("td", "No matching rows.", "muted");
    cell.colSpan = state.version.columns.length;
    row.append(cell);
    body.append(row);
  }
  byId("rows").replaceChildren(body);
}

function renderStatistics(count, stats) {
  const metrics = byId("metrics");
  const frequencies = byId("frequencies");
  metrics.replaceChildren();
  frequencies.replaceChildren();
  function metric(title, value) {
    const card = element("div", undefined, "metric");
    const number = element("strong", value === null ? "—" : format.format(value));
    if (value !== null) number.title = String(value);
    card.append(element("span", title), number);
    metrics.append(card);
  }
  metric("Matching rows", count);
  frequencies.hidden = !stats?.most_common?.length;
  if (!stats) return;
  const titles = { present: "Nonmissing", missing: "Missing", distinct_values: "Distinct values",
    sum: "Sum", mean: "Mean", min: "Minimum", max: "Maximum" };
  for (const [key, title] of Object.entries(titles)) {
    if (Object.hasOwn(stats, key)) metric(title, stats[key]);
  }
  for (const entry of stats.most_common || []) {
    frequencies.append(element("li", `${entry.value} · ${format.format(entry.count)}`));
  }
}

async function runQuery(reset = false) {
  if (!state.version?.columns.length) return;
  if (reset) state.offset = 0;
  const request = ++state.queryRequest;
  const limit = Number(byId("page-size").value);
  byId("row-summary").textContent = "Loading rows…";
  byId("prev").disabled = true;
  byId("next").disabled = true;
  errorMessage();

  try {
    const result = await api(`/api/versions/${state.version.id}/query`, {
      filters: state.filters, sort: byId("sort").value || null, direction: byId("direction").value,
      statistic: byId("statistic").value || null, offset: state.offset, limit
    });
    if (request !== state.queryRequest) return;
    const matching = result.matching_rows;
    renderRows(result.rows);
    renderStatistics(matching, result.statistics);
    const range = result.rows.length ? `${state.offset + 1}–${state.offset + result.rows.length}` : "0";
    byId("row-summary").textContent = `${range} of ${format.format(matching)} matching rows`;
    byId("page-number").textContent = `Page ${Math.floor(state.offset / limit) + 1}`;
    byId("prev").disabled = state.offset === 0;
    byId("next").disabled = state.offset + limit >= matching;
  } catch (error) {
    if (request !== state.queryRequest) return;
    byId("rows").replaceChildren();
    byId("row-summary").textContent = "Query failed";
    byId("metrics").replaceChildren();
    byId("frequencies").hidden = true;
    errorMessage(error.message);
  }
}

byId("refresh").addEventListener("click", refresh);
byId("search").addEventListener("input", renderCatalog);
for (const fieldId of ["kind", "status", "jurisdiction", "year"]) {
  byId(fieldId).addEventListener("change", renderCatalog);
}
byId("version").addEventListener("change", () => loadVersion(Number(byId("version").value)));
byId("add-filter").addEventListener("click", addFilter);
byId("filters").addEventListener("submit", event => {
  event.preventDefault();
  state.filters = [...byId("filter-list").children].map(row => {
    const [column, operator, value] = row.querySelectorAll("select, input");
    return { column: column.value, operator: operator.value, value: value.disabled ? "" : value.value };
  });
  updateFilterActions();
  runQuery(true);
});
byId("clear-filters").addEventListener("click", () => {
  byId("filter-list").replaceChildren();
  state.filters = [];
  updateFilterActions();
  runQuery(true);
});
for (const fieldId of ["sort", "direction", "statistic", "page-size"]) {
  byId(fieldId).addEventListener("change", () => runQuery(fieldId !== "statistic"));
}
byId("prev").addEventListener("click", () => {
  state.offset -= Number(byId("page-size").value);
  runQuery();
});
byId("next").addEventListener("click", () => {
  state.offset += Number(byId("page-size").value);
  runQuery();
});

refresh();

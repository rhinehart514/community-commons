const $ = (s) => document.querySelector(s);
const el = (tag, text, cls) => {
  const n = document.createElement(tag);
  if (text !== undefined) n.textContent = text;
  if (cls) n.className = cls;
  return n;
};
const fmt = (n) => Number(n).toLocaleString();
let profileOffset = 0,
  matchOffset = 0,
  runId = "",
  searchVersion = 0,
  matchVersion = 0,
  pairVersion = 0,
  detailVersion = 0;
function notice(message, error = false) {
  const n = $("#notice");
  n.hidden = !message;
  n.textContent = message;
  n.className = error ? "error" : "";
}
async function api(path, data) {
  const response = await fetch(
    "/api/" + path,
    data === undefined
      ? {}
      : {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-BP-Commons": "workbench",
          },
          body: JSON.stringify(data),
        },
  );
  const value = await response.json();
  if (!response.ok) throw Error(value.error || "Request failed");
  return value;
}
function guard(fn) {
  return async (...args) => {
    try {
      await fn(...args);
    } catch (e) {
      notice(e.message, true);
    }
  };
}
async function busy(button, fn) {
  button.disabled = true;
  const old = button.textContent;
  button.textContent = "Working…";
  try {
    return await fn();
  } finally {
    button.disabled = false;
    button.textContent = old;
  }
}
function display(value) {
  if (typeof value === "string") {
    try {
      const parsed = JSON.parse(value);
      if (Array.isArray(parsed)) return parsed.join(" · ");
    } catch {}
    return value;
  }
  return typeof value === "object"
    ? JSON.stringify(value, null, 2)
    : String(value);
}
function fields(object, omit = []) {
  const dl = el("dl");
  for (const [key, value] of Object.entries(object || {})) {
    if (value === null || value === "" || omit.includes(key)) continue;
    const row = el("div", undefined, "field");
    row.append(el("dt", key.replaceAll("_", " ")));
    const dd = el("dd");
    if (typeof value === "string" && /^https?:\/\//i.test(value)) {
      const a = el("a", value);
      a.href = value;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      dd.append(a);
    } else dd.textContent = display(value);
    row.append(dd);
    dl.append(row);
  }
  return dl;
}
function evidence(title, record) {
  const d = el("details");
  d.append(el("summary", title), fields(record));
  return d;
}
function raw(title, data, open = false) {
  const d = el("details");
  d.open = open;
  d.append(el("summary", title), el("pre", JSON.stringify(data, null, 2)));
  return d;
}
function paginate(target, offset, total, size, callback) {
  target.replaceChildren();
  if (!total) return;
  const prev = el("button", "← Previous");
  prev.disabled = offset === 0;
  prev.onclick = guard(() => callback(Math.max(0, offset - size)));
  const next = el("button", "Next →");
  next.disabled = offset + size >= total;
  next.onclick = guard(() => callback(offset + size));
  target.append(
    prev,
    el(
      "span",
      `${fmt(offset + 1)}–${fmt(Math.min(offset + size, total))} of ${fmt(total)}`,
    ),
    next,
  );
}
function selectDetail(selector) {
  if (innerWidth <= 700)
    $(selector).scrollIntoView({ behavior: "smooth", block: "start" });
}
async function search(offset = 0) {
  profileOffset = offset;
  const version = ++searchVersion;
  const params = new URLSearchParams({
    q: $("#query").value,
    collector: $("#collector").value,
    offset,
  });
  $("#result-count").textContent = "Searching…";
  const result = await api("profiles?" + params);
  if (version !== searchVersion) return;
  $("#result-count").textContent = `${fmt(result.total)} source records`;
  const list = $("#profiles");
  list.replaceChildren();
  if (!result.total)
    list.append(
      el(
        "p",
        "No records found. Try fewer words or another collection.",
        "empty",
      ),
    );
  for (const item of result.profiles) {
    const p = item.record;
    const b = el("button", undefined, "row");
    b.append(
      el("span", p.collector || "Source record", "tag"),
      el("strong", p.name || p.full_name || item.id),
      el("p", [p.organization, p.role].filter(Boolean).join(" · ")),
    );
    b.onclick = guard(async () => {
      const token = ++detailVersion;
      const data = await api("profile/" + encodeURIComponent(item.id));
      if (token !== detailVersion) return;
      list
        .querySelectorAll(".selected")
        .forEach((n) => n.classList.remove("selected"));
      b.classList.add("selected");
      const detail = $("#profile-detail");
      detail.replaceChildren(
        el("span", "SOURCE PROFILE · UNVERIFIED IDENTITY", "tag"),
        el("h2", p.name || p.full_name || item.id),
        fields(data.profile),
      );
      detail.append(el("h3", `${data.observations.length} observations`));
      for (const [i, record] of data.observations.entries())
        detail.append(evidence(`Observation ${i + 1}`, record));
      detail.append(
        el("h3", `${data.relationships.length} recorded connections`),
      );
      for (const [i, record] of data.relationships.entries())
        detail.append(evidence(`Connection ${i + 1}`, record));
      selectDetail("#profile-detail");
    });
    list.append(b);
  }
  paginate($("#profile-pages"), offset, result.total, 30, search);
}
async function catalog(selected) {
  const data = await api("catalog");
  for (const side of ["left", "right"]) {
    const s = $("#" + side + "-dataset");
    const previous = s.value;
    s.replaceChildren();
    for (const d of data.datasets) {
      const o = el(
        "option",
        `${d.dataset_id} · ${fmt(d.records)} ${d.entity_type} records · ${d.observed_at}`,
      );
      o.value = d.digest;
      s.append(o);
    }
    if (previous) s.value = previous;
  }
  if (!runId && data.datasets[1]) $("#right-dataset").selectedIndex = 1;
  const runs = $("#runs");
  runs.replaceChildren();
  for (const r of data.runs) {
    const o = el(
      "option",
      `${r.left_dataset} → ${r.right_dataset} · ${new Date(r.created_at).toLocaleString()}`,
    );
    o.value = r.run_id;
    runs.append(o);
  }
  if (selected) runs.value = selected;
  else if (runId) runs.value = runId;
  runId = runs.value;
  $("#rerun").disabled = !runId;
  $("#download").hidden = !runId;
  return data;
}
async function loadRun(offset = 0) {
  matchOffset = offset;
  const version = ++matchVersion;
  runId = $("#runs").value;
  const list = $("#matches");
  ++pairVersion;
  list.replaceChildren(el("p", "Loading comparison…", "empty"));
  $("#match-detail").replaceChildren(
    el("p", "Select a result to inspect its evidence."),
  );
  if (!runId) {
    list.replaceChildren(
      el("p", "No comparisons yet. Select New comparison to begin.", "empty"),
    );
    return;
  }
  const result = await api(
    `runs/${runId}?` +
      new URLSearchParams({ offset, status: $("#status").value }),
  );
  if (version !== matchVersion) return;
  $("#download").href = `/api/runs/${runId}?download=1`;
  $("#run-summary").replaceChildren();
  for (const [key, value] of Object.entries(result.summary)) {
    const n = el("div");
    n.append(el("strong", fmt(value)), el("span", key.replaceAll("_", " ")));
    $("#run-summary").append(n);
  }
  list.replaceChildren();
  if (!result.total)
    list.append(el("p", "No results in this category.", "empty"));
  for (const row of result.results) {
    const b = el("button", undefined, "row");
    b.append(
      el("span", row.status.replaceAll("_", " "), "tag"),
      el("strong", row.name),
      el(
        "p",
        `${row.candidates.length} candidates · ${row.reason || "Inspect evidence"}`,
      ),
    );
    b.onclick = guard(async () => {
      list
        .querySelectorAll(".selected")
        .forEach((n) => n.classList.remove("selected"));
      b.classList.add("selected");
      ++pairVersion;
      const detail = $("#match-detail");
      detail.replaceChildren(
        el("span", row.status.replaceAll("_", " "), "tag"),
        el("h2", row.name),
        el("p", row.reason || "Review the candidates below."),
        fields({
          source: row.source_ref,
          coverage_complete: row.coverage_complete,
        }),
      );
      if (!row.candidates.length)
        detail.append(
          el(
            "p",
            "No candidate found. This result does not establish whether this person participated in an event or program.",
          ),
        );
      for (const c of row.candidates) {
        const button = el(
          "button",
          `${c.name} · ${c.status.replaceAll("_", " ")} →`,
          "candidate",
        );
        button.onclick = guard(() => inspectPair(row, c));
        detail.append(button);
      }
      selectDetail("#match-detail");
    });
    list.append(b);
  }
  paginate($("#match-pages"), offset, result.total, 30, loadRun);
}
async function inspectPair(row, candidate) {
  const version = ++pairVersion;
  const originalRun = runId;
  const data = await api(
    `runs/${originalRun}/pair?` +
      new URLSearchParams({ left: row.left_id, right: candidate.right_id }),
  );
  if (originalRun !== runId || version !== pairVersion) return;
  const panel = $("#match-detail");
  panel.replaceChildren(
    el("span", "PAIR REVIEW", "tag"),
    el("h2", row.name),
    el("p", "Inspect both source records before making a decision."),
  );
  for (const side of ["left", "right"]) {
    panel.append(
      el("h3", data[side + "_dataset"]),
      fields(data[side], ["original"]),
      raw("Original source record", data[side].original || data[side]),
    );
  }
  panel.append(
    raw("Matching evidence", candidate.evidence, true),
    raw("Dataset coverage", data.coverage),
  );
  const form = el("form", undefined, "review-form");
  form.append(el("h3", "Record a decision"));
  const reviewer = el("input");
  reviewer.required = true;
  reviewer.maxLength = 200;
  reviewer.autocomplete = "name";
  reviewer.value = localStorage.getItem("bp-reviewer") || "";
  const reason = el("textarea");
  reason.required = true;
  reason.maxLength = 5000;
  const verdict = el("select");
  for (const [v, t] of [
    ["unsure", "Unsure — keep for review"],
    ["same", "Same identity"],
    ["different", "Different identities"],
  ]) {
    const o = el("option", t);
    o.value = v;
    verdict.append(o);
  }
  for (const [title, input] of [
    ["Your name", reviewer],
    ["Decision", verdict],
    ["Evidence / reason", reason],
  ]) {
    const label = el("label", title);
    label.append(input);
    form.append(label);
  }
  const button = el("button", "Save decision", "primary");
  form.append(
    button,
    el(
      "p",
      "Decisions are retained in the audit log. Re-run the comparison to apply them.",
      "muted",
    ),
  );
  form.onsubmit = guard(async (event) => {
    event.preventDefault();
    await busy(button, () =>
      api(`runs/${originalRun}/review`, {
        left_id: row.left_id,
        right_id: candidate.right_id,
        verdict: verdict.value,
        reviewer: reviewer.value,
        reason: reason.value,
      }),
    );
    localStorage.setItem("bp-reviewer", reviewer.value);
    notice(
      "Decision saved. Use “Re-run with decisions” to create updated results; this original comparison is preserved.",
    );
    form.remove();
    panel.append(el("p", "Decision saved to the audit log.", "notice"));
  });
  panel.append(form);
}
async function history() {
  const snapshots = await api("history");
  const list = $("#snapshots");
  list.replaceChildren();
  if (!snapshots.length)
    list.append(el("p", "No snapshots have been imported yet."));
  for (const s of snapshots) {
    const d = el("details");
    d.append(
      el("summary", `Snapshot · ${new Date(s.created_at).toLocaleString()}`),
      fields({ snapshot_id: s.snapshot_id }),
      raw("Import manifest", s.metadata),
    );
    const button = el("button", "Inspect changes");
    button.onclick = guard(() => changes(s.snapshot_id, 0));
    d.append(button);
    list.append(d);
  }
}
async function changes(id, offset) {
  const rows = await api(`changes/${id}?offset=${offset}`);
  const target = $("#changes");
  target.replaceChildren(el("h2", `Snapshot changes · ${offset + 1} onward`));
  if (!rows.length) target.append(el("p", "No more changes in this snapshot."));
  for (const row of rows)
    target.append(
      raw(`${row.change_type} · ${row.kind} · ${row.record_key}`, row),
    );
  const prev = el("button", "← Previous");
  prev.disabled = offset === 0;
  prev.onclick = guard(() => changes(id, Math.max(0, offset - 50)));
  const next = el("button", "Next 50 →");
  next.disabled = rows.length < 50;
  next.onclick = guard(() => changes(id, offset + 50));
  target.append(prev, next);
}
for (const button of document.querySelectorAll("[data-view]"))
  button.onclick = guard(async () => {
    for (const v of document.querySelectorAll(".view"))
      v.hidden = v.id !== button.dataset.view;
    for (const b of document.querySelectorAll("[data-view]"))
      b.classList.toggle("active", b === button);
    $("#section-label").textContent = button.textContent.toUpperCase();
    notice("");
    if (button.dataset.view === "compare") {
      await catalog();
      await loadRun();
    }
    if (button.dataset.view === "history") await history();
    if (button.dataset.view === "enrichment") await loadClaims();
  });
$("#search-form").onsubmit = guard(async (e) => {
  e.preventDefault();
  await busy(e.submitter, () => search());
});
$("#collector").onchange = guard(() => search());
$("#runs").onchange = guard(async () => {
  $("#match-detail").replaceChildren(
    el("p", "Select a result to inspect its evidence."),
  );
  await loadRun();
});
$("#status").onchange = guard(() => loadRun());
$("#compare-form").onsubmit = guard(async (e) => {
  e.preventDefault();
  await busy(e.submitter, async () => {
    const data = {};
    for (const side of ["left", "right"]) {
      const file = $("#" + side + "-file").files[0];
      if (file && file.size > 25 * 1024 * 1024)
        throw Error("Each uploaded dataset must be smaller than 25 MB.");
      data[side] = file
        ? JSON.parse(await file.text())
        : $("#" + side + "-dataset").value;
    }
    const result = await api("compare", data);
    await catalog(result.run_id);
    $("#status").value = "";
    await loadRun();
    notice("Comparison saved. Select a result to inspect its candidates.");
  });
});
$("#rerun").onclick = guard(async (e) => {
  await busy(e.currentTarget, async () => {
    const result = await api(`runs/${runId}/rerun`, {});
    await catalog(result.run_id);
    $("#match-detail").replaceChildren(
      el("p", "Updated comparison created. Select a result to inspect it."),
    );
    await loadRun();
    notice(
      "New comparison saved with the current review decisions. Previous results remain in saved comparisons.",
    );
  });
});
guard(async () => {
  const stats = await api("stats");
  for (const [key, label] of [
    ["profiles", "Source profiles"],
    ["observations", "Observations"],
    ["relationships", "Connections"],
  ]) {
    const d = el("div", undefined, "stat");
    d.append(el("strong", fmt(stats[key])), el("span", label));
    $("#stats").append(d);
  }
  for (const c of stats.collectors) {
    const o = el("option", `${c.collector} (${fmt(c.count)})`);
    o.value = c.collector;
    $("#collector").append(o);
  }
  await search();
})();

for (const side of ["left", "right"])
  $("#" + side + "-file").onchange = () => {
    $("#" + side + "-dataset").required = !$("#" + side + "-file").files.length;
  };

async function loadClaims(offset = 0) {
  const result = await api(
    "enrichment?" + new URLSearchParams({ q: $("#claim-query").value, offset }),
  );
  $("#enrichment-stats").replaceChildren();
  for (const key of ["responses", "claims", "due"]) {
    const card = el("div");
    card.append(el("strong", fmt(result.stats[key])), el("span", key));
    $("#enrichment-stats").append(card);
  }
  $("#worker-status").textContent = JSON.stringify(result.stats, null, 2);
  const list = $("#claim-results");
  list.replaceChildren();
  if (!result.total)
    list.append(
      el(
        "p",
        "No enriched evidence found. The worker collects claims from queued public sources.",
        "empty",
      ),
    );
  for (const claim of result.claims) {
    const item = evidence(
      claim.subject + " · " + claim.predicate.replaceAll("_", " "),
      {
        value: claim.value,
        source: claim.url,
        observed: new Date(claim.observed * 1000).toISOString(),
        sha256: claim.sha256,
      },
    );
    const download = el("a", "Download preserved source response ↗", "button");
    download.href = "/api/evidence/" + claim.response_id;
    item.append(download);
    list.append(item);
  }
  paginate($("#claim-pages"), offset, result.total, 50, loadClaims);
}
$("#claim-search").onsubmit = guard(async (event) => {
  event.preventDefault();
  await busy(event.submitter, () => loadClaims());
});
$("#transition-search").onsubmit = guard(async (event) => {
  event.preventDefault();
  await busy(event.submitter, async () => {
    const params = new URLSearchParams();
    for (const id of $("#origin-institutions")
      .value.split(",")
      .map((x) => x.trim())
      .filter(Boolean))
      params.append("institution", id);
    const rows = await api("transitions?" + params);
    const target = $("#transition-results");
    target.replaceChildren(
      el(
        "p",
        rows.length +
          " candidates. No result does not establish that someone stayed.",
      ),
    );
    for (const row of rows)
      target.append(evidence(row.name + " · later affiliation candidate", row));
  });
});

/* Documents audit page: coverage gaps, orphan files, dead links, duplicates.

   Read-mostly. The only write is "Délier", which reuses the existing DELETE
   routes — there is deliberately no way to delete a FILE from here: a certificate
   erased by mistake is unrecoverable, and the nightly mirror would propagate it.

   Rows are built with createElement/textContent, never innerHTML: filenames are
   user-supplied since the upload feature exists. */

const docSearch = document.getElementById("doc-search");
const docMeta = document.getElementById("doc-meta");
const docStats = document.getElementById("doc-stats");
const docStatus = document.getElementById("doc-status");

const SECTIONS = ["morts", "doublons", "orphelins", "sans_sds", "sans_bc", "sans_coa"];

const KIND_LABEL = { sds: "SDS", coa: "COA", bc: "BC" };
const KIND_ENDPOINT = {
  sds: { url: "api/material-sds", key: "materiau" },
  coa: { url: "api/coa-link", key: "reference" },
  bc: { url: "api/bc-link", key: "bon_commande" },
};

let audit = null;
let statusTimer = null;

// Accent-insensitive normalize (parity with the main search and categories page).
function norm(s) {
  return (s || "").toLowerCase().normalize("NFD").replace(/[̀-ͯ]/g, "");
}

function status(msg, err) {
  if (!docStatus) return;
  if (statusTimer) clearTimeout(statusTimer);
  docStatus.textContent = msg;
  docStatus.className = "edit-status " + (err ? "error" : "success");
  docStatus.classList.remove("hidden");
  if (!err) statusTimer = setTimeout(() => docStatus.classList.add("hidden"), 3000);
}

function td(parent, text) {
  const cell = document.createElement("td");
  cell.textContent = text === undefined || text === null ? "" : String(text);
  parent.appendChild(cell);
  return cell;
}

function fileName(path) {
  return (path || "").split("/").pop();
}

/* Deep link into the inventory. app.js reads ?q= on boot, so a reported gap is
   one click away from the ＋ button that fixes it. */
function linkCell(parent, label, query) {
  const cell = document.createElement("td");
  const a = document.createElement("a");
  a.href = "./?q=" + encodeURIComponent(query);
  a.textContent = label;
  a.title = `Ouvrir l'inventaire filtré sur « ${query} »`;
  cell.appendChild(a);
  parent.appendChild(cell);
  return cell;
}

function unlinkCell(parent, kind, cle, path) {
  const cell = document.createElement("td");
  const btn = document.createElement("button");
  btn.className = "btn-cancel btn-unlink";
  btn.textContent = "Délier";
  btn.setAttribute("aria-label", `Délier ${fileName(path)} de ${cle}`);
  btn.addEventListener("click", () => unlink(kind, cle, path, btn));
  cell.appendChild(btn);
  parent.appendChild(cell);
}

async function unlink(kind, cle, path, btn) {
  const cfg = KIND_ENDPOINT[kind];
  if (!cfg) return;
  btn.disabled = true;
  try {
    const res = await fetch(cfg.url, {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ [cfg.key]: cle, path }),
    });
    const data = await res.json();
    if (data.success) {
      status(`Lien supprimé : ${fileName(path)}`);
      await load();
    } else {
      status(data.error || "Erreur", true);
      btn.disabled = false;
    }
  } catch (e) {
    status(e.message, true);
    btn.disabled = false;
  }
}

// --- Filtering ---------------------------------------------------------------

const HAYSTACK = {
  morts: (e) => [KIND_LABEL[e.kind], e.cle, e.path],
  doublons: (e) => [KIND_LABEL[e.kind], e.cle, e.path],
  orphelins: (e) => [KIND_LABEL[e.kind], e.path],
  sans_sds: (e) => [e.materiau],
  sans_coa: (e) => [e.reference, e.materiau],
  sans_bc: (e) => [e.bon_commande].concat(e.references || []),
};

function filtered(section) {
  const rows = (audit && audit[section]) || [];
  const raw = docSearch ? docSearch.value.trim() : "";
  const tokens = raw.split(/\s+/).filter(Boolean).map(norm);
  if (!tokens.length) return rows;
  return rows.filter((e) => {
    const hay = norm(HAYSTACK[section](e).join(" "));
    return tokens.every((t) => hay.includes(t));
  });
}

// --- Rendering ---------------------------------------------------------------

const RENDER = {
  morts(tr, e) {
    td(tr, KIND_LABEL[e.kind] || e.kind);
    td(tr, e.cle);
    td(tr, e.path).className = "doc-path";
    td(tr, e.source);
    unlinkCell(tr, e.kind, e.cle, e.path);
  },
  doublons(tr, e) {
    td(tr, KIND_LABEL[e.kind] || e.kind);
    td(tr, e.cle);
    td(tr, e.path).className = "doc-path";
    td(tr, "×" + e.n);
    unlinkCell(tr, e.kind, e.cle, e.path);
  },
  orphelins(tr, e) {
    td(tr, KIND_LABEL[e.kind] || e.kind);
    const cell = document.createElement("td");
    const a = document.createElement("a");
    a.href = e.url;
    a.target = "_blank";
    a.rel = "noopener";
    a.textContent = e.name || fileName(e.path);
    cell.appendChild(a);
    tr.appendChild(cell);
    td(tr, e.path).className = "doc-path";
  },
  sans_sds(tr, e) {
    td(tr, e.materiau);
    td(tr, e.cibles);
    linkCell(tr, "Voir les cibles", e.materiau);
  },
  sans_coa(tr, e) {
    td(tr, e.reference);
    td(tr, e.materiau);
    linkCell(tr, "Ouvrir", e.reference);
  },
  sans_bc(tr, e) {
    td(tr, e.bon_commande);
    td(tr, e.cibles);
    const cell = document.createElement("td");
    // bon_commande is not FTS-indexed: link on each reference instead.
    (e.references || []).forEach((ref, i) => {
      if (i) cell.appendChild(document.createTextNode(", "));
      const a = document.createElement("a");
      a.href = "./?q=" + encodeURIComponent(ref);
      a.textContent = ref;
      cell.appendChild(a);
    });
    tr.appendChild(cell);
  },
};

function renderSection(section) {
  const tbody = document.getElementById("tb-" + section);
  const wrap = document.getElementById("sec-" + section);
  if (!tbody) return 0;
  tbody.innerHTML = "";

  const rows = filtered(section);
  const heading = wrap && wrap.querySelector("h2");
  if (heading && !heading.dataset.base) heading.dataset.base = heading.textContent;
  if (heading) heading.textContent = `${heading.dataset.base} (${rows.length})`;

  if (!rows.length) {
    const tr = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 6;
    cell.className = "coa-empty";
    cell.textContent = "Rien à signaler.";
    tr.appendChild(cell);
    tbody.appendChild(tr);
    return 0;
  }
  rows.forEach((e) => {
    const tr = document.createElement("tr");
    RENDER[section](tr, e);
    tbody.appendChild(tr);
  });
  return rows.length;
}

function renderStats() {
  if (!docStats || !audit) return;
  const s = audit.stats || {};
  const f = s.fichiers || {};
  const chips = [
    `${s.cibles || 0} cibles`,
    `${(f.sds || 0) + (f.coa || 0) + (f.bc || 0)} fichiers`,
    `${s.cibles_avec_sds || 0} avec SDS`,
    `${s.cibles_avec_coa || 0} avec COA`,
    `${s.orphelins || 0} orphelins`,
    `${s.morts || 0} liens morts`,
  ];
  docStats.innerHTML = "";
  chips.forEach((text) => {
    const div = document.createElement("div");
    div.className = "stats";
    div.textContent = text;
    docStats.appendChild(div);
  });
}

function renderAll() {
  const total = SECTIONS.reduce((n, s) => n + renderSection(s), 0);
  if (docMeta) {
    const q = docSearch ? docSearch.value.trim() : "";
    docMeta.textContent = q ? `Filtre : "${q}" — ${total} ligne(s)` : "";
  }
}

async function load() {
  try {
    const res = await fetch("api/documents-audit");
    if (!res.ok) throw new Error("HTTP " + res.status);
    audit = await res.json();
  } catch (e) {
    status("Chargement impossible : " + e.message, true);
    return;
  }
  renderStats();
  renderAll();
}

if (docSearch) docSearch.addEventListener("input", renderAll);
document.addEventListener("keydown", (e) => {
  if (e.key === "/" && document.activeElement !== docSearch) {
    e.preventDefault();
    if (docSearch) docSearch.focus();
  }
});

load();

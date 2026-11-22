const state = {
  q: "",
  sort: "",
  dir: "asc",
  limit: 200,
  offset: 0,
  columns: [],
};

const searchInput = document.getElementById("search");
const thead = document.getElementById("thead");
const tbody = document.getElementById("tbody");
const stats = document.getElementById("stats");
const statsNeuve = document.getElementById("stats-neuve");
const statsFinie = document.getElementById("stats-finie");
const meta = document.getElementById("meta");
// Two pagers (above and below the table) share these classes and are driven together.
const pagerPrevs = document.querySelectorAll(".pager-prev");
const pagerNexts = document.querySelectorAll(".pager-next");
const pagerInfos = document.querySelectorAll(".pager-info");
const pagerSizes = document.querySelectorAll(".pager-size-select");

function debounce(fn, delay) {
  let t;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), delay);
  };
}

// Default-visible columns first, then toggleable extras.
const viewColumns = [
  "materiau",
  "reference",
  "forme",
  "usure",
  "documents",
  "emplacement",
  "fournisseur",
  "purete",
  "epaisseur",
  "prix",
  "date_achat",
  "equipement",
  "dimensions",
  "remarque",
];

const columnLabels = {
  materiau: "Matériau",
  reference: "Référence",
  forme: "Forme",
  usure: "État",
  documents: "Documents",
  emplacement: "Emplacement",
  fournisseur: "Fournisseur",
  purete: "Pureté",
  epaisseur: "Épaisseur",
  prix: "Prix",
  date_achat: "Date",
  equipement: "Équipement",
  dimensions: "Dimensions",
  remarque: "Remarque",
};

// Every column is toggleable. These start hidden by default; the rest start visible.
const DEFAULT_HIDDEN = new Set(["prix", "date_achat", "equipement", "dimensions", "remarque"]);

function defaultVisibility() {
  const v = {};
  viewColumns.forEach((c) => { v[c] = !DEFAULT_HIDDEN.has(c); });
  return v;
}

let columnVisibility = defaultVisibility();

// Synthetic columns: not real DB columns, computed at render time.
// (the "materiau" dependency just guarantees the column is shown — data comes from row.*_docs)
const SYNTHETIC = {
  dimensions: ["longueur", "largeur", "hauteur", "diametre"],
  documents: ["materiau"],
};

function loadColumnVisibility() {
  columnVisibility = defaultVisibility();
  const saved = localStorage.getItem("columnVisibilityCibles");
  if (saved) {
    try {
      const s = JSON.parse(saved);
      Object.keys(columnVisibility).forEach((k) => {
        if (k in s) columnVisibility[k] = !!s[k];
      });
    } catch (e) {}
  }
}

function saveColumnVisibility() {
  localStorage.setItem("columnVisibilityCibles", JSON.stringify(columnVisibility));
}

function usureClass(s) {
  const l = (s || "").toLowerCase();
  if (l.startsWith("neuv")) return "u-neuve";
  if (l.startsWith("utilis")) return "u-utilisee";
  if (l.startsWith("fini")) return "u-finie";
  return "u-other";
}

async function advanceUsure(rowid) {
  try {
    const res = await fetch(`api/inventory/${rowid}/advance-usure`, { method: "POST" });
    if (res.status === 429) {
      alert("Trop de requêtes, réessayez dans un instant");
      return;
    }
    const data = await res.json();
    if (data.success) search();
  } catch (err) {
    console.error("advance-usure failed:", err);
  }
}

function renderUsure(row) {
  const wrap = document.createElement("div");
  wrap.className = "usure-cell";
  const value = (row.usure || "").trim() || "—";

  if (window.EDIT_ENABLED) {
    // The badge itself is the button; clicking cycles the state indefinitely.
    const btn = document.createElement("button");
    btn.className = "usure-badge usure-btn " + usureClass(value);
    btn.textContent = value;
    btn.title = "Cliquer pour changer l'état (Neuve → Utilisée → Finie → …)";
    btn.onclick = () => advanceUsure(row.rowid);
    wrap.appendChild(btn);
  } else {
    const badge = document.createElement("span");
    badge.className = "usure-badge " + usureClass(value);
    badge.textContent = value;
    wrap.appendChild(badge);
  }
  return wrap;
}

function startRemarkEdit(td, row) {
  if (td.querySelector("textarea")) return; // already editing
  const original = row.remarque || "";
  td.textContent = "";
  td.classList.remove("empty-hint");
  td.onclick = null;
  td.title = "";

  const ta = document.createElement("textarea");
  ta.className = "remarque-edit";
  ta.value = original;
  td.appendChild(ta);
  ta.focus();
  ta.setSelectionRange(ta.value.length, ta.value.length);

  let done = false;
  const finish = async (saveIt) => {
    if (done) return;
    done = true;
    const newVal = ta.value;
    if (saveIt && newVal !== original) {
      await saveRemark(row.rowid, newVal, row.version);
    }
    search(); // re-render the table
  };

  ta.addEventListener("blur", () => finish(true));
  ta.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      ta.blur(); // triggers save
    } else if (e.key === "Escape") {
      e.preventDefault();
      finish(false); // cancel
    }
  });
}

async function saveRemark(rowid, value, version) {
  try {
    const body = { remarque: value };
    if (version != null) body._version = version;
    const res = await fetch(`api/inventory/${rowid}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (res.status === 429) {
      alert("Trop de requêtes, réessayez dans un instant");
      return;
    }
    const data = await res.json();
    if (!data.success) alert(data.error || "Erreur d'enregistrement");
  } catch (err) {
    console.error("saveRemark failed:", err);
  }
}

// UN seul badge par type, compte en suffixe : "SDS ×9" au lieu de neuf badges "SDS 1…SDS 9".
// Certains materiaux ont jusqu'a 9 SDS : en badges individuels la cellule faisait 444px de
// large, et une fois la colonne bornee elle debordait en HAUTEUR (ligne a 85px au lieu de 51).
// Le badge ouvre le premier document ; le bouton « ＋ » donne acces a la liste complete.
function docLinks(label, docs, cls) {
  const span = document.createElement("span");
  span.className = "doc-group";
  const first = docs[0];
  const a = document.createElement("a");
  a.href = first.url;
  a.target = "_blank";
  a.rel = "noopener";
  a.className = "doc-link " + cls +
    (first.source && first.source.indexOf("auto") === 0 ? " doc-auto" : "");
  a.textContent = docs.length > 1 ? `${label} ×${docs.length}` : label;
  const fname = (first.path || "").split("/").pop();
  a.title = docs.length > 1
    ? `${docs.length} documents — ouvre « ${fname} » ; ＋ pour la liste complète`
    : fname + (first.source ? ` (${first.source})` : "");
  span.appendChild(a);
  return span;
}

function renderDocs(row) {
  const wrap = document.createElement("div");
  wrap.className = "docs-cell";
  const sds = row.sds_docs || [];
  const coa = row.coa_docs || [];
  const bc = row.bc_docs || [];
  if (sds.length) wrap.appendChild(docLinks("SDS", sds, "doc-sds"));
  if (coa.length) wrap.appendChild(docLinks("COA", coa, "doc-coa"));
  if (bc.length) wrap.appendChild(docLinks("BC", bc, "doc-bc"));

  // Manage-documents button (SDS by material, COA by reference, BC by order no).
  if (window.EDIT_ENABLED && window.DocsModal) {
    const btn = document.createElement("button");
    btn.className = "btn-doc-add";
    btn.textContent = "＋";
    btn.title = "Gérer les documents (SDS / COA / bon de commande)";
    btn.onclick = () => DocsModal.open(row);
    wrap.appendChild(btn);
  }
  return wrap;
}

function renderDimensions(row) {
  const nz = (v) => v && String(v).trim() && String(v).trim() !== "0";
  const parts = [];
  const d = (row.diametre || "").trim();
  if (nz(d)) parts.push("Ø" + d);
  const rect = [row.longueur, row.largeur, row.hauteur]
    .map((v) => (v || "").trim())
    .filter(nz);
  if (rect.length) parts.push(rect.join("×"));
  return parts.join(" ");
}

async function fetchColumns() {
  const res = await fetch("api/columns");
  const data = await res.json();
  const available = data.columns || [];

  state.columns = viewColumns.filter((c) => {
    let exists;
    if (c in SYNTHETIC) {
      exists = SYNTHETIC[c].some((real) => available.includes(real));
    } else {
      exists = available.includes(c);
    }
    if (c in columnVisibility) {
      return exists && columnVisibility[c];
    }
    return exists;
  });
  renderHeader();
}

function renderHeader() {
  const row = document.createElement("tr");

  if (window.EDIT_ENABLED) {
    const th = document.createElement("th");
    th.textContent = "";
    // Largeur pilotee par le CSS (th:not([data-col])) : un style inline la figeait a 80px
    // et rendait la regle CSS inoperante.
    row.appendChild(th);
  }

  state.columns.forEach((col) => {
    const th = document.createElement("th");
    th.textContent = columnLabels[col] || col;
    th.dataset.col = col;
    // Synthetic columns are not sortable server-side.
    const sortable = !(col in SYNTHETIC);
    // Sans cette distinction, `cursor: pointer` s'appliquait a TOUS les en-tetes, y compris
    // Documents/Dimensions qui ne se trient pas : affordance trompeuse.
    th.classList.add(sortable ? "sortable" : "not-sortable");
    if (sortable) {
      th.title = `Trier par ${columnLabels[col] || col}`;
      if (state.sort === col) {
        th.classList.add(state.dir === "asc" ? "sort-asc" : "sort-desc");
        th.setAttribute("aria-sort", state.dir === "asc" ? "ascending" : "descending");
      }
      th.addEventListener("click", () => {
        if (state.sort === col) {
          state.dir = state.dir === "asc" ? "desc" : "asc";
        } else {
          state.sort = col;
          state.dir = "asc";
        }
        state.offset = 0;
        renderHeader();
        search();
      });
    }
    row.appendChild(th);
  });
  thead.innerHTML = "";
  thead.appendChild(row);
}

async function search() {
  const params = new URLSearchParams({
    q: state.q,
    sort: state.sort,
    dir: state.dir,
    limit: String(state.limit),
    offset: String(state.offset),
  });
  const res = await fetch(`api/search?${params.toString()}`);
  const data = await res.json();
  renderRows(data.rows || []);
  const total = data.total ?? 0;
  const returned = data.returned ?? 0;
  const from = total === 0 ? 0 : state.offset + 1;
  const to = state.offset + returned;
  stats.textContent = `${from} – ${to} / ${total} cibles`;
  if (statsNeuve) statsNeuve.textContent = `Neuves ${data.count_neuve ?? 0}`;
  if (statsFinie) statsFinie.textContent = `Finies ${data.count_finie ?? 0}`;
  meta.textContent = state.q ? `Filtre: "${state.q}"` : "";
  renderPagination(total);
}

function renderPagination(total) {
  const pageCount = Math.max(1, Math.ceil(total / state.limit));
  const page = Math.floor(state.offset / state.limit) + 1;
  const atStart = state.offset <= 0;
  const atEnd = state.offset + state.limit >= total;
  pagerInfos.forEach((el) => { el.textContent = `Page ${page} / ${pageCount}`; });
  pagerPrevs.forEach((el) => { el.disabled = atStart; });
  pagerNexts.forEach((el) => { el.disabled = atEnd; });
}

function renderRows(rows) {
  tbody.innerHTML = "";
  rows.forEach((row) => {
    const tr = document.createElement("tr");

    if (window.EDIT_ENABLED) {
      const tdEdit = document.createElement("td");
      const btn = document.createElement("button");
      btn.className = "btn-edit";
      btn.textContent = "✎";
      btn.title = "Modifier cette cible";
      btn.setAttribute("aria-label", `Modifier ${row.reference || row.materiau || "cette cible"}`);
      btn.onclick = () => EditModal.open(row.rowid);
      tdEdit.appendChild(btn);
      tr.appendChild(tdEdit);
    }

    state.columns.forEach((col) => {
      const td = document.createElement("td");
      // Permet au CSS de cibler une colonne precise (alignement des valeurs numeriques).
      td.dataset.col = col;
      if (col === "usure") {
        td.appendChild(renderUsure(row));
      } else if (col === "documents") {
        td.appendChild(renderDocs(row));
      } else if (col === "dimensions") {
        const dims = renderDimensions(row);
        td.textContent = dims;
        if (dims) td.title = dims;
      } else if (col === "remarque") {
        const text = row.remarque || "";
        td.className = "wrap cell-remarque";
        if (window.EDIT_ENABLED) {
          td.classList.add("editable");
          if (text) {
            td.textContent = text;
            td.title = text;
          } else {
            td.textContent = "✎";
            td.classList.add("empty-hint");
            td.title = "Ajouter une remarque";
          }
          td.onclick = () => startRemarkEdit(td, row);
        } else {
          td.textContent = text;
          if (text) td.title = text;
        }
      } else {
        // Les colonnes ont une largeur fixe et tronquent (ellipsis) : le title garantit
        // que la valeur complete reste accessible au survol.
        const text = row[col] || "";
        td.textContent = text;
        if (text) td.title = text;
      }
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  });
}

const onInput = debounce((e) => {
  state.q = e.target.value.trim();
  state.offset = 0;
  search();
}, 120);

searchInput.addEventListener("input", onInput);

// « / » place le focus dans la recherche depuis n'importe ou (sauf si on tape deja
// dans un champ). Echap vide la recherche. Deux gestes de moins a la souris.
document.addEventListener("keydown", (e) => {
  const dansUnChamp = /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName);
  if (e.key === "/" && !dansUnChamp) {
    e.preventDefault();
    searchInput.focus();
    searchInput.select();
  } else if (e.key === "Escape" && e.target === searchInput && searchInput.value) {
    searchInput.value = "";
    onInput({ target: searchInput });
  }
});

// Changing page keeps the scroll position at the pager (bottom of the list), so go back
// up to the search bar. The site header is sticky, so its height is measured live and
// subtracted — otherwise the search bar would land hidden behind it.
function scrollToSearch() {
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const behavior = reduce ? "auto" : "smooth";
  const target = document.querySelector(".controls");
  if (!target) {
    window.scrollTo({ top: 0, behavior });
    return;
  }
  const header = document.querySelector(".site-header");
  const headerH = header ? header.getBoundingClientRect().height : 0;
  const top = Math.max(0, window.scrollY + target.getBoundingClientRect().top - headerH - 8);
  // Only ever scroll UP. Using the top pager already leaves the search bar in view, and
  // scrolling down to "reach" it would yank the page for no reason.
  if (window.scrollY <= top) return;
  window.scrollTo({ top, behavior });
}

// Pagination controls — the same handler is bound to the top and bottom pagers.
function goToPage(mutateOffset) {
  return () => {
    mutateOffset();
    search();
    scrollToSearch();
  };
}

pagerPrevs.forEach((btn) =>
  btn.addEventListener("click", goToPage(() => {
    state.offset = Math.max(0, state.offset - state.limit);
  }))
);
pagerNexts.forEach((btn) =>
  btn.addEventListener("click", goToPage(() => {
    state.offset += state.limit;
  }))
);
pagerSizes.forEach((sel) =>
  sel.addEventListener("change", goToPage(() => {
    state.limit = parseInt(sel.value, 10) || 200;
    state.offset = 0;
    // Keep both selects showing the same page size.
    pagerSizes.forEach((other) => { other.value = sel.value; });
  }))
);

// Export button handler
const exportBtn = document.getElementById("export-btn");
if (exportBtn) {
  exportBtn.addEventListener("click", () => {
    const params = new URLSearchParams({ q: state.q, format: "txt" });
    window.location.href = `api/export?${params.toString()}`;
  });
}

// Build one visibility toggle per column (all columns are toggleable).
function buildToggleButtons() {
  const rowEl = document.getElementById("toggle-row");
  if (!rowEl) return;
  rowEl.innerHTML = "";
  viewColumns.forEach((col) => {
    const btn = document.createElement("button");
    btn.className = "toggle-btn";
    btn.dataset.column = col;
    btn.textContent = columnLabels[col] || col;
    btn.classList.toggle("active", !!columnVisibility[col]);
    btn.addEventListener("click", () => {
      columnVisibility[col] = !columnVisibility[col];
      btn.classList.toggle("active", columnVisibility[col]);
      saveColumnVisibility();
      fetchColumns().then(() => search());
    });
    rowEl.appendChild(btn);
  });
}

(async function init() {
  // Deep link from the Documents page: ./?q=Ni lands on the matching rows so a
  // reported gap has a one-click path to its fix.
  const boot = new URLSearchParams(location.search).get("q");
  if (boot) {
    state.q = boot;
    if (searchInput) searchInput.value = boot;
  }
  loadColumnVisibility();
  buildToggleButtons();
  await fetchColumns();
  await search();
})();

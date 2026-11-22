const catTbody = document.getElementById("cat-tbody");
const statsCats = document.getElementById("stats-cats");
const statsBelow = document.getElementById("stats-below");
const catSearch = document.getElementById("cat-search");
const catMeta = document.getElementById("cat-meta");

let allCategories = [];
let belowOnly = false;

// Accent-insensitive normalize + shape abbreviations (parity with the main search).
function norm(s) {
  return (s || "").toLowerCase().normalize("NFD").replace(/[̀-ͯ]/g, "");
}
const SHAPE_ABBR = { rect: "rectangulaire", circ: "circulaire", cyl: "cylindrique" };

function tokenMatches(cat, token) {
  const shapeToken = SHAPE_ABBR[token] || token;
  return (
    norm(cat.materiau).includes(token) ||
    norm(cat.forme).includes(shapeToken) ||
    norm(cat.dimension).includes(token)
  );
}

async function loadCategories() {
  const res = await fetch("api/categories");
  const data = await res.json();
  allCategories = data.categories || [];
  applyFilter();
}

function applyFilter() {
  const raw = catSearch ? catSearch.value.trim() : "";
  // Multi-term AND: each token must match the material or the shape (like the main search).
  const tokens = raw.split(/\s+/).filter(Boolean).map(norm);
  let filtered = tokens.length
    ? allCategories.filter((c) => tokens.every((t) => tokenMatches(c, t)))
    : allCategories;
  if (belowOnly) filtered = filtered.filter((c) => c.below);
  render(filtered);

  if (catMeta) {
    const notes = [];
    if (raw) notes.push(`Filtre: "${raw}"`);
    if (belowOnly) notes.push("sous seuil uniquement");
    catMeta.textContent = notes.join(" · ");
  }
}

function render(categories) {
  catTbody.innerHTML = "";
  let belowCount = 0;

  categories.forEach((cat) => {
    const tr = document.createElement("tr");
    if (cat.below) {
      tr.className = "below";
      belowCount++;
    }

    const tdMat = document.createElement("td");
    tdMat.textContent = cat.materiau || "—";
    tr.appendChild(tdMat);

    const tdForme = document.createElement("td");
    tdForme.textContent = cat.forme || "—";
    tr.appendChild(tdForme);

    const tdDim = document.createElement("td");
    tdDim.textContent = cat.dimension || "—";
    tr.appendChild(tdDim);

    const tdCount = document.createElement("td");
    const count = document.createElement("span");
    count.className = "cat-count";
    count.textContent = cat.count;
    tdCount.appendChild(count);
    if (cat.below) {
      const badge = document.createElement("span");
      badge.className = "cat-badge-low";
      badge.textContent = "sous seuil";
      tdCount.appendChild(badge);
    }
    tr.appendChild(tdCount);

    const tdThreshold = document.createElement("td");
    const input = document.createElement("input");
    input.type = "number";
    input.min = "0";
    input.className = "cat-threshold-input";
    input.value = cat.threshold ?? "";
    input.placeholder = "—";
    input.addEventListener("change", () => saveThreshold(cat.category, input.value));
    tdThreshold.appendChild(input);
    tr.appendChild(tdThreshold);

    catTbody.appendChild(tr);
  });

  statsCats.textContent = `${categories.length} catégorie${categories.length > 1 ? "s" : ""}`;
  statsBelow.textContent = `${belowCount} sous seuil`;
}

if (catSearch) {
  catSearch.addEventListener("input", applyFilter);
}

if (statsBelow) {
  statsBelow.classList.add("clickable");
  statsBelow.title = "Cliquer pour n'afficher que les catégories sous seuil";
  statsBelow.addEventListener("click", () => {
    belowOnly = !belowOnly;
    statsBelow.classList.toggle("active", belowOnly);
    applyFilter();
  });
}

async function saveThreshold(categoryKey, value) {
  try {
    const res = await fetch(`api/categories/${encodeURIComponent(categoryKey)}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ min_count: value === "" ? null : value }),
    });
    if (res.status === 429) {
      alert("Trop de requêtes, réessayez dans un instant");
      return;
    }
    const data = await res.json();
    if (data.success) {
      loadCategories();
    } else {
      alert(data.error || "Erreur");
    }
  } catch (err) {
    console.error("saveThreshold failed:", err);
  }
}

loadCategories();

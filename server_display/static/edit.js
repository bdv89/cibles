/**
 * edit.js - Inventory editing functionality (separate from app.js display logic)
 * Provides modal-based editing with Basic Auth support.
 */

// The three wear states, in cycle order (mirrors USURE_STATES in import_cibles_xlsx.py
// and the badge cycle in app.js).
const USURE_STATES = ["Neuve", "Utilisée", "Finie"];

// Editable fields configuration (cibles inventory)
const EDITABLE_FIELDS = [
  { key: "materiau", label: "Matériau", type: "text", datalist: "materiau" },
  { key: "reference", label: "Référence", type: "text", nextRef: true },
  { key: "forme", label: "Forme", type: "text", datalist: "forme", hint: "Circulaire, Rectangulaire, Cylindrique" },
  // Closed vocabulary: a STATIC list, not one derived from the DB via api/suggestions.
  // A derived list would be self-reinforcing (one typo becomes everyone's suggestion).
  { key: "usure", label: "État", type: "text", values: USURE_STATES, hint: "Neuve, Utilisée, Finie" },
  { key: "emplacement", label: "Emplacement", type: "text", datalist: "emplacement", hint: "auto depuis la référence" },
  { key: "fournisseur", label: "Fournisseur", type: "text", datalist: "fournisseur" },
  { key: "purete", label: "Pureté", type: "text", datalist: "purete" },
  { key: "prix", label: "Prix", type: "text" },
  { key: "date_achat", label: "Date", type: "text" },
  { key: "equipement", label: "Équipement", type: "text" },
  { key: "particularite", label: "Particularité", type: "text" },
  { key: "longueur", label: "Longueur", type: "text" },
  { key: "largeur", label: "Largeur", type: "text" },
  { key: "hauteur", label: "Hauteur", type: "text" },
  { key: "diametre", label: "Diamètre", type: "text" },
  { key: "epaisseur", label: "Épaisseur", type: "text" },
  { key: "bon_commande", label: "N° Bon de commande", type: "text" },
  { key: "remarque", label: "Remarque", type: "text" },
];

// Current row being edited
let currentRowId = null;
let currentVersion = null;

// Cached autocomplete suggestions (distinct values per field), loaded once.
let SUGGESTIONS = {};

async function loadSuggestions() {
  try {
    const res = await fetch("api/suggestions");
    SUGGESTIONS = await res.json();
  } catch (err) {
    console.error("Failed to load suggestions:", err);
  }
}

// Location code = token immediately before the trailing number (mirrors server logic).
function emplacementFromRef(ref) {
  const m = (ref || "").match(/^(.*?)(\d+)$/);
  if (!m) return "";
  const prefix = m[1].replace(/[-_. ]+$/, "");
  if (!prefix) return "";
  const tokens = prefix.split(/[-_. ]+/);
  if (tokens.some((t) => t.toUpperCase() === "AGC")) return "AGC";
  return tokens.length ? tokens[tokens.length - 1] : "";
}

// Fill the reference input with the suggested next number for its family.
async function fillNextReference(input) {
  const current = (input.value || "").trim();
  try {
    const res = await fetch(`api/next-reference?prefix=${encodeURIComponent(current)}`);
    const data = await res.json();
    if (data.next) {
      input.value = data.next;
      const emp = document.getElementById("field-emplacement");
      if (emp) emp.value = emplacementFromRef(input.value);
      input.focus();
    } else {
      showStatus("Saisissez d'abord un préfixe de référence (ex. Ag-TM2-ABBE-)", true);
    }
  } catch (err) {
    console.error("next-reference failed:", err);
  }
}

// Write operations are open (no password required); rate-limited server-side.
async function authFetch(url, options = {}) {
  options.headers = {
    ...options.headers,
    "Content-Type": "application/json",
  };

  const resp = await fetch(url, options);

  if (resp.status === 429) {
    throw new Error("Trop de requetes, reessayez dans un instant");
  }

  return resp;
}

// Modal management
function getModal() {
  return document.getElementById("edit-modal");
}

function showModal() {
  const modal = getModal();
  if (modal) {
    modal.classList.remove("hidden");
    document.body.style.overflow = "hidden";
  }
}

function hideModal() {
  const modal = getModal();
  if (modal) {
    modal.classList.add("hidden");
    document.body.style.overflow = "";
  }
  currentRowId = null;
  currentVersion = null;
}

// Build form fields
function buildForm(row = {}) {
  const form = document.getElementById("edit-form");
  if (!form) return;

  form.innerHTML = "";

  EDITABLE_FIELDS.forEach((field) => {
    const div = document.createElement("div");
    div.className = "form-group";

    const label = document.createElement("label");
    label.textContent = field.label;
    label.htmlFor = `field-${field.key}`;

    const input = document.createElement("input");
    input.type = field.type === "number" ? "number" : "text";
    input.id = `field-${field.key}`;
    input.name = field.key;
    input.value = row[field.key] || "";
    if (field.type === "number") {
      input.step = "any";
      input.min = "0";
    }
    if (field.hint) {
      input.placeholder = field.hint;
    }

    div.appendChild(label);

    if (field.nextRef) {
      // Reference field: input + "next number" helper button on one row.
      const row = document.createElement("div");
      row.className = "input-row";
      row.appendChild(input);
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "btn-nextref";
      btn.textContent = "⟳ N° suivant";
      btn.title = "Proposer le prochain numéro pour cette famille de référence";
      btn.addEventListener("click", () => fillNextReference(input));
      row.appendChild(btn);
      div.appendChild(row);
      // Live-derive emplacement from the reference as the user types.
      input.addEventListener("input", () => {
        const emp = document.getElementById("field-emplacement");
        if (emp) emp.value = emplacementFromRef(input.value);
      });
    } else {
      div.appendChild(input);
    }

    // Autocomplete via a native <datalist>: either a static `values` list (closed
    // vocabularies) or values derived from existing rows through api/suggestions.
    if (field.datalist || field.values) {
      const listId = `dl-${field.key}`;
      input.setAttribute("list", listId);
      const dl = document.createElement("datalist");
      dl.id = listId;
      (field.values || SUGGESTIONS[field.datalist] || []).forEach((v) => {
        const opt = document.createElement("option");
        opt.value = v;
        dl.appendChild(opt);
      });
      div.appendChild(dl);
    }

    // Add validation warning container
    if (field.validate) {
      const warning = document.createElement("div");
      warning.className = "field-warning hidden";
      warning.id = `warning-${field.key}`;
      div.appendChild(warning);

      input.addEventListener("blur", () => {
        if (field.validate === "cas") {
          const result = validateCAS(input.value);
          if (!result.valid) {
            warning.textContent = result.message;
            warning.classList.remove("hidden");
          } else {
            warning.classList.add("hidden");
          }
        }
      });
    }

    form.appendChild(div);
  });
}

// Get form data
function getFormData() {
  const data = {};
  EDITABLE_FIELDS.forEach((field) => {
    const input = document.getElementById(`field-${field.key}`);
    if (input) {
      data[field.key] = input.value;
    }
  });
  return data;
}

// Show status message
function showStatus(message, isError = false) {
  const status = document.getElementById("edit-status");
  if (status) {
    status.textContent = message;
    status.className = isError ? "edit-status error" : "edit-status success";
    status.classList.remove("hidden");
    setTimeout(() => status.classList.add("hidden"), 3000);
  }
}

// Public API
window.EditModal = {
  async open(rowid) {
    try {
      const resp = await fetch(`api/inventory/${rowid}`);
      if (!resp.ok) {
        alert("Erreur: ligne non trouvee");
        return;
      }
      const data = await resp.json();
      const row = data.row || {};

      currentRowId = rowid;
      currentVersion = row.version || 1;

      const title = document.getElementById("edit-modal-title");
      if (title) title.textContent = `Modifier: ${row.reference || row.materiau || ""}`;

      buildForm(row);
      showModal();
    } catch (err) {
      alert("Erreur: " + err.message);
    }
  },

  create() {
    currentRowId = null;
    currentVersion = null;

    const title = document.getElementById("edit-modal-title");
    if (title) title.textContent = "Nouvelle entree";

    buildForm({});
    showModal();
  },

  close() {
    hideModal();
  },

  async save() {
    const data = getFormData();

    // Add version for conflict detection
    if (currentVersion !== null) {
      data._version = currentVersion;
    }

    try {
      let resp;
      if (currentRowId) {
        // Update existing
        resp = await authFetch(`api/inventory/${currentRowId}`, {
          method: "PUT",
          body: JSON.stringify(data),
        });
      } else {
        // Create new
        resp = await authFetch("api/inventory", {
          method: "POST",
          body: JSON.stringify(data),
        });
      }

      const result = await resp.json();

      if (result.success) {
        hideModal();
        showStatus(currentRowId ? "Modification enregistree" : "Entree creee");
        // Refresh the table
        if (typeof search === "function") {
          search();
        }
      } else {
        showStatus(result.error || "Erreur inconnue", true);
      }
    } catch (err) {
      showStatus(err.message, true);
    }
  },

  async delete() {
    if (!currentRowId) return;

    if (!confirm("Voulez-vous vraiment supprimer cette entree?")) {
      return;
    }

    try {
      const resp = await authFetch(`api/inventory/${currentRowId}`, {
        method: "DELETE",
      });

      const result = await resp.json();

      if (result.success) {
        hideModal();
        showStatus("Entree supprimee");
        // Refresh the table
        if (typeof search === "function") {
          search();
        }
      } else {
        showStatus(result.error || "Erreur", true);
      }
    } catch (err) {
      showStatus(err.message, true);
    }
  },
};

// Check if editing is enabled and set up UI
async function initEdit() {
  try {
    const resp = await fetch("api/edit-enabled");
    const data = await resp.json();
    window.EDIT_ENABLED = data.enabled;

    if (data.enabled) {
      // Preload autocomplete suggestions for the edit form.
      loadSuggestions();

      // Show add button if it exists
      const addBtn = document.getElementById("add-entry-btn");
      if (addBtn) {
        addBtn.classList.remove("hidden");
        addBtn.addEventListener("click", () => EditModal.create());
      }

      // Re-render table to show edit buttons (if app.js has already loaded)
      if (typeof renderHeader === "function") {
        renderHeader();
      }
      if (typeof search === "function") {
        search();
      }
    }
  } catch (err) {
    console.error("Failed to check edit status:", err);
    window.EDIT_ENABLED = false;
  }
}

// Close modal on escape key
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    hideModal();
  }
});

// Close modal when clicking outside
document.addEventListener("click", (e) => {
  const modal = getModal();
  if (modal && e.target === modal) {
    hideModal();
  }
});

// Initialize on load
initEdit();

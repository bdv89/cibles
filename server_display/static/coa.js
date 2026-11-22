/* Document management modal (per inventory row): view / link / unlink / upload
   SDS (by material), COA (by reference) and BC (by purchase-order number).

   Sections are generated from KINDS rather than copy-pasted: three near-identical
   blocks in the markup, the list renderer, the pickers and open() would be six
   places to keep in sync. Element ids stay `docs-{id}-*`.

   All URLs are relative (no leading slash): the app is served under /cibles/. */
(function () {
  const KINDS = [
    {
      id: "sds",
      title: "SDS",
      key: "materiau",
      endpoint: "api/material-sds",
      files: "api/sds-files",
      // Source of truth is documents.py SDS_EXT / COA_EXT; the server revalidates.
      accept: ".pdf,.docx,.doc",
      hint: "PDF, DOCX, DOC — 100 Mo max",
      placeholder: "Rechercher une SDS…",
      note: (row) =>
        row.materiau ? `(matériau ${row.materiau} — partagé)` : "(matériau manquant)",
    },
    {
      id: "coa",
      title: "Certificats (COA)",
      key: "reference",
      endpoint: "api/coa-link",
      files: "api/coa-files",
      accept: ".pdf,.xlsx,.jpg,.jpeg,.png,.docx",
      hint: "PDF, XLSX, JPG, PNG, DOCX — 100 Mo max",
      placeholder: "Rechercher un certificat…",
      note: (row) =>
        (row.reference || "").trim() ? `(réf. ${row.reference})` : "(référence manquante)",
    },
    {
      id: "bc",
      title: "Bon de commande",
      key: "bon_commande",
      endpoint: "api/bc-link",
      files: "api/bc-files",
      accept: ".pdf,.xlsx,.jpg,.jpeg,.png,.docx",
      hint: "PDF, XLSX, JPG, PNG, DOCX — 100 Mo max",
      placeholder: "Rechercher un bon de commande…",
      note: (row) =>
        (row.bon_commande || "").trim()
          ? `(bon ${row.bon_commande} — partagé)`
          : "(aucun bon de commande sur cette cible)",
    },
  ];

  const byId = {};
  KINDS.forEach((k) => (byId[k.id] = k));

  const fileCache = {};
  let currentRow = null;
  let lastTrigger = null;
  let statusTimer = null;

  function el(id) {
    return document.getElementById(id);
  }

  async function ensureFiles(kind) {
    if (!fileCache[kind]) fileCache[kind] = await fetchFiles(byId[kind].files);
    return fileCache[kind];
  }

  async function fetchFiles(url) {
    try {
      const res = await fetch(url);
      const data = await res.json();
      return data.files || [];
    } catch (e) {
      return [];
    }
  }

  function sectionHtml(k) {
    return `
      <h3 class="docs-h">${k.title} <span class="docs-note" id="docs-${k.id}-note"></span></h3>
      <div id="docs-${k.id}-list"></div>
      <div class="coa-add-row">
        <input id="docs-${k.id}-input" list="docs-${k.id}-datalist"
               placeholder="${k.placeholder}" autocomplete="off"
               aria-label="${k.title} — rechercher un fichier déjà présent" />
        <datalist id="docs-${k.id}-datalist"></datalist>
        <button type="button" class="btn-save" id="docs-${k.id}-add">Lier</button>
        <input type="file" id="docs-${k.id}-file" class="coa-file-input"
               accept="${k.accept}" hidden />
        <button type="button" class="btn-save coa-upload" id="docs-${k.id}-upload"
                aria-describedby="docs-${k.id}-hint">Ajouter</button>
      </div>
      <p class="docs-hint" id="docs-${k.id}-hint">${k.hint}</p>`;
  }

  function buildModal() {
    let modal = el("docs-modal");
    if (modal) return modal;
    modal = document.createElement("div");
    modal.id = "docs-modal";
    modal.className = "modal hidden";
    modal.innerHTML = `
      <div class="modal-content" role="dialog" aria-modal="true" aria-labelledby="docs-title">
        <h2>Documents — <span id="docs-title"></span></h2>
        ${KINDS.map(sectionHtml).join("")}
        <div id="docs-status" class="edit-status hidden" role="status" aria-live="polite"></div>
        <div class="modal-actions">
          <button type="button" class="btn-cancel" id="docs-close">Fermer</button>
        </div>
      </div>`;
    document.body.appendChild(modal);
    modal.addEventListener("click", (e) => {
      if (e.target === modal) hide();
    });
    el("docs-close").addEventListener("click", hide);
    KINDS.forEach((k) => {
      el(`docs-${k.id}-add`).addEventListener("click", () => attach(k.id));
      el(`docs-${k.id}-upload`).addEventListener("click", () => el(`docs-${k.id}-file`).click());
      el(`docs-${k.id}-file`).addEventListener("change", (e) => upload(k.id, e.target));
    });
    return modal;
  }

  function onKeydown(e) {
    if (e.key === "Escape") {
      e.stopPropagation();
      hide();
    }
  }

  function hide() {
    const m = el("docs-modal");
    if (m) m.classList.add("hidden");
    document.removeEventListener("keydown", onKeydown, true);
    document.body.style.overflow = "";
    currentRow = null;
    // Give the keyboard back to the ＋ button that opened the modal.
    if (lastTrigger && document.contains(lastTrigger)) lastTrigger.focus();
    lastTrigger = null;
  }

  // ms = 0 keeps the message on screen: "sending…" and errors must not vanish
  // under the reader, and an upload result carries a filename worth reading.
  function status(msg, err, ms) {
    const s = el("docs-status");
    if (!s) return;
    if (statusTimer) clearTimeout(statusTimer);
    s.textContent = msg;
    s.className = "edit-status " + (err ? "error" : "success");
    s.classList.remove("hidden");
    const delay = ms === undefined ? 2500 : ms;
    if (delay > 0) statusTimer = setTimeout(() => s.classList.add("hidden"), delay);
  }

  function busy(on) {
    const content = document.querySelector("#docs-modal .modal-content");
    if (content) content.setAttribute("aria-busy", on ? "true" : "false");
    KINDS.forEach((k) => {
      const add = el(`docs-${k.id}-add`);
      const up = el(`docs-${k.id}-upload`);
      if (on) {
        add.disabled = true;
        up.disabled = true;
      } else {
        applyAvailability(k);
      }
    });
  }

  function hasKey(k) {
    return !!(currentRow && (currentRow[k.key] || "").trim());
  }

  function applyAvailability(k) {
    const available = hasKey(k);
    ["add", "upload", "input"].forEach((suffix) => {
      const node = el(`docs-${k.id}-${suffix}`);
      if (!node) return;
      node.disabled = !available;
      // A disabled control is neither focusable nor announced: point at the note
      // so the reason ("référence manquante") stays reachable.
      if (available) node.removeAttribute("aria-describedby");
      else node.setAttribute("aria-describedby", `docs-${k.id}-note`);
    });
    if (available) {
      el(`docs-${k.id}-upload`).setAttribute("aria-describedby", `docs-${k.id}-hint`);
    }
  }

  function docsFor(kind) {
    return (currentRow && currentRow[kind + "_docs"]) || [];
  }

  function renderList(kind) {
    const box = el(`docs-${kind}-list`);
    box.innerHTML = "";
    const docs = docsFor(kind);
    if (!docs.length) {
      const p = document.createElement("p");
      p.className = "coa-empty";
      p.textContent = "Aucun document lié.";
      box.appendChild(p);
      return;
    }
    docs.forEach((d) => {
      const item = document.createElement("div");
      item.className = "coa-item";
      const a = document.createElement("a");
      a.href = d.url;
      a.target = "_blank";
      a.rel = "noopener";
      a.textContent = (d.path || "").split("/").pop();
      const tag = document.createElement("span");
      tag.className = "coa-src";
      tag.textContent = d.source || "";
      const rm = document.createElement("button");
      rm.className = "coa-remove";
      rm.textContent = "✕";
      rm.title = "Délier ce document";
      rm.setAttribute("aria-label", "Délier " + a.textContent);
      rm.onclick = () => remove(kind, d.path);
      item.appendChild(a);
      item.appendChild(tag);
      item.appendChild(rm);
      box.appendChild(item);
    });
  }

  function keyField(kind) {
    const k = byId[kind];
    return { [k.key]: currentRow[k.key] };
  }

  async function attach(kind) {
    const input = el(`docs-${kind}-input`);
    const val = (input.value || "").trim();
    if (!val || !currentRow) return;

    const files = await ensureFiles(kind);
    const match = files.find((f) => f.path === val || f.name === val);
    // Never post raw input: it used to create a link to a file that does not
    // exist, showing a badge that 404s on click.
    if (!match) {
      status("Fichier introuvable — utilisez « Ajouter » pour le déposer", true, 0);
      return;
    }

    try {
      const res = await fetch(byId[kind].endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...keyField(kind), path: match.path }),
      });
      if (res.status === 429) {
        status("Trop de requêtes", true, 0);
        return;
      }
      const data = await res.json();
      if (data.success) {
        input.value = "";
        status(data.message || "Document lié");
        await refreshRow();
      } else status(data.error || "Erreur", true, 0);
    } catch (e) {
      status(e.message, true, 0);
    }
  }

  async function upload(kind, inputEl) {
    const file = inputEl.files && inputEl.files[0];
    if (!file || !currentRow) return;

    const fd = new FormData();
    fd.append("kind", kind);
    fd.append("key", currentRow[byId[kind].key] || "");
    fd.append("file", file);

    busy(true);
    status(`Envoi de « ${file.name} » en cours…`, false, 0);
    try {
      // No Content-Type header: the browser must set the multipart boundary.
      const res = await fetch("api/document-upload", { method: "POST", body: fd });
      if (res.status === 429) {
        status("Trop de requêtes — réessayez dans une minute", true, 0);
        return;
      }
      if (res.status === 413) {
        // nginx caps /cibles/ at 100 Mo and answers before FastAPI: the body is
        // not JSON, so res.json() would throw a parse error instead.
        status("Fichier trop volumineux (max 100 Mo)", true, 0);
        return;
      }
      const data = await res.json();
      if (!data.success) {
        status(data.error || "Erreur", true, 0);
        return;
      }
      // Invalidate the picker cache, otherwise the new file stays invisible in
      // "Lier" for every other row until the page is reloaded.
      fileCache[kind] = null;
      await populateDatalist(kind);
      const name = (data.path || "").split("/").pop();
      status(data.renamed ? `Ajouté sous le nom « ${name} »` : "Document ajouté", false, 6000);
      await refreshRow();
    } catch (e) {
      status(e.message, true, 0);
    } finally {
      busy(false);
      // Without this, re-picking the SAME file fires no change event.
      inputEl.value = "";
    }
  }

  async function remove(kind, path) {
    try {
      const res = await fetch(byId[kind].endpoint, {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...keyField(kind), path }),
      });
      const data = await res.json();
      if (data.success) {
        status("Délié");
        await refreshRow();
      } else status(data.error || "Erreur", true, 0);
    } catch (e) {
      status(e.message, true, 0);
    }
  }

  async function refreshRow() {
    const q = currentRow.reference || currentRow.materiau || "";
    try {
      const res = await fetch(`api/search?q=${encodeURIComponent(q)}&limit=200`);
      const data = await res.json();
      const found = (data.rows || []).find((r) => r.rowid === currentRow.rowid);
      if (found) {
        KINDS.forEach((k) => {
          currentRow[k.id + "_docs"] = found[k.id + "_docs"] || [];
        });
      }
    } catch (e) {}
    KINDS.forEach((k) => renderList(k.id));
    if (typeof search === "function") search();
  }

  async function populateDatalist(kind) {
    const files = await ensureFiles(kind);
    const dl = el(`docs-${kind}-datalist`);
    dl.innerHTML = "";
    files.forEach((f) => {
      const opt = document.createElement("option");
      opt.value = f.name;
      dl.appendChild(opt);
    });
  }

  window.DocsModal = {
    async open(row) {
      currentRow = row;
      lastTrigger = document.activeElement;
      const modal = buildModal();
      el("docs-title").textContent = row.reference || row.materiau || "";

      KINDS.forEach((k) => {
        el(`docs-${k.id}-note`).textContent = k.note(row);
        applyAvailability(k);
        renderList(k.id);
      });

      modal.classList.remove("hidden");
      document.body.style.overflow = "hidden";
      document.addEventListener("keydown", onKeydown, true);
      el("docs-close").focus();

      for (const k of KINDS) await populateDatalist(k.id);
    },
  };

  // Backwards-compatible alias.
  window.CoaModal = window.DocsModal;
})();

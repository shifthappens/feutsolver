/* Static component controller: keep CSP policy free of inline JavaScript. */
    const WF = window.WordfeudBoard;
    const app = document.getElementById("app");
    const keyboard = document.getElementById("keyboard");
    let props = {};
    let editor = null;
    let selectedSuggestion = 0;
    let activeSaveId = null;
    let activeSaveName = "";
    let saveName = "";
    let saveNameDirty = false;
    let notice = null;
    let initialized = false;
    let lastMessage = 0;
    let boardVersion = 0;
    let lastSolveToken = null;
    let autosaveTimer = null;
    let autosaveDirty = false;
    let localChangesPending = false;
    let focusVisibilityFrame = null;
    let focusVisibilityTimers = [];
    const DRAFT_STORAGE_KEY = "wordfeud-board-draft-v1";

    function storage() { try { return window.localStorage; } catch (_error) { return null; } }
    function draftStorage() { try { return window.sessionStorage; } catch (_error) { return null; } }
    function storageNamespace() { return String(props.storage_namespace || "unauthenticated"); }
    function same(left, right) { return JSON.stringify(left) === JSON.stringify(right); }
    function draftStorageKey() {
      const namespace = encodeURIComponent(storageNamespace()).slice(0, 128);
      return `${DRAFT_STORAGE_KEY}:${namespace}:${boardVersion}`;
    }
    function validSelection(selection) {
      if (!selection || typeof selection !== "object") return null;
      if (selection.kind === "board" && Number.isInteger(selection.row) && Number.isInteger(selection.col) &&
          selection.row >= 0 && selection.row < WF.SIZE && selection.col >= 0 && selection.col < WF.SIZE) {
        return { kind: "board", row: selection.row, col: selection.col, index: 0 };
      }
      if (selection.kind === "rack" && Number.isInteger(selection.index) &&
          selection.index >= 0 && selection.index < WF.MAX_RACK) {
        const caret = Number.isInteger(selection.caret) ? Math.max(0, Math.min(WF.MAX_RACK, selection.caret)) : selection.index;
        return { kind: "rack", row: 0, col: 0, index: selection.index, caret };
      }
      return null;
    }
    function clearDraft() {
      const source = draftStorage();
      if (!source) return;
      try { source.removeItem(draftStorageKey()); } catch (_error) { /* sessionStorage is best effort */ }
    }
    function persistDraft() {
      if (!editor || props.mode === "preview" || !WF.isSnapshot(editor.snapshot)) return;
      const source = draftStorage();
      if (!source) return;
      try {
        source.setItem(draftStorageKey(), JSON.stringify({
          schemaVersion: 1,
          snapshot: editor.snapshot,
          selection: editor.selection,
          direction: editor.direction,
        }));
      } catch (_error) { /* sessionStorage is best effort */ }
    }
    function restoreDraft() {
      if (props.mode === "preview") {
        clearDraft();
        return false;
      }
      const source = draftStorage();
      if (!source) return false;
      let draft;
      try {
        const raw = source.getItem(draftStorageKey());
        draft = raw ? JSON.parse(raw) : null;
      } catch (_error) {
        clearDraft();
        return false;
      }
      const selection = validSelection(draft?.selection);
      if (draft?.schemaVersion !== 1 || !WF.isSnapshot(draft?.snapshot) || !selection) {
        if (draft) clearDraft();
        return false;
      }
      editor = WF.createEditor(draft.snapshot);
      editor.selection = selection;
      editor.direction = draft.direction === "V" ? "V" : "H";
      localChangesPending = !same(editor.snapshot, props.snapshot);
      return true;
    }
    function escapeHtml(value) {
      return String(value).replace(/[&<>"']/g, character => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[character]));
    }
    function saves() { return WF.readSaves(storage(), storageNamespace()).records; }
    function setActive(id, name) {
      const result = WF.setActiveSaveId(storage(), id || null, storageNamespace());
      if (!result.ok) return result;
      activeSaveId = id || null; activeSaveName = name || "";
      saveName = name || "";
      saveNameDirty = false;
      return result;
    }
    function closeActiveSave() {
      resetAutosave();
      notice = null;
      const link = setActive(null, "");
      if (link.ok) return true;
      activeSaveId = null; activeSaveName = ""; saveName = ""; saveNameDirty = false;
      notice = { message:`De actieve spelopslag kon niet worden gesloten: ${link.error}`, kind:"error" };
      return false;
    }
    function resetAutosave() {
      if (autosaveTimer !== null) {
        window.clearTimeout(autosaveTimer);
        autosaveTimer = null;
      }
      autosaveDirty = false;
    }
    function showAutosaveSuccess(record) {
      notice = { message:`Spel “${record.name}” is automatisch opgeslagen.`, kind:"success" };
    }
    function runAutosave() {
      autosaveTimer = null;
      if (!autosaveDirty || !editor || !WF.isSnapshot(editor.snapshot)) return;
      const saved = WF.autosaveExistingSnapshot(storage(), editor.snapshot, activeSaveId, storageNamespace());
      if (saved.skipped) {
        autosaveDirty = false;
        return;
      }
      if (saved.ok) {
        autosaveDirty = false;
        activeSaveName = saved.record.name;
        saveName = saved.record.name;
        saveNameDirty = false;
        showAutosaveSuccess(saved.record);
      } else if (saved.saved) {
        autosaveDirty = false;
        notice = { message:`Automatisch opgeslagen aan “${saved.record.name}”, maar de actieve spelopslag kon niet worden opgeslagen: ${saved.error}`, kind:"error" };
      } else {
        notice = { message:`Automatisch opslaan mislukt: ${saved.error}`, kind:"error" };
      }
      render();
    }
    function scheduleAutosave() {
      if (autosaveTimer !== null) window.clearTimeout(autosaveTimer);
      autosaveTimer = null;
      if (!autosaveDirty || !activeSaveId || !editor || !WF.isSnapshot(editor.snapshot)) return;
      autosaveTimer = window.setTimeout(runAutosave, 3000);
    }
    function markAutosaveDirty() {
      if (activeSaveId) autosaveDirty = true;
      scheduleAutosave();
    }
    function restoreActive() {
      const stored = WF.readActiveSaveId(storage(), storageNamespace());
      if (!stored.ok) {
        notice = { message: stored.error, kind: "error" };
        return false;
      }
      const id = stored.id;
      const record = saves().find(item => item.id === id);
      if (!record) return false;
      activeSaveId = record.id; activeSaveName = record.name; saveName = record.name; saveNameDirty = false;
      const snapshot = WF.snapshotFromRecord(record);
      if (snapshot && !same(snapshot, props.snapshot)) {
        editor = WF.createEditor(snapshot);
        localChangesPending = false;
        props.mode = "edit";
        props.solve_result = null;
        emit("load", { snapshot, saveId: record.id });
      }
      return Boolean(snapshot);
    }
    function emit(type, payload) {
      lastMessage += 1;
      Streamlit.setComponentValue({
        id: lastMessage,
        type,
        payload: { ...(payload || {}), boardVersion },
      });
    }
    function showNotice(message, kind) { notice = message ? { message, kind: kind || "" } : null; render(); }
    function selectedFocusTarget() {
      if (editor?.selection?.kind === "board") return document.querySelector(".cell.selected");
      if (editor?.selection?.kind === "rack") return document.querySelector(".rack-slot.selected");
      return null;
    }
    function parentContext() {
      try {
        const parentWindow = window.parent && window.parent !== window ? window.parent : window;
        return {
          window: parentWindow,
          document: parentWindow.document,
          frame: window.frameElement,
          main: parentWindow.document.querySelector("section.stMain"),
        };
      } catch (_error) {
        return { window, document, frame: null, main: null };
      }
    }
    function visibleViewport(context) {
      const viewport = context.window.visualViewport;
      const top = viewport?.offsetTop ?? 0;
      const height = viewport?.height ?? context.window.innerHeight;
      return { top, bottom: top + height };
    }
    function targetRectInParent(target, frame) {
      const targetRect = target.getBoundingClientRect();
      if (!frame) return targetRect;
      const frameRect = frame.getBoundingClientRect();
      return {
        top: frameRect.top + targetRect.top,
        bottom: frameRect.top + targetRect.bottom,
        left: frameRect.left + targetRect.left,
        right: frameRect.left + targetRect.right,
      };
    }
    function scrollParentBy(context, amount) {
      let remaining = amount;
      const candidates = [context.main, context.document.scrollingElement, context.document.documentElement]
        .filter((candidate, index, all) => candidate && all.indexOf(candidate) === index);
      for (const candidate of candidates) {
        const before = candidate.scrollTop;
        candidate.scrollTop = before + remaining;
        remaining -= candidate.scrollTop - before;
        if (Math.abs(remaining) < 1) return;
      }
      if (Math.abs(remaining) >= 1 && typeof context.window.scrollBy === "function") {
        context.window.scrollBy(0, remaining);
      }
    }
    function keepFocusTargetVisible() {
      const target = selectedFocusTarget();
      if (!target) return;
      const context = parentContext();
      const viewport = visibleViewport(context);
      const rect = targetRectInParent(target, context.frame);
      const mainRect = context.main?.getBoundingClientRect();
      const top = Math.max(viewport.top + 12, (mainRect?.top ?? viewport.top) + 12);
      const bottom = Math.min(viewport.bottom - 12, (mainRect?.bottom ?? viewport.bottom) - 12);
      let amount = 0;
      if (rect.bottom > bottom) amount = rect.bottom - bottom;
      else if (rect.top < top) amount = rect.top - top;
      if (amount) scrollParentBy(context, amount);
    }
    function scheduleFocusVisibility() {
      if (focusVisibilityFrame !== null) window.cancelAnimationFrame(focusVisibilityFrame);
      focusVisibilityTimers.forEach(timer => window.clearTimeout(timer));
      focusVisibilityTimers = [];
      const check = () => {
        focusVisibilityFrame = null;
        if (document.activeElement === keyboard) keepFocusTargetVisible();
      };
      focusVisibilityFrame = window.requestAnimationFrame(check);
      [100, 300, 600].forEach(delay => {
        focusVisibilityTimers.push(window.setTimeout(check, delay));
      });
    }
    function focusKeyboard() {
      const target = selectedFocusTarget();
      if (target) {
        // Keep the real focus anchor next to the selected board/rack control.
        // Mobile browsers then use the right part of the iframe when opening
        // the keyboard instead of trying to reveal the iframe's top edge.
        const rect = target.getBoundingClientRect();
        keyboard.style.left = `${Math.max(0, rect.left + rect.width / 2)}px`;
        keyboard.style.top = `${Math.max(0, rect.top + rect.height / 2)}px`;
      }
      keyboard.focus({ preventScroll:true });
      scheduleFocusVisibility();
    }
    function handleViewportChange() {
      if (document.activeElement === keyboard) scheduleFocusVisibility();
    }
    window.visualViewport?.addEventListener("resize", handleViewportChange);
    window.visualViewport?.addEventListener("scroll", handleViewportChange);
    try {
      if (window.parent !== window && window.parent.visualViewport) {
        window.parent.visualViewport.addEventListener("resize", handleViewportChange);
        window.parent.visualViewport.addEventListener("scroll", handleViewportChange);
      }
    } catch (_error) { /* cross-origin hosts may not expose the parent viewport */ }
    function dismissKeyboard() {
      keyboard.blur();
      const activeElement = document.activeElement;
      if (activeElement && typeof activeElement.blur === "function") activeElement.blur();
    }
    function apply(action) {
      if (!editor || props.mode === "preview") return;
      const changesSnapshot = ["set_board", "clear_board", "backspace", "set_rack", "remove_rack"].includes(action.type);
      // Keep typing inside the iframe. Sending one component event per key
      // reruns Streamlit and can blur this input on mobile browsers.
      if (changesSnapshot) localChangesPending = true;
      editor = WF.reduceEditor(editor, action);
      persistDraft();
      render();
      if (changesSnapshot) {
        markAutosaveDirty();
      } else if (autosaveDirty) {
        scheduleAutosave();
      }
      focusKeyboard();
    }
    function boardLabel(row, col, cell, overlay) {
      if (overlay) return overlay.letter;
      if (cell.letter) return cell.is_blank ? cell.letter.toLowerCase() : cell.letter;
      if (row === 7 && col === 7) return "★";
      return WF.BONUSES[cell.bonus] || "";
    }
    function renderBoard(snapshot, preview) {
      const overlay = new Map();
      if (preview) preview.tiles.forEach(tile => overlay.set(`${tile.row}:${tile.col}`, tile));
      const buttons = [];
      for (let row = 0; row < WF.SIZE; row += 1) for (let col = 0; col < WF.SIZE; col += 1) {
        const cell = snapshot.grid[row][col];
        const tile = overlay.get(`${row}:${col}`);
        const selected = props.mode !== "preview" && editor?.selection.kind === "board" && editor.selection.row === row && editor.selection.col === col;
        const classes = ["cell", (tile ? "new" : cell.letter ? "existing" : (cell.bonus || "NORMAL").toLowerCase()),
          tile?.is_blank || cell.is_blank ? "blank" : "", selected ? "selected" : "", selected && editor.direction === "V" ? "selected-v" : ""].filter(Boolean).join(" ");
        buttons.push(`<button class="${classes}" data-row="${row}" data-col="${col}" aria-label="Rij ${row + 1}, kolom ${col + 1}${cell.letter ? `, ${cell.letter}` : ""}">${boardLabel(row, col, cell, tile)}</button>`);
      }
      return `<div class="board" role="grid">${buttons.join("")}</div>`;
    }
    function renderRack(snapshot) {
      const slots = [];
      for (let index = 0; index < WF.MAX_RACK; index += 1) {
        const value = snapshot.rack[index] || "";
        const selected = props.mode !== "preview" && editor?.selection.kind === "rack" && editor.selection.index === index;
        slots.push(`<button class="rack-slot ${value ? "" : "empty"} ${selected ? "selected" : ""}" data-rack="${index}" aria-label="Rekpositie ${index + 1}">${value || "leeg"}</button>`);
      }
      return `<div class="rack" aria-label="Rek">${slots.join("")}</div>`;
    }
    function formatMove(move) {
      const direction = move.direction === "H" ? "horizontaal" : "verticaal";
      const crosses = move.cross_words?.length ? ` · kruiswoorden: ${move.cross_words.join(", ")}` : "";
      const bingo = move.bingo ? " · bingo +40" : "";
      return `${direction}, start rij ${move.row + 1}, kolom ${move.col + 1}${crosses}${bingo}`;
    }
    function renderSuggestions(result) {
      if (!result?.moves?.length) return `<p class="empty">Geen legale zet gevonden in de gekozen woordenlijst.</p>`;
      return result.moves.map((move, index) => `<article class="suggestion ${index === selectedSuggestion ? "selected" : ""}">
        <button data-suggestion="${index}" aria-pressed="${index === selectedSuggestion}"><strong>${index + 1}. ${escapeHtml(move.word)} · ${escapeHtml(move.score)} punten</strong><small>${escapeHtml(formatMove(move))}</small></button></article>`).join("");
    }
    function renderSaves(editing) {
      const loaded = WF.readSaves(storage(), storageNamespace());
      const records = loaded.records;
      const active = records.find(record => record.id === activeSaveId);
      const currentName = saveNameDirty ? saveName : (active?.name || activeSaveName || saveName || "");
      if (active) activeSaveName = active.name;
      const options = records.map(record => `<option value="${escapeHtml(record.id)}" ${record.id === activeSaveId ? "selected" : ""}>${escapeHtml(record.name)}</option>`).join("");
      return `<section class="panel" aria-label="Opgeslagen spellen">
        <span class="save-label">Opgeslagen spellen${currentName ? ` · ${escapeHtml(currentName)}` : ""}</span>
        <div class="save-row"><input id="save-name" value="${escapeHtml(currentName)}" placeholder="Naam van dit spel" ${!editing ? "disabled" : ""}><button id="save-button" ${!editing ? "disabled" : ""}>${active ? "Opslaan" : "Opslaan als…"}</button></div>
        <div class="save-row"><select id="save-select" aria-label="Opgeslagen spel" ${!editing || !records.length ? "disabled" : ""}><option value="">Kies een opgeslagen spel…</option>${options}</select><button id="load-button" ${!editing || !records.length ? "disabled" : ""}>Laden</button></div>
        <div class="save-actions"><button id="rename-button" ${!editing || !active ? "disabled" : ""}>Naam wijzigen</button><button id="delete-button" class="danger" ${!editing || !active ? "disabled" : ""}>Verwijderen</button><button id="clear-local-button" class="danger" ${!editing ? "disabled" : ""}>Lokale gegevens wissen</button></div>
        ${loaded.warnings.map(warning => `<p class="hint">${escapeHtml(warning)}</p>`).join("")}
      </section>`;
    }
    function render() {
      if (!editor || !WF.isSnapshot(editor.snapshot)) return;
      const preview = props.mode === "preview" && props.solve_result?.moves?.[selectedSuggestion];
      const editing = props.mode !== "preview";
      const hasSuggestions = Boolean(props.solve_result?.moves?.length);
      const notification = notice ? `<div class="notice ${escapeHtml(notice.kind)}" role="status" aria-live="polite">${escapeHtml(notice.message)}</div>` : "";
      const solveDisabled = !editor.snapshot.rack.length || !editing;
      app.innerHTML = `<section class="panel"><div class="board-tools"><span>${editing ? "Bord bewerken" : "Voorbeeld van geselecteerde zet"}</span><span>${editing ? `Richting: ${editor.direction === "H" ? "horizontaal" : "verticaal"}` : "Bewerken vergrendeld"}</span></div>
        <div class="actions"><button id="new-button" class="danger" ${!editing ? "disabled" : ""}>Nieuw bord</button><button id="solve-button" class="primary" ${solveDisabled ? "disabled" : ""}>Geef oplossingen weer</button>${!editor.snapshot.rack.length ? '<span class="hint">Vul minstens één rekletter in om te kunnen zoeken.</span>' : ""}${!editing ? `<button id="cancel-button">Annuleren</button>${hasSuggestions ? '<button id="place-button" class="primary">Zet plaatsen</button>' : '<span class="hint">Er is geen zet om te plaatsen.</span>'}` : ""}</div>
        ${renderBoard(editor.snapshot, preview)}
        <div class="hint">Klik een vakje; klik nogmaals om de richting te wisselen. Typ A–Z. Gebruik Wissen of Terug om te wissen. Pijltjes verplaatsen de selectie.</div>
        ${renderRack(editor.snapshot)}
        ${notification}</section>
        <section><section class="panel"><h3>Suggesties</h3>${props.mode === "preview" ? renderSuggestions(props.solve_result) : '<p class="empty">Bewerk het bord en kies Geef oplossingen weer voor maximaal zes suggesties.</p>'}</section>${renderSaves(editing)}</section>`;
      bindEvents();
      Streamlit.setFrameHeight(document.body.scrollHeight + 16);
    }
    function bindEvents() {
      document.querySelectorAll("[data-row]").forEach(button => button.addEventListener("click", () => { editor = WF.selectBoard(editor, Number(button.dataset.row), Number(button.dataset.col)); persistDraft(); render(); scheduleAutosave(); focusKeyboard(); }));
      document.querySelectorAll("[data-rack]").forEach(button => button.addEventListener("click", () => { editor = WF.selectRack(editor, Number(button.dataset.rack)); persistDraft(); render(); scheduleAutosave(); focusKeyboard(); }));
      document.querySelectorAll("[data-suggestion]").forEach(button => button.addEventListener("click", () => { selectedSuggestion = Number(button.dataset.suggestion); render(); }));
      document.getElementById("new-button")?.addEventListener("click", () => {
        resetAutosave();
        clearDraft();
        localChangesPending = false;
        const link = setActive(null, "");
        if (!link.ok) return showNotice(`Nieuw bord kon niet worden geopend omdat de actieve spelopslag niet kon worden gewist: ${link.error}`, "error");
        emit("new_board");
      });
      document.getElementById("solve-button")?.addEventListener("click", () => {
        localChangesPending = false;
        emit("solve_request", { snapshot: editor.snapshot, clientRevision: editor.revision });
      });
      document.getElementById("cancel-button")?.addEventListener("click", () => { notice = null; emit("cancel"); });
      document.getElementById("place-button")?.addEventListener("click", () => {
        const move = props.solve_result?.moves?.[selectedSuggestion];
        if (move) {
          localChangesPending = false;
          emit("place_request", { solveToken: props.solve_result.token, selectedMove: move, stateHash: props.solve_result.state_hash });
        }
      });
      const nameInput = document.getElementById("save-name");
      nameInput?.addEventListener("input", event => { saveName = event.target.value; saveNameDirty = true; });
      document.getElementById("save-button")?.addEventListener("click", () => {
        const name = (nameInput?.value || saveName || "").trim();
        let result = WF.saveSnapshot(storage(), name, editor.snapshot, activeSaveId, storageNamespace());
        if (!result.ok && result.duplicateId && !activeSaveId) {
          if (!window.confirm(`Er bestaat al een opgeslagen spel met de naam “${result.duplicateName}”. Wil je dit spel overschrijven?`)) {
            return showNotice("Opslaan geannuleerd; het bestaande spel is niet gewijzigd.", "");
          }
          result = WF.saveSnapshot(storage(), name, editor.snapshot, activeSaveId, storageNamespace(), result.duplicateId);
        }
        if (!result.ok) return showNotice(result.error, "error");
        resetAutosave();
        const link = setActive(result.record.id, result.record.name);
        if (!link.ok) return showNotice(`Opgeslagen als “${result.record.name}”, maar de actieve spelopslag kon niet worden opgeslagen: ${link.error}`, "error");
        showNotice(`Opgeslagen als “${result.record.name}”.`, "success");
      });
      document.getElementById("load-button")?.addEventListener("click", () => {
        const id = document.getElementById("save-select")?.value;
        const record = saves().find(item => item.id === id);
        const snapshot = record && WF.snapshotFromRecord(record);
        if (!snapshot) return showNotice("Dit opgeslagen spel is ongeldig of niet meer beschikbaar.", "error");
        resetAutosave();
        clearDraft();
        localChangesPending = false;
        const link = setActive(record.id, record.name);
        if (!link.ok) return showNotice(`Opgeslagen spel kon niet worden geladen omdat de actieve spelopslag niet kon worden opgeslagen: ${link.error}`, "error");
        editor = WF.createEditor(snapshot);
        render(); emit("load", { snapshot, saveId: record.id });
      });
      document.getElementById("rename-button")?.addEventListener("click", () => {
        const name = (nameInput?.value || "").trim();
        const result = WF.saveSnapshot(storage(), name, editor.snapshot, activeSaveId, storageNamespace());
        if (!result.ok) return showNotice(result.error, "error");
        resetAutosave();
        const link = setActive(result.record.id, result.record.name);
        if (!link.ok) return showNotice(`Naam gewijzigd naar “${result.record.name}”, maar de actieve spelopslag kon niet worden opgeslagen: ${link.error}`, "error");
        showNotice(`Naam gewijzigd naar “${result.record.name}”.`, "success");
      });
      document.getElementById("delete-button")?.addEventListener("click", () => {
        if (!activeSaveId || !window.confirm(`Opgeslagen spel “${activeSaveName}” verwijderen?`)) return;
        resetAutosave();
        const result = WF.deleteSave(storage(), activeSaveId, storageNamespace());
        if (!result.ok) return showNotice(result.error, "error");
        const link = setActive(null, "");
        if (!link.ok) {
          activeSaveId = null; activeSaveName = ""; saveName = ""; saveNameDirty = false;
          return showNotice(`Opgeslagen spel verwijderd, maar de actieve spelopslag kon niet worden gewist: ${link.error}`, "error");
        }
        showNotice("Opgeslagen spel verwijderd; het huidige bord blijft open als onopgeslagen bord.", "success");
      });
      document.getElementById("clear-local-button")?.addEventListener("click", () => {
        if (!window.confirm("Alle lokaal opgeslagen spellen en voorkeuren op dit apparaat wissen?")) return;
        resetAutosave();
        clearDraft();
        const cleared = WF.clearSaves(storage(), storageNamespace());
        if (!cleared.ok) return showNotice(cleared.error, "error");
        activeSaveId = null; activeSaveName = ""; saveName = ""; saveNameDirty = false;
        showNotice("Lokale gegevens gewist.", "success");
      });
    }
    keyboard.addEventListener("keydown", event => {
      if (!editor || props.mode === "preview") return;
      if (["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"].includes(event.key)) { event.preventDefault(); apply({ type:"arrow", key:event.key }); return; }
      if (editor.selection.kind === "board" && /^[a-zA-Z]$/.test(event.key)) { event.preventDefault(); apply({ type:"set_board", letter:event.key.toUpperCase() }); return; }
      if (editor.selection.kind === "rack" && (/^[a-zA-Z]$/.test(event.key) || event.key === "?")) { event.preventDefault(); apply({ type:"set_rack", tile:event.key === "?" ? "?" : event.key.toUpperCase() }); return; }
      if (event.key === "Delete") { event.preventDefault(); apply(editor.selection.kind === "board" ? { type:"clear_board" } : { type:"remove_rack" }); return; }
      if (event.key === "Backspace") { event.preventDefault(); apply({ type:"backspace" }); }
    });
      function dismissKeyboardForAction(event) {
        const target = event.target instanceof Element ? event.target : null;
        const button = target?.closest("button");
        if (!button || button.matches("[data-row], [data-rack]")) return;
        dismissKeyboard();
      }
      // Only board and rack buttons activate the hidden typing input. Action
      // buttons (including save/load) must not open the mobile keyboard.
      document.addEventListener("pointerdown", dismissKeyboardForAction, true);
      document.addEventListener("click", dismissKeyboardForAction);
      Streamlit.events.addEventListener(Streamlit.RENDER_EVENT, event => {
        props = event.detail.args || {};
        const incomingBoardVersion = Number(props.board_version);
        const boardWasReplaced = Number.isFinite(incomingBoardVersion) && incomingBoardVersion !== boardVersion;
        if (Number.isFinite(incomingBoardVersion)) boardVersion = incomingBoardVersion;
        if (boardWasReplaced) {
          clearDraft();
          closeActiveSave();
          localChangesPending = false;
        }
        let restoredActive = false;
        const firstRender = !initialized;
        if (firstRender) { restoredActive = restoreActive(); initialized = true; restoreDraft(); }
        if (props.mode === "preview") clearDraft();
        const incomingSnapshotChanged = Boolean(editor) && !same(editor.snapshot, props.snapshot);
        if (!editor || (!restoredActive && incomingSnapshotChanged && !localChangesPending)) {
          if (incomingSnapshotChanged && !localChangesPending) clearDraft();
          editor = WF.createEditor(props.snapshot);
          localChangesPending = false;
          if (incomingSnapshotChanged && !restoredActive && !boardWasReplaced) markAutosaveDirty();
        }
        const solveToken = props.mode === "preview" ? (props.solve_result?.token || null) : null;
        if (props.mode === "preview") selectedSuggestion = WF.suggestionSelection(lastSolveToken, solveToken, selectedSuggestion, props.solve_result?.moves?.length || 0);
        lastSolveToken = solveToken;
        if (props.response) {
        const response = props.response;
        if (response.kind === "place_result" && response.ok) {
          editor = WF.createEditor(response.snapshot); props.mode = "edit";
          localChangesPending = false;
          resetAutosave();
          clearDraft();
          if (activeSaveId) {
            const saved = WF.autosaveSnapshot(storage(), activeSaveName, response.snapshot, activeSaveId, storageNamespace());
            if (saved.ok) showAutosaveSuccess(saved.record);
            else if (saved.saved) notice = { message:`Automatisch opgeslagen aan “${saved.record.name}”, maar de actieve spelopslag kon niet worden opgeslagen: ${saved.error}`, kind:"error" };
            else notice = { message:`Plaatsing gelukt, maar automatisch opslaan mislukte: ${saved.error}`, kind:"error" };
          } else notice = { message:"Zet geplaatst.", kind:"success" };
        } else if (response.kind === "place_result") { props.mode = "edit"; localChangesPending = false; notice = { message:response.error || "De zet is verouderd en kon niet worden geplaatst.", kind:"error" }; }
        else if (response.kind === "solve_error") { props.mode = "edit"; localChangesPending = false; notice = { message:response.error, kind:"error" }; }
      }
      if (props.mode === "preview" && props.solve_result && selectedSuggestion >= props.solve_result.moves.length) selectedSuggestion = 0;
      render();
    });
    Streamlit.setComponentReady();
    Streamlit.setFrameHeight(500);

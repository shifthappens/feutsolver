/* Pure browser state and storage helpers. Loaded by the Streamlit component and
 * required directly by the Node test suite. */
(function (root, factory) {
  const api = factory();
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.WordfeudBoard = api;
})(typeof window !== "undefined" ? window : globalThis, function () {
  const SIZE = 15;
  const MAX_RACK = 7;
  const SCHEMA_VERSION = 1;
  const STORAGE_KEY = "wordfeud-analyzer:saved-games:v1";
  const ACTIVE_STORAGE_KEY = "wordfeud-analyzer:active-save:v1";
  const BONUSES = { NORMAL: "", DL: "2L", TL: "3L", DW: "2W", TW: "3W" };

  function clone(value) {
    return JSON.parse(JSON.stringify(value));
  }

  function effectiveBonus(snapshot, row, col) {
    return snapshot.effective_bonuses?.[row]?.[col] || snapshot.grid[row][col].bonus || "NORMAL";
  }

  function isLetter(value) {
    return typeof value === "string" && /^[A-Z]$/.test(value);
  }

  function isSnapshot(value) {
    return Boolean(value && Array.isArray(value.grid) && value.grid.length === SIZE &&
      value.grid.every(row => Array.isArray(row) && row.length === SIZE && row.every(cell =>
        cell && (cell.letter === null || isLetter(cell.letter)) &&
        typeof cell.is_blank === "boolean" && (!cell.is_blank || cell.letter !== null) &&
        BONUSES[cell.bonus] !== undefined)) &&
      Array.isArray(value.rack) && value.rack.length <= MAX_RACK &&
      value.rack.every(tile => tile === "?" || isLetter(tile)) &&
      (value.effective_bonuses === undefined ||
        (Array.isArray(value.effective_bonuses) && value.effective_bonuses.length === SIZE &&
          value.effective_bonuses.every(row => Array.isArray(row) && row.length === SIZE && row.every(b => BONUSES[b] !== undefined)))));
  }

  function emptyCell(snapshot, row, col) {
    return { letter: null, is_blank: false, bonus: effectiveBonus(snapshot, row, col) };
  }

  function createEditor(snapshot) {
    return {
      snapshot: clone(snapshot),
      selection: { kind: "board", row: 7, col: 7, index: 0 },
      direction: "H",
      revision: 0,
    };
  }

  function setBoardCell(editor, row, col, letter) {
    if (!editor || !isSnapshot(editor.snapshot) || row < 0 || row >= SIZE || col < 0 || col >= SIZE) return editor;
    const next = clone(editor);
    next.snapshot.grid[row][col] = {
      ...next.snapshot.grid[row][col],
      letter: letter || null,
      is_blank: false,
      bonus: letter ? "NORMAL" : effectiveBonus(next.snapshot, row, col),
    };
    next.revision += 1;
    return next;
  }

  function clearBoardCell(editor, row, col) {
    return setBoardCell(editor, row, col, null);
  }

  function setRackTile(editor, index, tile) {
    if (!editor || !isSnapshot(editor.snapshot) || index < 0 || index >= MAX_RACK ||
        !(tile === "?" || isLetter(tile))) return editor;
    const next = clone(editor);
    const target = Math.min(index, next.snapshot.rack.length);
    if (target === next.snapshot.rack.length) next.snapshot.rack.push(tile);
    else next.snapshot.rack[target] = tile;
    next.revision += 1;
    return next;
  }

  function removeRackTile(editor, index) {
    if (!editor || !isSnapshot(editor.snapshot) || index < 0 || index >= editor.snapshot.rack.length) return editor;
    const next = clone(editor);
    next.snapshot.rack.splice(index, 1);
    next.selection.index = Math.max(0, Math.min(index, next.snapshot.rack.length));
    if (next.selection.kind === "rack") next.selection.caret = next.selection.index;
    next.revision += 1;
    return next;
  }

  function advanceRackSelection(editor, index) {
    if (!editor || editor.selection.kind !== "rack") return editor;
    const next = clone(editor);
    next.selection.index = Math.min(index + 1, MAX_RACK - 1);
    next.selection.caret = Math.min(index + 1, MAX_RACK);
    return next;
  }

  function selectBoard(editor, row, col) {
    if (!editor || row < 0 || row >= SIZE || col < 0 || col >= SIZE) return editor;
    const next = clone(editor);
    if (next.selection.kind === "board" && next.selection.row === row && next.selection.col === col) {
      next.direction = next.direction === "H" ? "V" : "H";
    } else {
      next.selection = { kind: "board", row, col, index: 0 };
      next.direction = "H";
    }
    return next;
  }

  function selectRack(editor, index) {
    if (!editor || index < 0 || index >= MAX_RACK) return editor;
    const next = clone(editor);
    next.selection = { kind: "rack", row: 0, col: 0, index, caret: index };
    return next;
  }

  function suggestionSelection(previousToken, nextToken, selected, count) {
    if (previousToken !== nextToken || selected < 0 || selected >= count) return 0;
    return selected;
  }

  function moveSelection(editor, dr, dc) {
    if (!editor || editor.selection.kind !== "board") return editor;
    const next = clone(editor);
    next.selection.row = Math.max(0, Math.min(SIZE - 1, next.selection.row + dr));
    next.selection.col = Math.max(0, Math.min(SIZE - 1, next.selection.col + dc));
    return next;
  }

  function previousSelection(editor) {
    return editor.direction === "H" ? moveSelection(editor, 0, -1) : moveSelection(editor, -1, 0);
  }

  function advanceSelection(editor) {
    return editor.direction === "H" ? moveSelection(editor, 0, 1) : moveSelection(editor, 1, 0);
  }

  function reduceEditor(editor, action) {
    if (!editor || !action || editor.mode === "preview") return editor;
    if (action.type === "select_board") return selectBoard(editor, action.row, action.col);
    if (action.type === "select_rack") return selectRack(editor, action.index);
    if (action.type === "set_board") {
      const placed = setBoardCell(editor, editor.selection.row, editor.selection.col, action.letter);
      return advanceSelection(placed);
    }
    if (action.type === "clear_board") return clearBoardCell(editor, editor.selection.row, editor.selection.col);
    if (action.type === "set_rack") {
      const previousLength = editor.snapshot.rack.length;
      const placed = setRackTile(editor, editor.selection.index, action.tile);
      if (placed === editor) return editor;
      const placedIndex = Math.min(editor.selection.index, previousLength);
      return advanceRackSelection(placed, placedIndex);
    }
    if (action.type === "remove_rack") return removeRackTile(editor, editor.selection.index);
    if (action.type === "backspace") {
      if (editor.selection.kind === "rack") {
        const cursor = Math.min(editor.selection.caret ?? editor.selection.index, editor.snapshot.rack.length);
        return cursor > 0 ? removeRackTile(editor, cursor - 1) : editor;
      }
      const current = editor.snapshot.grid[editor.selection.row][editor.selection.col];
      if (current.letter) return previousSelection(clearBoardCell(editor, editor.selection.row, editor.selection.col));
      const previous = previousSelection(editor);
      return clearBoardCell(previous, previous.selection.row, previous.selection.col);
    }
    if (action.type === "arrow") {
      const deltas = { ArrowUp: [-1, 0], ArrowDown: [1, 0], ArrowLeft: [0, -1], ArrowRight: [0, 1] };
      const delta = deltas[action.key];
      return delta ? moveSelection(editor, delta[0], delta[1]) : editor;
    }
    return editor;
  }

  function gridPlacement(snapshot) {
    const placement = [];
    const blankFlags = [];
    snapshot.grid.forEach((row, r) => row.forEach((cell, c) => {
      if (cell.letter) {
        placement.push({ row: r, col: c, letter: cell.letter });
        if (cell.is_blank) blankFlags.push({ row: r, col: c });
      }
    }));
    return { placement, blankFlags };
  }

  function makeId() {
    if (typeof crypto !== "undefined" && crypto.randomUUID) return crypto.randomUUID();
    return `save-${Date.now()}-${Math.random().toString(36).slice(2)}`;
  }

  function recordFromSnapshot(name, snapshot, previous) {
    const now = new Date().toISOString();
    const { placement, blankFlags } = gridPlacement(snapshot);
    return {
      schemaVersion: SCHEMA_VERSION,
      id: previous?.id || makeId(),
      name: name.trim(),
      createdAt: previous?.createdAt || now,
      updatedAt: now,
      grid: clone(snapshot.grid),
      effectiveBonuses: clone(snapshot.effective_bonuses || snapshot.grid.map(row => row.map(cell => cell.bonus))),
      tilePlacement: placement,
      blankFlags,
      rack: [...snapshot.rack],
    };
  }

  function snapshotFromRecord(record) {
    if (!isValidRecord(record)) return null;
    const snapshot = {
      grid: clone(record.grid),
      rack: [...record.rack],
      effective_bonuses: clone(record.effectiveBonuses),
    };
    // The redundant placement/blank fields are intentional schema guards: if a
    // record was partially corrupted, it must not silently load another board.
    const { placement, blankFlags } = gridPlacement(snapshot);
    if (JSON.stringify(placement) !== JSON.stringify(record.tilePlacement) ||
        JSON.stringify(blankFlags) !== JSON.stringify(record.blankFlags)) return null;
    return snapshot;
  }

  function isValidRecord(record) {
    return Boolean(record && record.schemaVersion === SCHEMA_VERSION && typeof record.id === "string" &&
      typeof record.name === "string" && record.name.trim() && typeof record.createdAt === "string" &&
      typeof record.updatedAt === "string" && isSnapshot({ grid: record.grid, rack: record.rack }) &&
      Array.isArray(record.effectiveBonuses) && record.effectiveBonuses.length === SIZE &&
      record.effectiveBonuses.every(row => Array.isArray(row) && row.length === SIZE && row.every(b => BONUSES[b] !== undefined)) &&
      Array.isArray(record.tilePlacement) && Array.isArray(record.blankFlags));
  }

  function readSaves(storage) {
    const source = storage || (typeof localStorage !== "undefined" ? localStorage : null);
    if (!source) {
      const error = "localStorage is niet beschikbaar.";
      return { records: [], warnings: [error], ok: false, error };
    }
    try {
      const raw = source.getItem(STORAGE_KEY);
      if (!raw) return { records: [], warnings: [], ok: true };
      const envelope = JSON.parse(raw);
      if (!envelope || envelope.schemaVersion !== SCHEMA_VERSION || !Array.isArray(envelope.records)) {
        const error = "Opgeslagen spellen hebben een onbekend of ongeldig formaat.";
        return { records: [], warnings: [error], ok: false, error };
      }
      const candidates = envelope.records;
      const records = [];
      let skipped = 0;
      for (const record of candidates) {
        if (isValidRecord(record) && snapshotFromRecord(record)) records.push(record);
        else skipped += 1;
      }
      return {
        records,
        warnings: skipped ? [`${skipped} corrupte of niet-ondersteunde opgeslagen spellen overgeslagen.`] : [],
        ok: true,
      };
    } catch (_error) {
      const error = "Opgeslagen spellen konden niet veilig worden gelezen.";
      return { records: [], warnings: [error], ok: false, error };
    }
  }

  function writeSaves(storage, records) {
    const source = storage || (typeof localStorage !== "undefined" ? localStorage : null);
    if (!source) return { ok: false, error: "localStorage is niet beschikbaar." };
    try {
      source.setItem(STORAGE_KEY, JSON.stringify({ schemaVersion: SCHEMA_VERSION, records }));
      return { ok: true };
    } catch (_error) {
      return { ok: false, error: "De lokale opslag is niet beschikbaar of vol." };
    }
  }

  function readActiveSaveId(storage) {
    const source = storage || (typeof localStorage !== "undefined" ? localStorage : null);
    if (!source) return { ok: false, id: null, error: "localStorage is niet beschikbaar." };
    try {
      return { ok: true, id: source.getItem(ACTIVE_STORAGE_KEY) };
    } catch (_error) {
      return { ok: false, id: null, error: "De actieve spelopslag kon niet worden gelezen." };
    }
  }

  function setActiveSaveId(storage, id) {
    const source = storage || (typeof localStorage !== "undefined" ? localStorage : null);
    if (!source) return { ok: false, error: "localStorage is niet beschikbaar." };
    try {
      if (id) source.setItem(ACTIVE_STORAGE_KEY, id);
      else source.removeItem(ACTIVE_STORAGE_KEY);
      return { ok: true };
    } catch (_error) {
      return { ok: false, error: "De koppeling met de actieve spelopslag kon niet worden opgeslagen." };
    }
  }

  function saveSnapshot(storage, name, snapshot, activeId) {
    const cleanName = String(name || "").trim();
    if (!cleanName) return { ok: false, error: "Geef het spel een naam." };
    const loaded = readSaves(storage);
    if (!loaded.ok) return { ok: false, error: loaded.error || "Opgeslagen spellen konden niet veilig worden gewijzigd." };
    const duplicate = loaded.records.find(record => record.name.toLocaleLowerCase() === cleanName.toLocaleLowerCase() && record.id !== activeId);
    if (duplicate) return { ok: false, error: "Die naam bestaat al." };
    const previous = loaded.records.find(record => record.id === activeId);
    const record = recordFromSnapshot(cleanName, snapshot, previous);
    const records = previous ? loaded.records.map(item => item.id === previous.id ? record : item) : [...loaded.records, record];
    const written = writeSaves(storage, records);
    return written.ok ? { ok: true, record } : written;
  }

  function deleteSave(storage, id) {
    const loaded = readSaves(storage);
    if (!loaded.ok) return { ok: false, error: loaded.error || "Opgeslagen spellen konden niet veilig worden gewijzigd." };
    const records = loaded.records.filter(record => record.id !== id);
    if (records.length === loaded.records.length) return { ok: false, error: "Opgeslagen spel niet gevonden." };
    return writeSaves(storage, records);
  }

  function autosaveSnapshot(storage, name, snapshot, activeId) {
    const saved = saveSnapshot(storage, name, snapshot, activeId);
    if (!saved.ok) return { ...saved, saved:false };
    const linked = setActiveSaveId(storage, saved.record.id);
    if (!linked.ok) return { ok:false, saved:true, record:saved.record, error:linked.error };
    return { ok:true, saved:true, record:saved.record };
  }

  function autosaveExistingSnapshot(storage, snapshot, activeId) {
    if (!activeId) return { ok:true, saved:false, skipped:true };
    const loaded = readSaves(storage);
    if (!loaded.ok) return { ok:false, saved:false, error:loaded.error || "Opgeslagen spellen konden niet veilig worden gelezen." };
    const active = loaded.records.find(record => record.id === activeId);
    if (!active) return { ok:true, saved:false, skipped:true };
    return autosaveSnapshot(storage, active.name, snapshot, activeId);
  }

  return {
    SIZE, MAX_RACK, SCHEMA_VERSION, STORAGE_KEY, ACTIVE_STORAGE_KEY, BONUSES,
    clone, effectiveBonus, isSnapshot, createEditor, setBoardCell, clearBoardCell,
    setRackTile, removeRackTile, selectBoard, selectRack, suggestionSelection, moveSelection, reduceEditor,
    gridPlacement, recordFromSnapshot, snapshotFromRecord, isValidRecord, readSaves,
    writeSaves, readActiveSaveId, setActiveSaveId, saveSnapshot, deleteSave, autosaveSnapshot,
    autosaveExistingSnapshot,
  };
});

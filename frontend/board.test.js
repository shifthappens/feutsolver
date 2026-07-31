const test = require("node:test");
const assert = require("node:assert/strict");
const WF = require("./board.js");

function snapshot(rack = []) {
  const grid = Array.from({ length: 15 }, () => Array.from({ length: 15 }, () => ({ letter:null, bonus:"NORMAL", is_blank:false })));
  return { grid, rack };
}

function memoryStorage(initial) {
  const data = new Map(Object.entries(initial || {}));
  return { getItem: key => data.get(key) || null, setItem: (key, value) => data.set(key, value), removeItem: key => data.delete(key) };
}

test("board selection toggles direction, overwrites, deletes, and backspaces", () => {
  let editor = WF.createEditor(snapshot());
  editor = WF.reduceEditor(editor, { type:"select_board", row:3, col:4 });
  assert.equal(editor.direction, "H");
  editor = WF.reduceEditor(editor, { type:"select_board", row:3, col:4 });
  assert.equal(editor.direction, "V");
  editor = WF.reduceEditor(editor, { type:"set_board", letter:"A" });
  editor = WF.reduceEditor(editor, { type:"set_board", letter:"B" });
  assert.equal(editor.snapshot.grid[3][4].letter, "A");
  assert.equal(editor.snapshot.grid[4][4].letter, "B");
  editor = WF.reduceEditor(editor, { type:"backspace" });
  assert.equal(editor.snapshot.grid[4][4].letter, null);
  assert.deepEqual(editor.selection, { kind:"board", row:4, col:4, index:0 });
});

test("rack accepts blanks, compacts after deletion, and rejects gaps", () => {
  let editor = WF.createEditor(snapshot());
  editor = WF.reduceEditor(editor, { type:"select_rack", index:5 });
  editor = WF.reduceEditor(editor, { type:"set_rack", tile:"?" });
  assert.deepEqual(editor.snapshot.rack, ["?"]);
  assert.equal(editor.selection.index, 1);
  editor = WF.reduceEditor(editor, { type:"select_rack", index:0 });
  editor = WF.reduceEditor(editor, { type:"set_rack", tile:"A" });
  assert.deepEqual(editor.snapshot.rack, ["A"]);
  assert.equal(editor.selection.index, 1);
  editor = WF.reduceEditor(editor, { type:"select_rack", index:0 });
  editor = WF.reduceEditor(editor, { type:"remove_rack" });
  assert.deepEqual(editor.snapshot.rack, []);
});

test("rack typing advances through the slots and stops at the last slot", () => {
  let editor = WF.createEditor(snapshot());
  editor = WF.reduceEditor(editor, { type:"select_rack", index:0 });

  for (const tile of ["A", "B", "C", "D", "E", "F", "G"]) {
    editor = WF.reduceEditor(editor, { type:"set_rack", tile });
  }

  assert.deepEqual(editor.snapshot.rack, ["A", "B", "C", "D", "E", "F", "G"]);
  assert.equal(editor.selection.index, 6);
});

test("preview mode locks reducer edits", () => {
  const editor = { ...WF.createEditor(snapshot(["A"])), mode:"preview" };
  const next = WF.reduceEditor(editor, { type:"set_board", letter:"Z" });
  assert.deepEqual(next, editor);
});

test("a new solve token resets the selected suggestion", () => {
  assert.equal(WF.suggestionSelection("solve-1", "solve-2", 4, 6), 0);
  assert.equal(WF.suggestionSelection("solve-1", "solve-1", 4, 6), 4);
  assert.equal(WF.suggestionSelection("solve-1", "solve-1", 6, 6), 0);
});

test("save CRUD enforces unique names and skips corrupt records", () => {
  const storage = memoryStorage();
  const first = WF.saveSnapshot(storage, "Eerste", snapshot(["A"]));
  assert.equal(first.ok, true);
  assert.equal(WF.saveSnapshot(storage, "eerste", snapshot(), null).ok, false);
  const updated = WF.saveSnapshot(storage, "Eerste", snapshot(["?"]), first.record.id);
  assert.equal(updated.ok, true);
  const raw = JSON.parse(storage.getItem(WF.STORAGE_KEY));
  raw.records.push({ schemaVersion:99});
  storage.setItem(WF.STORAGE_KEY, JSON.stringify(raw));
  const loaded = WF.readSaves(storage);
  assert.equal(loaded.records.length, 1);
  assert.equal(loaded.warnings.length, 1);
  assert.deepEqual(WF.snapshotFromRecord(loaded.records[0]).rack, ["?"]);
  assert.equal(WF.deleteSave(storage, first.record.id).ok, true);
});

test("save reload restores the complete snapshot and rejects malformed storage safely", () => {
  const storage = memoryStorage();
  const source = snapshot(["?", "A"]);
  source.grid[0][0] = { letter:"B", bonus:"NORMAL", is_blank:true };
  const saved = WF.saveSnapshot(storage, "Volledig", source);
  assert.equal(saved.ok, true);
  const loaded = WF.readSaves(storage);
  assert.equal(loaded.ok, true);
  const restored = WF.snapshotFromRecord(loaded.records[0]);
  assert.deepEqual(restored.grid, source.grid);
  assert.deepEqual(restored.rack, source.rack);
  assert.equal(restored.effective_bonuses[0][0], "NORMAL");

  const raw = storage.getItem(WF.STORAGE_KEY);
  storage.setItem(WF.STORAGE_KEY, "{" );
  const failed = WF.saveSnapshot(storage, "Nieuwe naam", snapshot());
  assert.equal(failed.ok, false);
  assert.equal(storage.getItem(WF.STORAGE_KEY), "{");
  storage.setItem(WF.STORAGE_KEY, raw);
  assert.equal(WF.readSaves(storage).records.length, 1);
});

test("a storage read failure never overwrites the existing save set", () => {
  const base = memoryStorage();
  assert.equal(WF.saveSnapshot(base, "Bestaand", snapshot(["A"])).ok, true);
  const before = base.getItem(WF.STORAGE_KEY);
  let readsFail = true;
  const unstable = {
    getItem: key => { if (readsFail) throw new Error("temporary read failure"); return base.getItem(key); },
    setItem: (key, value) => base.setItem(key, value),
    removeItem: key => base.removeItem(key),
  };
  assert.equal(WF.saveSnapshot(unstable, "Nieuw", snapshot()).ok, false);
  assert.equal(base.getItem(WF.STORAGE_KEY), before);
  readsFail = false;
  assert.equal(WF.saveSnapshot(unstable, "Nieuw", snapshot()).ok, true);
  assert.equal(WF.readSaves(base).records.length, 2);
});

test("active save linkage reports storage failures without changing the stored id", () => {
  const storage = memoryStorage();
  assert.deepEqual(WF.setActiveSaveId(storage, "save-1"), { ok:true });
  assert.deepEqual(WF.readActiveSaveId(storage), { ok:true, id:"save-1" });

  const failing = {
    getItem: () => { throw new Error("read failure"); },
    setItem: () => { throw new Error("write failure"); },
    removeItem: () => { throw new Error("remove failure"); },
  };
  assert.equal(WF.setActiveSaveId(failing, null).ok, false);
  assert.equal(WF.readActiveSaveId(storage).id, "save-1");
  assert.equal(WF.readActiveSaveId(failing).ok, false);
});

test("linked autosave distinguishes a saved board from a failed active-link update", () => {
  const storage = memoryStorage();
  const first = WF.saveSnapshot(storage, "Partij", snapshot(["A"]));
  assert.equal(first.ok, true);

  const linkFails = {
    getItem: key => storage.getItem(key),
    setItem: (key, value) => {
      if (key === WF.ACTIVE_STORAGE_KEY) throw new Error("quota");
      storage.setItem(key, value);
    },
    removeItem: key => storage.removeItem(key),
  };
  const failedLink = WF.autosaveSnapshot(linkFails, "Partij", snapshot(["?"]), first.record.id);
  assert.equal(failedLink.ok, false);
  assert.equal(failedLink.saved, true);
  assert.deepEqual(WF.snapshotFromRecord(WF.readSaves(storage).records[0]).rack, ["?"]);

  const linked = WF.autosaveSnapshot(storage, "Partij", snapshot(["A"]), first.record.id);
  assert.equal(linked.ok, true);
  assert.equal(WF.readActiveSaveId(storage).id, first.record.id);
});

test("autosave of an existing game does nothing without an active save", () => {
  const storage = memoryStorage();
  const result = WF.autosaveExistingSnapshot(storage, snapshot(["A"]), null);
  assert.deepEqual(result, { ok:true, saved:false, skipped:true });
  assert.equal(storage.getItem(WF.STORAGE_KEY), null);
});

test("autosave of an existing game updates only the linked save", () => {
  const storage = memoryStorage();
  const first = WF.saveSnapshot(storage, "Partij", snapshot(["A"]));
  const other = WF.saveSnapshot(storage, "Andere", snapshot(["B"]));
  const result = WF.autosaveExistingSnapshot(storage, snapshot(["?"]), first.record.id);
  assert.equal(result.ok, true);
  assert.deepEqual(WF.snapshotFromRecord(WF.readSaves(storage).records.find(record => record.id === first.record.id)).rack, ["?"]);
  assert.deepEqual(WF.snapshotFromRecord(WF.readSaves(storage).records.find(record => record.id === other.record.id)).rack, ["B"]);
});

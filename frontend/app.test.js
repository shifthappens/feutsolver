const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const WF = require("./board.js");

function snapshot(rack = []) {
  const grid = Array.from({ length: 15 }, () => Array.from({ length: 15 }, () => ({
    letter: null,
    bonus: "NORMAL",
    is_blank: false,
  })));
  return { grid, rack };
}

function memoryStorage() {
  const values = new Map();
  return {
    getItem: key => values.get(key) || null,
    setItem: (key, value) => values.set(key, value),
    removeItem: key => values.delete(key),
  };
}

class FakeElement {
  constructor(document, tagName, attributes = {}) {
    this.ownerDocument = document;
    this.tagName = tagName.toUpperCase();
    this.attributes = attributes;
    this.dataset = {};
    this.id = attributes.id || "";
    this.className = attributes.class || "";
    this.style = {};
    this.listeners = new Map();
    for (const [key, value] of Object.entries(attributes)) {
      if (key.startsWith("data-")) {
        const dataKey = key.slice(5).replace(/-([a-z])/g, (_match, character) => character.toUpperCase());
        this.dataset[dataKey] = value;
      }
    }
  }

  addEventListener(type, handler) {
    const handlers = this.listeners.get(type) || [];
    handlers.push(handler);
    this.listeners.set(type, handlers);
  }

  dispatchEvent(event) {
    event.target ||= this;
    for (const handler of this.listeners.get(event.type) || []) handler(event);
    return true;
  }

  click() { this.dispatchEvent({ type: "click", target: this }); }

  focus() { this.ownerDocument.activeElement = this; }

  blur() {
    if (this.ownerDocument.activeElement === this) this.ownerDocument.activeElement = this.ownerDocument.body;
  }

  getBoundingClientRect() {
    return { top: 0, bottom: 20, left: 0, right: 20, width: 20, height: 20 };
  }

  closest(selector) { return selector === "button" && this.tagName === "BUTTON" ? this : null; }

  matches(selector) {
    if (selector === "[data-row], [data-rack]") return "row" in this.dataset || "rack" in this.dataset;
    return false;
  }
}

class FakeApp extends FakeElement {
  set innerHTML(value) {
    this.html = value;
    this.children = [];
    const elements = /<(button|input|select)\b([^>]*)>/g;
    for (const match of value.matchAll(elements)) {
      const attributes = {};
      for (const attribute of match[2].matchAll(/([\w-]+)="([^"]*)"/g)) attributes[attribute[1]] = attribute[2];
      this.children.push(new FakeElement(this.ownerDocument, match[1], attributes));
    }
  }

  querySelectorAll(selector) { return this.ownerDocument.querySelectorAll(selector); }
}

class FakeDocument {
  constructor() {
    this.body = { scrollHeight: 500 };
    this.activeElement = this.body;
    this.listeners = new Map();
    this.keyboard = new FakeElement(this, "input", { id: "keyboard" });
    this.app = new FakeApp(this, "main", { id: "app" });
  }

  addEventListener(type, handler) {
    const handlers = this.listeners.get(type) || [];
    handlers.push(handler);
    this.listeners.set(type, handlers);
  }

  getElementById(id) {
    if (id === "keyboard") return this.keyboard;
    if (id === "app") return this.app;
    return this.app.children.find(child => child.id === id) || null;
  }

  querySelectorAll(selector) {
    const children = this.app.children || [];
    if (selector === "[data-row]") return children.filter(child => "row" in child.dataset);
    if (selector === "[data-rack]") return children.filter(child => "rack" in child.dataset);
    if (selector === "[data-suggestion]") return children.filter(child => "suggestion" in child.dataset);
    if (selector === "[data-purge-suggestion]") return children.filter(child => "purgeSuggestion" in child.dataset);
    return [];
  }

  querySelector(selector) {
    const children = this.app.children || [];
    if (selector === ".cell.selected") return children.find(child => child.className.split(" ").includes("cell") && child.className.split(" ").includes("selected")) || null;
    if (selector === ".rack-slot.selected") return children.find(child => child.className.split(" ").includes("rack-slot") && child.className.split(" ").includes("selected")) || null;
    return null;
  }
}

class EventBus {
  constructor() { this.listeners = []; }
  addEventListener(_type, listener) { this.listeners.push(listener); }
  dispatch(args) { for (const listener of this.listeners) listener({ detail: { args } }); }
}

function loadController(localStorage = memoryStorage()) {
  const document = new FakeDocument();
  const events = new EventBus();
  const messages = [];
  const window = {
    WordfeudBoard: WF,
    localStorage,
    sessionStorage: memoryStorage(),
    parent: null,
    clearTimeout: () => {},
    setTimeout: () => 1,
    cancelAnimationFrame: () => {},
    requestAnimationFrame: () => 1,
    visualViewport: null,
  };
  window.parent = window;
  const streamlit = {
    RENDER_EVENT: "streamlit:render",
    events,
    setComponentValue: value => messages.push(value),
    setComponentReady: () => {},
    setFrameHeight: () => {},
  };
  const context = vm.createContext({
    window,
    document,
    Element: FakeElement,
    Streamlit: streamlit,
    console,
    JSON,
    Math,
    Date,
    CustomEvent: class CustomEvent {},
  });
  vm.runInContext(fs.readFileSync("frontend/app.js", "utf8"), context, { filename: "frontend/app.js" });
  return { document, events, messages, localStorage };
}

function editProps(source, response = null) {
  return {
    snapshot: source,
    board_version: 0,
    mode: "edit",
    solve_result: null,
    response,
    storage_namespace: "test",
    can_purge_suggestions: true,
    wordlist_update_active: false,
  };
}

function previewProps(source, moves, response = null) {
  return {
    ...editProps(source, response),
    mode: "preview",
    solve_result: {
      token: "solve-1",
      state_hash: "hash-1",
      moves,
    },
  };
}

test("purging a suggestion emits the selected word and current solve identity", () => {
  const controller = loadController();
  const source = snapshot(["A"]);
  controller.events.dispatch(previewProps(source, [
    { word: "A", score: 1, row: 7, col: 7, direction: "H", tiles: [] },
  ]));

  controller.document.querySelectorAll("[data-purge-suggestion]")[0].click();

  assert.deepEqual(JSON.parse(JSON.stringify(controller.messages[0])), {
    id: 1,
    type: "purge_suggestion",
    payload: {
      suggestionIndex: 0,
      word: "A",
      solveToken: "solve-1",
      stateHash: "hash-1",
      boardVersion: 0,
    },
  });
});

test("rack selection survives a rerun before the first tile is typed", () => {
  const controller = loadController();
  const source = snapshot(["A"]);
  const empty = snapshot();
  controller.events.dispatch(editProps(source));
  controller.events.dispatch({
    ...editProps(source),
    mode: "preview",
    solve_result: {
      token: "solve-1",
      state_hash: "hash",
      moves: [{ word: "A", score: 1, row: 7, col: 7, direction: "H", tiles: [] }],
    },
  });
  controller.document.getElementById("place-button").click();
  controller.events.dispatch(editProps(empty, { kind: "place_result", ok: true, snapshot: empty }));

  controller.document.querySelectorAll("[data-rack]")[0].click();
  assert.ok(controller.document.querySelector(".rack-slot.selected"));

  // This is the stale/different snapshot that used to recreate the editor
  // with the default board selection before the key event was handled.
  controller.events.dispatch(editProps(source));
  assert.ok(controller.document.querySelector(".rack-slot.selected"));
  assert.equal(controller.document.querySelector(".cell.selected"), null);

  const keyboard = controller.document.getElementById("keyboard");
  keyboard.dispatchEvent({ type: "keydown", key: "B", preventDefault() {} });
  const rack = controller.document.querySelectorAll("[data-rack]");
  assert.equal(rack[0].className.includes("empty"), false);
  assert.equal(rack[1].className.includes("selected"), true);
  assert.equal(controller.document.querySelector(".cell.selected"), null);
});

test("editing an active saved game waits for an explicit save", () => {
  const localStorage = memoryStorage();
  const source = snapshot(["A"]);
  const saved = WF.saveSnapshot(localStorage, "Partij", source, null, "test");
  assert.equal(saved.ok, true);
  assert.equal(WF.setActiveSaveId(localStorage, saved.record.id, "test").ok, true);

  const controller = loadController(localStorage);
  controller.events.dispatch(editProps(source));
  controller.document.getElementById("keyboard").dispatchEvent({ type: "keydown", key: "B", preventDefault() {} });

  const beforeExplicitSave = WF.snapshotFromRecord(WF.readSaves(localStorage, "test").records[0]);
  assert.equal(beforeExplicitSave.grid[7][7].letter, null);
  assert.deepEqual(beforeExplicitSave.rack, ["A"]);

  controller.document.getElementById("save-button").click();
  const afterExplicitSave = WF.snapshotFromRecord(WF.readSaves(localStorage, "test").records[0]);
  assert.equal(afterExplicitSave.grid[7][7].letter, "B");
  assert.deepEqual(afterExplicitSave.rack, ["A"]);
});

test("placing a suggestion does not update the active saved game automatically", () => {
  const localStorage = memoryStorage();
  const source = snapshot(["A"]);
  const placed = snapshot();
  placed.grid[7][7] = { letter: "A", bonus: "NORMAL", is_blank: false };
  const saved = WF.saveSnapshot(localStorage, "Partij", source, null, "test");
  assert.equal(saved.ok, true);
  assert.equal(WF.setActiveSaveId(localStorage, saved.record.id, "test").ok, true);

  const controller = loadController(localStorage);
  controller.events.dispatch(editProps(source));
  controller.events.dispatch({
    ...editProps(source),
    mode: "preview",
    solve_result: {
      token: "solve-1",
      state_hash: "hash",
      moves: [{ word: "A", score: 1, row: 7, col: 7, direction: "H", tiles: [] }],
    },
  });
  controller.document.getElementById("place-button").click();
  controller.events.dispatch(editProps(placed, { kind: "place_result", ok: true, snapshot: placed }));

  const afterPlacement = WF.snapshotFromRecord(WF.readSaves(localStorage, "test").records[0]);
  assert.equal(afterPlacement.grid[7][7].letter, null);
  assert.deepEqual(afterPlacement.rack, ["A"]);
});

test("explicit save replaces the placement status and survives a replayed response", () => {
  const localStorage = memoryStorage();
  const source = snapshot(["A"]);
  const placed = snapshot();
  placed.grid[7][7] = { letter: "A", bonus: "NORMAL", is_blank: false };
  const saved = WF.saveSnapshot(localStorage, "Partij", source, null, "test");
  assert.equal(saved.ok, true);
  assert.equal(WF.setActiveSaveId(localStorage, saved.record.id, "test").ok, true);

  const controller = loadController(localStorage);
  controller.events.dispatch(editProps(source));
  const placeResponse = { kind: "place_result", ok: true, snapshot: placed };
  controller.events.dispatch(editProps(placed, placeResponse));
  assert.match(controller.document.app.html, /Sla het spel handmatig op/);

  controller.document.getElementById("save-button").click();
  assert.match(controller.document.app.html, /Opgeslagen als “Partij”/);

  // A parent rerun can replay the last response; it must not overwrite the
  // newer confirmation shown after the explicit save.
  controller.events.dispatch(editProps(placed, placeResponse));
  assert.match(controller.document.app.html, /Opgeslagen als “Partij”/);
});

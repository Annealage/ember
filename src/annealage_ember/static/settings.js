/**
 * The settings window, and the boot-time read that precedes it.
 *
 * Two jobs, and the first happens whether or not anyone opens the window.
 * `load` (on what `initSettings` returns) runs at page load and applies the
 * settings whose effect is "load" to this page: whether tool cards start
 * closed, here, and whatever the product's own load-effect keys mean, through
 * the `onLoad` hook the product passes (Mesh: which axis is up). Those go
 * through store setters rather than being applied to the DOM here, because
 * other modules read them and a value with two writers is the defect
 * `store.js` exists to prevent.
 *
 * The second job is the window itself. Every field shows where its value came
 * from, because a three-layer scheme is otherwise mystifying: "port 9000, from
 * user settings" tells you which file to edit, while a bare "9000" does not.
 * Fields whose effect is "restart" say so instead of pretending to apply, and
 * there is deliberately no live rebind: moving a listening socket while
 * connections are open is complexity with a security-relevant failure mode,
 * and restarting is cheap.
 *
 * The server answers `GET /settings` with what is in effect plus a `pending` map
 * of anything saved that differs, and "in effect" is per key: a restart-effect
 * key reports what the running process resolved at startup, while a load-effect
 * key reports what is on disk, because each page applies those as it loads.
 * Every control here edits the *saved* value, and the note beside it carries
 * what the running server is still using, so pressing Save with no edits can
 * never revert a change made earlier.
 *
 * Nothing here names a setting beyond the one generic load-effect key. The
 * payload says which keys the window shows and under which heading
 * (`sections`, from each key's own declaration on the server), which of them
 * are a closed set of choices (`choices`, `nullable`), and which product it is
 * describing (`product`), so a product's key appears and is editable here
 * because the product declared it, not because this file knows its name.
 *
 * Under Diagnostics, the Agent log section lists the agent backend's own logs
 * for this session (`GET /agent/logs`: omp's process log, the Claude CLI's
 * transcript, a Codex rollout, each backend's stderr) with where each one is,
 * and shows the end of one on request (`GET /agent/logs/<name>`), since that is
 * where the reason for a failure usually is. It is filled in once the window
 * is open, so a slow disk never holds up the settings.
 */

import { store } from "./store.js";
import { withToken } from "./ws.js";
import { toast } from "./ui.js";
import { appUrl } from "./url.js";
import { providerLoginSection } from "./login.js";

// Which layer a value came from, in words a person can act on. The keys are
// the layer names `settings.py` reports; the project file lives in the
// product's own state directory, which the payload names.
function originText(from, product) {
  switch (from) {
    case "flag":
      return "from a command-line flag, this run only";
    case "project":
      return `from this project's ${product.state_dirname}/config.toml`;
    case "user":
      return "from your user settings";
    case "default":
      return "built-in default";
    default:
      return from;
  }
}

// The options a key with a closed set of values is offered as, so the window
// shows a select rather than a free-text box that can only be got wrong. An
// empty value means "not set", offered first for a key where that is legal.
function choicesOf(entry) {
  const choices = entry.choices || [];
  if (!choices.length) return null;
  return entry.nullable ? ["", ...choices] : choices;
}

/**
 * Fetches `/settings` and returns the payload, or null if it could not be
 * read. A failure is not fatal to the page: the page works without ever
 * knowing what the saved preferences are, so this reports and carries on.
 */
async function fetchSettings() {
  try {
    const res = await fetch(withToken(appUrl("settings")));
    if (!res.ok) return null;
    return await res.json();
  } catch (err) {
    return null;
  }
}

/**
 * Applies the load-effect settings to this page, through the store: the
 * product's first, through its `onLoad(settings)` hook, then the chat pane's
 * own tool-card preference.
 *
 * Called at boot and after every save. Anything else in the payload describes
 * the run rather than the page, and is only shown when the window is opened.
 * Only a value that differs is written, here and (by the same rule) in the
 * product's hook: this read arrives well after the first frame, and
 * announcing a value the page already has can undo something done since,
 * a whole HTTP round trip later.
 */
function applyToPage(payload, onLoad) {
  const settings = payload && payload.settings;
  if (!settings) return;
  onLoad(settings);
  if (settings.tool_cards_collapsed) {
    const collapsed = settings.tool_cards_collapsed.value !== false;
    if (collapsed !== store.getState().toolCardsCollapsed) {
      store.setToolCardsCollapsed(collapsed);
    }
  }
}

function fieldRow(name, entry, pending, product, options = {}) {
  const row = document.createElement("div");
  row.className = "setrow";
  row.dataset.key = name;

  const label = document.createElement("label");
  label.className = "setlabel";
  label.textContent = options.label || name;
  label.htmlFor = "set-" + name;
  row.appendChild(label);

  // The control edits the value that is *saved*, which after a write is the
  // pending one rather than the one this run is using. Showing the running
  // value instead would mean that opening the window and pressing Save with no
  // edits silently reverted the last change.
  const editing = options.displayDefault ? "" : pending ? pending.value : entry.value;

  let input;
  const choices = choicesOf(entry);
  if (choices) {
    input = document.createElement("select");
    for (const choice of choices) {
      const option = document.createElement("option");
      option.value = choice;
      option.textContent = choice === "" ? (options.configured ? "Default (inherit)" : "(not set)") : choice;
      input.appendChild(option);
    }
    input.value = editing === null ? "" : String(editing);
  } else if (entry.type === "bool") {
    input = document.createElement("input");
    input.type = "checkbox";
    input.checked = editing === true;
  } else {
    input = document.createElement("input");
    input.type = entry.type === "int" ? "number" : "text";
    input.value = editing === null ? "" : String(editing);
  }
  if (options.models && input.tagName === "INPUT") {
    const select = document.createElement("select");
    select.className = "setinput-model-choice";
    select.id = "set-choice-" + name;
    select.disabled = !entry.editable;
    for (const [value, text] of [
      ["", options.inherits ? "Default (inherit)" : "Default"],
      ...options.models.map(value => [value, value]),
      ["__manual__", "Enter a model ID…"],
    ]) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = text;
      select.appendChild(option);
    }
    select.value = !editing ? "" : options.models.includes(editing) ? editing : "__manual__";
    input.hidden = select.value !== "__manual__";
    input.placeholder = "provider/model";
    input.setAttribute("autocomplete", "off");
    select.addEventListener("change", () => {
      input.hidden = select.value !== "__manual__";
      input.value = select.value === "__manual__" ? "" : select.value;
    });
    row.appendChild(select);
    label.htmlFor = select.id;
  }
  input.id = "set-" + name;
  input.className = "setinput";
  if (options.displayDefault) input.dataset.defaultBaseline = "true";
  if (options.configured) input.dataset.configured = "true";
  input.dataset.key = name;
  input.dataset.valueType = entry.type;
  if (!entry.editable) input.disabled = true;
  row.appendChild(input);

  const note = document.createElement("div");
  note.className = "setnote";
  const origin = originText(options.configured && pending ? pending.from : entry.from, product);
  const parts = [origin];
  if (entry.in_effect === false) {
    // A value this app does not use at all (the bind of an app a front door
    // serves): saying when it would take effect would be wrong.
    parts.push("not in effect: " + entry.reason);
  } else if (entry.effect === "restart") {
    parts.push("takes effect next run");
  }
  if (pending) {
    // The field above holds the saved value, so what the note has to supply is
    // the other half: what the running server is still using.
    parts.push(options.configured
      ? "pending restart; startup setting was " + JSON.stringify(entry.value)
      : "saved; this run is still using " + JSON.stringify(entry.value));
  }
  note.textContent = parts.join(" · ");
  row.appendChild(note);

  const help = document.createElement("div");
  help.className = "sethelp";
  help.textContent = options.description || entry.description || "";
  row.appendChild(help);

  return row;
}

// A product declares its tier/role keys; the UI does not know product names.
// These controls save configuration only. Backend-reported runtime is kept
// separate, since a resumed conversation can have a different actual model.
function modelSettingsBlock(payload) {
  const controls = payload.model_controls;
  const block = document.createElement("section");
  block.className = "setmodels";
  const heading = document.createElement("h3");
  heading.textContent = "Models and providers";
  const intro = document.createElement("p");
  intro.className = "sethelp";
  intro.textContent = "Shared defaults apply unless an agent has an override. "
    + "Leave either override empty to inherit its corresponding shared value. "
    + "Use provider/model IDs; changing provider does not change the backend. "
    + "Save changes your user settings for the next server run. Restart the server "
    + "and resume this conversation to apply them; saving never interrupts, restarts "
    + "or forks the conversation. Model availability depends on the operator's provider login.";
  block.append(heading, intro);

  const runtime = document.createElement("p");
  runtime.className = "setnote";
  const actual = controls.runtime;
  runtime.textContent = actual
    ? "Actual coordinator runtime (backend-reported): " + [
      actual.backend, actual.provider, actual.model,
      actual.effort ? "effort " + actual.effort : null,
    ].filter(Boolean).join(" · ")
    : "Actual coordinator runtime: not reported by the backend. Saved selections are not runtime confirmation.";
  block.appendChild(runtime);
  const session = document.createElement("p");
  session.className = "setnote";
  session.textContent = "Conversation: " + (controls.session_id || "not started")
    + ". Child runtime is reported by each work run, not by these saved defaults.";
  block.appendChild(session);

  const profile = controls.profile;
  const profileEntry = profile && payload.settings[profile.key];
  const profileDetail = document.createElement("p");
  profileDetail.className = "sethelp";
  if (profileEntry) {
    const row = fieldRow(profile.key, {
      ...profileEntry, choices: profile.profiles.map(item => item.id),
    }, payload.pending && payload.pending[profile.key], payload.product || {}, {
      label: "Default profile", configured: true,
      description: "Shared tier overrides must be cleared for a new profile to take effect; per-agent overrides are preserved.",
    });
    const select = row.querySelector("#set-" + profile.key);
    for (const option of select.children) {
      const item = profile.profiles.find(candidate => candidate.id === option.value);
      if (item) option.textContent = item.title;
    }
    let previous = select.value;
    select.addEventListener("change", () => {
      if (select.value === previous) return;
      const overrides = [];
      for (const spec of Object.values(controls.groups)) {
        for (const kind of ["model", "effort"]) {
          const input = block.querySelector("#set-" + spec[kind]);
          if (input && input.value.trim()) overrides.push({ input, title: spec.title + " " + kind });
        }
      }
      if (overrides.length && !window.confirm(
        "Switching profiles will clear these shared overrides on Save:\n\n"
        + overrides.map(item => item.title + ": " + item.input.value).join("\n")
        + "\n\nPer-agent overrides will not be changed. Continue?"
      )) {
        select.value = previous;
        return;
      }
      for (const { input } of overrides) {
        input.value = "";
        const chooser = block.querySelector("#set-choice-" + input.dataset.key);
        if (chooser) {
          chooser.value = "";
          input.hidden = true;
        }
      }
      previous = select.value;
    });
    block.append(row, profileDetail);
  }
  const catalogNote = document.createElement("p");
  catalogNote.className = "sethelp";
  catalogNote.textContent = (controls.models || []).length
    ? "Model suggestions come from the running backend. You can also enter a provider/model ID."
    : "No backend model catalog is available yet. Enter a provider/model ID directly.";
  block.appendChild(catalogNote);

  const previews = [];
  function pair(spec, title, inherits) {
    const heading = document.createElement("h4");
    heading.textContent = title;
    block.appendChild(heading);
    for (const kind of ["model", "effort"]) {
      const name = spec[kind];
      const entry = payload.settings[name];
      if (!entry) continue;
      const saved = payload.pending && payload.pending[name];
      const choices = kind === "effort" && controls.efforts
        ? [...new Set([...controls.efforts, saved ? saved.value : entry.value].filter(Boolean))]
        : null;
      block.appendChild(fieldRow(name, choices ? { ...entry, choices, nullable: true } : entry,
        saved, payload.product || {}, {
          label: kind === "model" ? "Provider / model" : "Reasoning effort",
          models: kind === "model" ? (controls.models || []) : null, inherits, configured: true,
          displayDefault: !!profileEntry && !inherits && (saved ? saved.from : entry.from) === "default",
          description: inherits
            ? "Optional user override. Default inherits " + kind + " from "
              + controls.groups[inherits].title + ". Saves to your user settings, not this project."
            : "Shared override. Default uses the selected profile; built-in values without a user override also inherit the profile.",
        }));
    }
    const preview = document.createElement("p");
    preview.className = "setnote";
    block.appendChild(preview);
    previews.push({ spec, inherits, preview });
  }
  for (const [id, spec] of Object.entries(controls.groups)) {
    pair({ ...spec, group_id: id }, spec.title + " shared default", null);
  }
  const overrides = document.createElement("h3");
  overrides.textContent = "Optional per-agent overrides";
  block.appendChild(overrides);
  for (const spec of controls.agents) {
    pair(spec, spec.title + " — inherits " + controls.groups[spec.inherits].title, spec.inherits);
  }
  function value(name) {
    const input = block.querySelector("#set-" + name);
    return input ? input.value.trim() : "";
  }
  function updatePreviews(edited = false) {
    const selectedProfile = profileEntry && profile.profiles.find(item => item.id === value(profile.key));
    if (profileEntry) {
      profileDetail.textContent = selectedProfile
        ? selectedProfile.billing + " " + Object.entries(selectedProfile.tiers)
          .map(([tier, config]) => tier + ": " + config.model + " · effort " + config.effort).join(" · ")
        : "No profile details available for this selection.";
    }
    function tierValue(id, kind) {
      const group = controls.groups[id];
      const name = group[kind];
      const current = value(name);
      return current || (selectedProfile && selectedProfile.tiers[id]
        && selectedProfile.tiers[id][kind]) || group["default_" + kind] || "";
    }
    for (const { spec, inherits, preview } of previews) {
      const configured = ["model", "effort"].map((kind) => {
        const own = inherits ? value(spec[kind]) : "";
        const id = inherits || spec.group_id;
        const effective = own || tierValue(id, kind);
        return kind + ": " + (effective || "backend default")
          + (inherits && !own ? " (inherited " + controls.groups[inherits].title + ")" : "");
      });
      preview.textContent = (edited ? "Selected configuration (save to persist): " : "Saved configuration: ")
        + configured.join(" · ");
    }
  }
  block.addEventListener("input", () => updatePreviews(true));
  block.addEventListener("change", () => updatePreviews(true));
  updatePreviews();
  return block;
}

function diagnosticsBlock(facts, product) {
  const lines = [];
  const cli = facts.claude_cli || {};
  lines.push([product.title + " version", product.version]);
  lines.push(["Python", (facts.python || {}).version]);
  lines.push(["claude CLI", cli.source === "missing"
    ? "not found: agent mode cannot run"
    : (cli.version || "version not reported") + " (" +
      (cli.source === "bundled" ? "bundled with the SDK" : "found on PATH") + ")"]);
  lines.push(["git", facts.git ? facts.git.version || facts.git.path : "not installed"]);
  const sandbox = facts.sandbox || {};
  if (!(sandbox.dependencies || []).length) {
    lines.push(["Sandbox", "provided by this platform"]);
  } else if ((sandbox.missing || []).length) {
    lines.push(["Sandbox", "missing " + sandbox.missing.join(", ")]);
  } else {
    lines.push(["Sandbox", sandbox.dependencies.join(", ") + " present"]);
  }
  lines.push(["Project", facts.project_root]);
  lines.push(["Session", facts.session_id || "none (viewer only)"]);
  lines.push(["Bound to", facts.bind ? facts.bind + ":" + facts.port : "unknown"]);
  const reachable = facts.reachable || [];
  if (reachable.length) {
    lines.push(["Addresses", reachable
      .map((entry) => entry.address + " (" + entry.interface + ")").join(", ")]);
  }
  const files = facts.settings_files || {};
  lines.push(["User settings", files.user + (files.user_present ? "" : " (not written yet)")]);
  lines.push(["Project config",
    files.project + (files.project_present ? "" : " (not written yet)")]);

  const wrap = document.createElement("div");
  wrap.className = "diagblock";
  for (const [name, value] of lines) {
    const row = document.createElement("div");
    row.className = "diagrow";
    const key = document.createElement("span");
    key.className = "diagkey";
    key.textContent = name;
    const val = document.createElement("span");
    val.className = "diagval";
    val.textContent = value === undefined || value === null ? "unknown" : String(value);
    row.appendChild(key);
    row.appendChild(val);
    wrap.appendChild(row);
  }
  return wrap;
}

/**
 * `GET path` (one of the `/agent/logs` routes), parsed, or null if it could
 * not be read. A refusal answers with a bare body rather than JSON, which
 * lands here too.
 */
async function fetchAgentLogs(path) {
  try {
    const res = await fetch(withToken(path));
    return await res.json();
  } catch (err) {
    return null;
  }
}

// Whether a line of a JSON-lines log is at debug level. Most of omp's log is,
// and the warnings worth reading are among it, so those lines start hidden.
function isDebugLine(line) {
  if (!line.startsWith("{")) return false;
  try {
    const level = JSON.parse(line).level;
    return level === "debug" || level === "trace";
  } catch (err) {
    return false;
  }
}

function kib(bytes) {
  return Math.max(1, Math.round(bytes / 1024)) + " KiB";
}

// One log's end, as `GET /agent/logs/<name>` sent it, scrolled to its last line.
function logBody(payload) {
  const wrap = document.createElement("div");
  if (!payload || payload.ok !== true) {
    wrap.className = "lognote";
    wrap.textContent = (payload && payload.error) || "The log could not be read.";
    return wrap;
  }
  const lines = payload.text.split("\n");
  const opts = document.createElement("div");
  opts.className = "logopts";
  const note = document.createElement("span");
  note.textContent = payload.truncated
    ? "the last " + kib(new TextEncoder().encode(payload.text).length) + " of " + kib(payload.size)
    : kib(payload.size);
  opts.appendChild(note);
  const pre = document.createElement("pre");
  pre.className = "logtail";

  let showDebug = false;
  const debugCount = payload.format === "jsonl" ? lines.filter(isDebugLine).length : 0;
  function render() {
    const shown = showDebug ? lines : lines.filter((line) => !isDebugLine(line));
    pre.textContent = shown.join("\n").trim() || "(nothing to show)";
    requestAnimationFrame(() => { pre.scrollTop = pre.scrollHeight; });
  }
  if (debugCount) {
    const label = document.createElement("label");
    const box = document.createElement("input");
    box.type = "checkbox";
    box.addEventListener("change", () => {
      showDebug = box.checked;
      render();
    });
    label.append(box, " show " + debugCount + " debug lines");
    opts.appendChild(label);
  } else {
    showDebug = true;
  }
  wrap.append(opts, pre);
  render();
  return wrap;
}

function logRow(entry) {
  const row = document.createElement("div");
  row.className = "logrow";
  const head = document.createElement("div");
  head.className = "loghead";
  const name = document.createElement("span");
  name.className = "logname";
  name.textContent = entry.name;
  const where = document.createElement("span");
  where.className = "logpath";
  where.textContent = entry.path || "kept in memory by this server";
  const button = document.createElement("button");
  button.type = "button";
  button.className = "logbtn";
  button.textContent = "Show";
  head.append(name, where, button);
  row.appendChild(head);

  let body = null;
  button.addEventListener("click", async () => {
    if (body) {
      body.remove();
      body = null;
      button.textContent = "Show";
      return;
    }
    button.disabled = true;
    // By name, not position: the list grows while the window is open (omp's
    // conversation file appears with the first message), so a position could
    // by now be a different log.
    const payload = await fetchAgentLogs(appUrl("agent/logs/" + encodeURIComponent(entry.name)));
    button.disabled = false;
    body = logBody(payload);
    row.appendChild(body);
    button.textContent = "Hide";
  });
  return row;
}

function agentLogSection() {
  const section = document.createElement("section");
  const heading = document.createElement("h3");
  heading.textContent = "Agent log";
  section.appendChild(heading);
  const list = document.createElement("div");
  list.className = "lognote";
  list.textContent = "Looking for the agent's logs…";
  section.appendChild(list);
  fetchAgentLogs(appUrl("agent/logs")).then((payload) => {
    const logs = payload && payload.ok === true ? payload.logs : null;
    if (!logs) {
      list.textContent = "The agent's logs could not be listed.";
    } else if (!logs.length) {
      list.textContent = "This run's agent has no logs of its own.";
    } else {
      list.textContent = "";
      list.className = "loglist";
      logs.forEach((entry) => list.appendChild(logRow(entry)));
    }
  });
  return section;
}

/**
 * Reads every input back and returns only the values that differ from what the
 * payload reported, so a save writes what was actually changed rather than
 * rewriting every key with what it already held.
 */
function collectChanges(root, payload) {
  const changes = {};
  for (const input of root.querySelectorAll(".setinput")) {
    const name = input.dataset.key;
    const entry = payload.settings[name];
    if (!entry || input.disabled) continue;
    let value;
    if (input.type === "checkbox") {
      value = input.checked;
    } else if (input.value === "") {
      value = !entry.nullable && entry.type === "str" ? "" : null;
    } else if (entry.type === "int") {
      const parsed = Number(input.value);
      if (!Number.isInteger(parsed)) {
        throw new Error(name + " must be a whole number");
      }
      value = parsed;
    } else {
      value = input.value;
    }
    const saved = input.dataset.defaultBaseline === "true"
      ? (!entry.nullable && entry.type === "str" ? "" : null)
      : payload.pending && payload.pending[name] ? payload.pending[name].value : entry.value;
    if (value !== saved) changes[name] = value;
  }
  return changes;
}

async function saveChanges(changes) {
  const res = await fetch(withToken(appUrl("settings")), {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ changes }),
  });
  let payload = null;
  try {
    payload = await res.json();
  } catch (err) {
    payload = null;
  }
  if (!res.ok || !payload || payload.ok !== true) {
    throw new Error((payload && payload.error) || "the server refused the change");
  }
  return payload;
}

/**
 * Wires the topbar gear to a modal built fresh each time it opens, and
 * returns `{open, close, settings, load}`.
 *
 * Rebuilt per open rather than kept and updated, because it is opened rarely
 * and every field in it is a rendering of a payload that may have changed
 * since last time; a cached panel would be one more thing that can disagree
 * with the server.
 *
 * `openButton` and `container` default to the page's `#settingsBtn` and
 * `#settingsModal`; a page with neither still gets `load`. `onLoad(settings)`
 * is the product's hook for its own load-effect keys, given the payload's
 * `settings` map at boot and after every save. `load()` does the boot-time
 * read; the page calls it once, when it no longer minds the round trip.
 */
export function initSettings({
  openButton = document.getElementById("settingsBtn"),
  container = document.getElementById("settingsModal"),
  onLoad = () => {},
} = {}) {
  let current = null;

  async function load() {
    current = await fetchSettings();
    applyToPage(current, onLoad);
    return current;
  }

  if (!openButton || !container) {
    return { open: () => {}, close: () => {}, settings: () => current, load };
  }

  let onKeydown = null;

  function close() {
    container.hidden = true;
    container.textContent = "";
    if (onKeydown) {
      document.removeEventListener("keydown", onKeydown);
      onKeydown = null;
    }
  }

  async function open() {
    const payload = await fetchSettings();
    if (!payload || !payload.settings) {
      toast("Could not read the settings from the server.");
      return;
    }
    current = payload;
    container.textContent = "";

    const panel = document.createElement("div");
    panel.className = "setpanel";
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-label", "Settings");

    const header = document.createElement("header");
    const title = document.createElement("h2");
    title.textContent = "Settings";
    const closeBtn = document.createElement("button");
    closeBtn.type = "button";
    closeBtn.className = "setclose";
    closeBtn.setAttribute("aria-label", "Close settings");
    closeBtn.textContent = "×";
    closeBtn.addEventListener("click", close);
    header.appendChild(title);
    header.appendChild(closeBtn);
    panel.appendChild(header);

    if (payload.saved_error) {
      const warn = document.createElement("div");
      warn.className = "setwarn";
      warn.textContent = "A settings file could not be read: " + payload.saved_error;
      panel.appendChild(warn);
    }

    const body = document.createElement("div");
    body.className = "setbody";
    const product = payload.product || {};
    const modelKeys = new Set();
    if (payload.model_controls) {
      const controls = payload.model_controls;
      for (const spec of [...Object.values(controls.groups), ...controls.agents]) {
        modelKeys.add(spec.model);
        modelKeys.add(spec.effort);
      }
      if (controls.profile) modelKeys.add(controls.profile.key);
      body.appendChild(modelSettingsBlock(payload));
    }
    for (const section of payload.sections || []) {
      if (!section.keys.some((name) => !modelKeys.has(name))) continue;
      const block = document.createElement("section");
      const heading = document.createElement("h3");
      heading.textContent = section.title;
      block.appendChild(heading);
      for (const name of section.keys) {
        if (modelKeys.has(name)) continue;
        const entry = payload.settings[name];
        if (!entry) continue;
        block.appendChild(
          fieldRow(name, entry, payload.pending && payload.pending[name], product));
      }
      body.appendChild(block);
    }

    const diagSection = document.createElement("section");
    const diagHeading = document.createElement("h3");
    diagHeading.textContent = "Diagnostics";
    diagSection.appendChild(diagHeading);
    diagSection.appendChild(diagnosticsBlock(payload.diagnostics || {}, product));
    body.appendChild(diagSection);
    providerLoginSection().then((section) => {
      if (section) body.insertBefore(section, diagSection);
    });
    body.appendChild(agentLogSection());
    panel.appendChild(body);

    const footer = document.createElement("footer");
    const status = document.createElement("span");
    status.className = "setstatus";
    const save = document.createElement("button");
    save.type = "button";
    save.className = "primary";
    save.textContent = "Save";
    save.addEventListener("click", async () => {
      let changes;
      try {
        changes = collectChanges(panel, payload);
      } catch (err) {
        status.textContent = err.message;
        return;
      }
      if (!Object.keys(changes).length) {
        status.textContent = "Nothing changed.";
        return;
      }
      save.disabled = true;
      status.textContent = "Saving…";
      try {
        const saved = await saveChanges(changes);
        current = saved;
        applyToPage(saved, onLoad);
        close();
        const names = Object.keys(changes);
        const restart = names.filter(
          (name) => saved.settings[name] && saved.settings[name].effect === "restart");
        toast(restart.length
          ? "Saved. " + restart.join(", ") + " takes effect next run."
          : "Saved.");
      } catch (err) {
        status.textContent = err.message;
        save.disabled = false;
      }
    });
    footer.appendChild(status);
    footer.appendChild(save);
    panel.appendChild(footer);

    container.appendChild(panel);
    container.hidden = false;

    onKeydown = (event) => {
      if (event.key === "Escape") close();
    };
    document.addEventListener("keydown", onKeydown);
  }

  openButton.addEventListener("click", () => { open(); });
  container.addEventListener("click", (event) => {
    // Only the backdrop closes; a click inside the panel must not.
    if (event.target === container) close();
  });

  return { open, close, settings: () => current, load };
}

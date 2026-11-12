"""Settings model inheritance and empty overrides, exercised under Node."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from annealage_ember import product

SETTINGS_JS = Path(product.__file__).resolve().parent / "static" / "settings.js"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not on PATH")

HARNESS = r"""
import { readFileSync } from "node:fs";

// Isolate the settings DOM helpers from transport/store dependencies. They
// remain the production source, with only their imports replaced by this host.
const source = readFileSync(process.argv[2], "utf8").replace(/^import .*;$/gm, "")
  + "\nexport { modelSettingsBlock, collectChanges, fieldRow };";
function el(tag) {
  return {
    tagName: tag.toUpperCase(), children: [], dataset: {}, attrs: {}, listeners: {},
    value: "", disabled: false,
    get textContent() { return this.text ?? this.children.map(c => c.textContent).join(" "); },
    set textContent(value) { this.text = String(value); this.children = []; },
    setAttribute(key, value) { this.attrs[key] = value; },
    appendChild(child) { this.children.push(child); return child; },
    append(...children) { this.children.push(...children); },
    addEventListener(name, callback) { this.listeners[name] = callback; },
    querySelectorAll(selector) {
      const matches = child => selector.startsWith("#")
        ? child.id === selector.slice(1) : child.className === selector.slice(1);
      return this.children.flatMap(child => [
        ...(matches(child) ? [child] : []), ...child.querySelectorAll(selector),
      ]);
    },
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; },
  };
}
globalThis.document = { createElement: el };
let confirmed = false;
let prompt = "";
const toasts = [];
globalThis.toast = (message) => toasts.push(message);
// The page's confirmation row: the style library's confirmRow, handed in.
const confirmRow = async (anchor, opts) => {
  prompt = opts.title + "\n" + opts.fields.map(([name, value]) => name + ": " + value).join("\n");
  return confirmed;
};
const { modelSettingsBlock, collectChanges, fieldRow } = await import(
  "data:text/javascript;base64," + Buffer.from(source).toString("base64")
);
const entry = (value, nullable = false) => ({
  value, nullable, editable: true, effect: "restart", type: "str", from: "user",
});
const payload = {
  product: {}, settings: {
    high_model: entry("anthropic/opus"), high_effort: entry("high"),
    model: entry(null, true), effort: entry(null, true),
    author_model: entry(""), author_effort: entry(""),
    reference_import_roots: entry(""),
  },
  pending: { high_model: { value: "openai-codex/gpt", from: "user" } },
  model_controls: {
    groups: { high: {
      title: "High", model: "high_model", effort: "high_effort",
      default_model: "anthropic/opus", default_effort: "high",
    } },
    agents: [
      { title: "Coordinator", model: "model", effort: "effort", inherits: "high" },
      { title: "Author", model: "author_model", effort: "author_effort", inherits: "high" },
    ],
    runtime: { backend: "omp", provider: "anthropic", model: "actual-opus", effort: "low" },
    models: ["anthropic/opus", "openai-codex/gpt"], session_id: "same-conversation",
  },
};
const block = modelSettingsBlock(payload, confirmRow);
// The modal also contains unrelated generic settings. An untouched empty
// nonnullable string must not turn into a null change when saving a model.
const panel = el("div");
panel.appendChild(block);
const unrelated = el("input");
unrelated.className = "setinput";
unrelated.dataset.key = "reference_import_roots";
unrelated.value = "";
panel.appendChild(unrelated);
const output = {
  initial: block.textContent,
  unchanged: collectChanges(panel, payload),
  suggestions: block.querySelector("#set-author_model").children.map(c => c.value),
};
block.querySelector("#set-high_model").value = "anthropic/sonnet";
block.listeners.input();
output.updated = block.textContent;
output.changed = collectChanges(panel, payload);
block.querySelector("#set-author_model").value = "openai-codex/override";
block.listeners.input();
output.overridden = block.textContent;
block.querySelector("#set-author_model").value = "";
block.querySelector("#set-high_model").value = "";
block.listeners.input();
output.cleared = block.textContent;
output.blankChanges = collectChanges(panel, payload);
const profilePayload = {
  product: {}, settings: {
    profile: entry("oauth"),
    high_model: { ...entry("anthropic/opus"), from: "default" },
    high_effort: { ...entry("high"), from: "default" },
    low_model: { ...entry("anthropic/sonnet"), from: "default" },
    low_effort: { ...entry("medium"), from: "default" },
    model: entry(null, true), effort: entry(null, true),
    author_model: entry("custom/legacy"), author_effort: entry("max"),
  },
  pending: { profile: { value: "api", from: "user" } },
  model_controls: {
    ...payload.model_controls,
    groups: {
      ...payload.model_controls.groups,
      low: { title: "Low", model: "low_model", effort: "low_effort",
        default_model: "anthropic/sonnet", default_effort: "medium" },
    },
    efforts: ["low", "medium", "high"],
    profile: { key: "profile", profiles: [
      { id: "oauth", title: "OAuth", billing: "Subscription", tiers: {
        high: { model: "anthropic/opus", effort: "high" },
        low: { model: "anthropic/sonnet", effort: "medium" },
      } },
      { id: "api", title: "API", billing: "API usage billed separately", tiers: {
        high: { model: "anthropic-api/opus", effort: "low" },
        low: { model: "anthropic-api/sonnet", effort: "medium" },
      } },
    ] },
  },
};
const profiles = modelSettingsBlock(profilePayload, confirmRow);
output.profileInitial = profiles.textContent;
output.tierRows = profiles.querySelectorAll(".setnote").map(c => c.textContent);
output.profileUnchanged = collectChanges(profiles, profilePayload);
output.legacyChoice = profiles.querySelector("#set-author_model").value;
output.legacyOptions = profiles.querySelector("#set-author_model").children.map(c => c.value);
output.legacyEfforts = profiles.querySelector("#set-author_effort").children.map(c => c.value);
const unavailable = modelSettingsBlock({
  ...profilePayload,
  model_controls: { ...profilePayload.model_controls, models: [], models_error: true },
});
output.unavailable = unavailable.textContent;
output.unavailableChoice = unavailable.querySelector("#set-author_model").value;
output.unavailableUnchanged = collectChanges(unavailable, profilePayload);
const emptyCatalog = modelSettingsBlock({
  ...profilePayload,
  model_controls: { ...profilePayload.model_controls, models: [] },
});
output.emptyCatalog = emptyCatalog.textContent;
profiles.querySelector("#set-profile").value = "oauth";
profiles.listeners.change();
output.profileChanged = collectChanges(profiles, profilePayload);
output.profileUpdated = profiles.textContent;
const chooser = profiles.querySelector("#set-model");
chooser.value = "openai-codex/gpt";
profiles.listeners.change();
output.catalogueChange = collectChanges(profiles, profilePayload);
chooser.value = "__other__";
chooser.listeners.change();
profiles.querySelector("#set-custom-model").value = "custom/new";
profiles.listeners.input();
output.manualChange = collectChanges(profiles, profilePayload);
chooser.value = "";
profiles.listeners.change();
output.defaultChange = collectChanges(profiles, profilePayload);
const explicitPayload = {
  ...profilePayload,
  settings: { ...profilePayload.settings, high_model: entry("custom/shared"), high_effort: entry("medium") },
};
const explicit = modelSettingsBlock(explicitPayload, confirmRow);
const profileSelect = explicit.querySelector("#set-profile");
profileSelect.value = "oauth";
await profileSelect.listeners.change();
output.cancelled = { value: profileSelect.value, changes: collectChanges(explicit, explicitPayload), prompt };
confirmed = true;
profileSelect.value = "oauth";
await profileSelect.listeners.change();
output.confirmed = collectChanges(explicit, explicitPayload);
output.confirmedAgent = explicit.querySelector("#set-author_model").value;
const historical = modelSettingsBlock(profilePayload);
const highChooser = historical.querySelector("#set-high_model");
highChooser.value = "anthropic/opus";
historical.listeners.change();
output.historicalPreview = historical.textContent;
output.historicalChange = collectChanges(historical, profilePayload);
const sameValuePayload = {
  ...profilePayload,
  pending: { ...profilePayload.pending, high_model: { value: "anthropic/opus", from: "user" } },
};
const sameValue = modelSettingsBlock(sameValuePayload, confirmRow);
output.sameValueVisible = sameValue.querySelector("#set-high_model").value;
output.sameValueUnchanged = collectChanges(sameValue, sameValuePayload);
const sameProfile = sameValue.querySelector("#set-profile");
sameProfile.value = "oauth";
await sameProfile.listeners.change();
output.sameValueCleared = collectChanges(sameValue, sameValuePayload);
output.sameValuePrompt = prompt;
// A page that gave no confirmRow and has no "style/" import map entry cannot
// ask, so the profile is left as it was and the person is told.
const noAsk = modelSettingsBlock(explicitPayload);
const noAskSelect = noAsk.querySelector("#set-profile");
noAskSelect.value = "oauth";
await noAskSelect.listeners.change();
output.noAsk = { value: noAskSelect.value, changes: collectChanges(noAsk, explicitPayload), toasts };
output.livePending = modelSettingsBlock({ ...payload, model_apply: {status: "pending"} }).textContent;
output.liveError = modelSettingsBlock({ ...payload, model_apply: {status: "error", error: "refused"} }).textContent;
output.liveApplied = modelSettingsBlock({ ...payload, model_apply: {status: "applied"} }).textContent;
output.restartRequired = modelSettingsBlock({ ...payload, model_apply: {status: "restart_required"} }).textContent;
const plain = fieldRow("open_browser", {
  value: true, from: "default", effect: "restart", editable: true, type: "bool",
  label: "Open a browser on start", description: "Open a browser tab when the server starts.",
}, null, {});
const named = fieldRow("permission_mode", {
  value: null, from: "project", effect: "restart", editable: true, type: "str or null",
  choices: ["default", "acceptEdits", "plan"], nullable: true, description: "How often it asks.",
}, null, {});
output.rows = {
  plainLabel: plain.querySelector(".setlabel").textContent,
  plainNotes: plain.querySelectorAll(".setnote").map(c => c.textContent),
  fallbackLabel: named.querySelector(".setlabel").textContent,
  fallbackNote: named.querySelector(".setnote").textContent,
  options: named.querySelector("#set-permission_mode").children.map(c => c.textContent),
};
console.log(JSON.stringify(output));
"""


def test_model_controls_separate_saved_runtime_and_resolve_inheritance(tmp_path):
    script = tmp_path / "settings-harness.mjs"
    script.write_text(HARNESS)
    run = subprocess.run(
        ["node", str(script), str(SETTINGS_JS)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, run.stderr
    output = json.loads(run.stdout)
    assert output["unchanged"] == {}
    assert output["suggestions"] == ["", "anthropic/opus", "openai-codex/gpt", "__other__"]
    assert "pending restart; startup setting was" in output["initial"]
    assert "waiting for current turns" in output["livePending"]
    assert "pending restart" not in output["livePending"]
    assert "Saved, but applying failed: refused" in output["liveError"]
    assert "Saved model settings applied." in output["liveApplied"]
    assert "require a restart" in output["restartRequired"]
    assert "Model: openai-codex/gpt (inherited from High)" in output["initial"]
    assert "Running now: omp, anthropic, actual-opus, effort low" in output["initial"]
    assert "same-conversation" not in output["initial"]  # an id is not shown
    assert output["changed"] == {"high_model": "anthropic/sonnet"}
    assert "Model: anthropic/sonnet (inherited from High)" in output["updated"]
    assert (
        "Model: openai-codex/override; Reasoning effort: high (inherited from High)"
        in output["overridden"]
    )
    assert "Model: anthropic/opus (inherited from High)" in output["cleared"]
    assert output["blankChanges"] == {"high_model": ""}
    assert "actual-opus" in output["updated"]
    assert "API usage billed separately" in output["profileInitial"]
    assert "High: anthropic-api/opus, low" in output["profileInitial"]
    assert "Low: anthropic-api/sonnet, medium" in output["profileInitial"]
    assert "High: anthropic-api/opus, low" in output["tierRows"]
    assert "Low: anthropic-api/sonnet, medium" in output["tierRows"]
    assert "pending restart; startup setting was" in output["profileInitial"]
    assert "actual-opus" in output["profileInitial"]
    assert output["profileUnchanged"] == {}
    assert output["legacyChoice"] == "custom/legacy"
    assert "custom/legacy" in output["legacyOptions"]
    assert "Model catalogue unavailable" in output["unavailable"]
    assert output["unavailableChoice"] == "custom/legacy"
    assert output["unavailableUnchanged"] == {}
    assert "max" in output["legacyEfforts"]
    assert output["profileChanged"] == {"profile": "oauth"}
    assert "No catalogue models available" in output["emptyCatalog"]
    assert "Subscription" in output["profileUpdated"]
    assert "High: anthropic/opus, high" in output["profileUpdated"]
    assert "Low: anthropic/sonnet, medium" in output["profileUpdated"]
    assert output["catalogueChange"] == {"profile": "oauth", "model": "openai-codex/gpt"}
    assert output["manualChange"] == {"profile": "oauth", "model": "custom/new"}
    assert output["defaultChange"] == {"profile": "oauth"}
    assert output["cancelled"]["value"] == "api"
    assert output["cancelled"]["changes"] == {}
    assert "Switch profile" in output["cancelled"]["prompt"]
    assert "High model: custom/shared" in output["cancelled"]["prompt"]
    assert "High effort: medium" in output["cancelled"]["prompt"]
    assert output["confirmed"] == {
        "profile": "oauth",
        "high_model": "",
        "high_effort": "",
    }
    assert output["confirmedAgent"] == "custom/legacy"
    assert "High: anthropic-api/opus, low" in output["historicalPreview"]
    assert output["historicalChange"] == {"high_model": "anthropic/opus"}
    assert output["sameValueVisible"] == "anthropic/opus"
    assert output["sameValueUnchanged"] == {}
    assert output["sameValueCleared"] == {"profile": "oauth", "high_model": ""}
    assert "High model: anthropic/opus" in output["sameValuePrompt"]
    assert output["noAsk"]["value"] == "api"
    assert output["noAsk"]["changes"] == {}
    assert output["noAsk"]["toasts"] == [
        "The profile was not changed: this page cannot ask for confirmation."
    ]
    assert "window.confirm" not in SETTINGS_JS.read_text()
    rows = output["rows"]
    assert rows["plainLabel"] == "Open a browser on start"
    assert rows["plainNotes"] == []  # a built-in default says nothing
    assert rows["fallbackLabel"] == "Permission mode"  # the key's name in words
    assert rows["fallbackNote"] == "Set for this project"
    assert rows["options"] == ["(not set)", "Ask as usual", "Accept edits", "Plan only"]

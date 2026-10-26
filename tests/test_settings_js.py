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
  + "\nexport { modelSettingsBlock, collectChanges };";
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
const { modelSettingsBlock, collectChanges } = await import(
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
const block = modelSettingsBlock(payload);
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
  suggestions: block.querySelector("#settings-model-suggestions").children.map(c => c.value),
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
    assert output["suggestions"] == ["anthropic/opus", "openai-codex/gpt"]
    assert "pending restart; startup setting was" in output["initial"]
    assert "model: openai-codex/gpt (inherited High)" in output["initial"]
    assert (
        "Actual coordinator runtime (backend-reported): omp · anthropic · actual-opus · effort low"
        in output["initial"]
    )
    assert "same-conversation" in output["initial"]
    assert output["changed"] == {"high_model": "anthropic/sonnet"}
    assert "model: anthropic/sonnet (inherited High)" in output["updated"]
    assert "model: openai-codex/override · effort: high (inherited High)" in output["overridden"]
    assert "model: anthropic/opus (inherited High)" in output["cleared"]
    assert output["blankChanges"] == {"high_model": ""}
    assert "actual-opus" in output["updated"]

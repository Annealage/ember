/**
 * The entry a page built by `embed.render_agent_page` loads: it reads its
 * configuration from `data-ember-*` attributes on the chat pane (a page with
 * no inline script cannot be given it any other way under the policy) and
 * calls `bootstrapAgentPage`. A page that needs hooks of its own imports
 * bootstrap.js from a module of its own instead.
 */

import { bootstrapAgentPage } from "./bootstrap.js";

const pane = document.getElementById("chat");
const d = pane.dataset;
const flag = (value) => value === "true";

bootstrapAgentPage({
  hosted: flag(d.emberHosted),
  start: d.emberStart || undefined,
  sessionUrl: d.emberSessionUrl ? new URL(d.emberSessionUrl, location.href).pathname : null,
  chat: { modelControl: d.emberModelControl !== "false" },
  review: d.emberReview === "false" ? false : {},
  settings: d.emberSettings === "false" ? false : {},
});

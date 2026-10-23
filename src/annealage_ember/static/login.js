/**
 * The settings window's "Model providers" section: which providers omp is
 * logged in to, signing in again when a login has expired, and signing out.
 *
 * A sign-in is the provider's own page in another tab, then the code or the
 * address that page ended on pasted back here. Anthropic and Codex end on a
 * redirect to `localhost` on the machine omp runs on, so on any other machine
 * the browser shows a connection error there; the address in its bar is
 * exactly what is to be pasted, and the window says so. What is pasted goes
 * to `POST /agent/login/submit` and is never kept, shown again or logged.
 *
 * An app built without a `provider_login` answers 404 to the provider list;
 * the section is then not shown at all.
 */

import { withToken } from "./ws.js";
import { appUrl } from "./url.js";

async function call(method, url, body) {
  try {
    const res = await fetch(withToken(url), {
      method,
      headers: body === undefined ? {} : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    let payload = null;
    try { payload = await res.json(); } catch (err) { payload = null; }
    return { status: res.status, payload };
  } catch (err) {
    return { status: 0, payload: null };
  }
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function failureText(result) {
  const payload = result.payload;
  return (payload && payload.error) || "The request did not go through.";
}

// The sign-in step: the address, a copy button, what to do, and the paste box.
function flowPanel(flow, { onSubmit, onCancel }) {
  const box = el("div", "loginflow");
  box.appendChild(el("div", "logname", "Signing in to " + (flow.name || flow.provider_id)));
  if (flow.url) {
    const row = el("div", "logopts");
    const link = el("a", "loginlink", "Open the sign-in page");
    link.href = flow.url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    row.appendChild(link);
    const copy = el("button", "logbtn", "Copy link");
    copy.type = "button";
    copy.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(flow.url);
        copy.textContent = "Copied";
      } catch (err) {
        copy.textContent = "Copy failed";
      }
    });
    row.appendChild(copy);
    box.appendChild(row);
  }
  if (flow.instructions) box.appendChild(el("div", "lognote", flow.instructions));
  box.appendChild(el("div", "lognote",
    "When the page finishes it may send your browser to an address on localhost, " +
    "which shows a connection error unless this is the machine omp runs on. " +
    "That's expected: copy the whole address from the browser's address bar " +
    "(or the code the page shows) and paste it below."));
  if (flow.state === "awaiting_code") {
    const input = el("input", "setinput");
    input.type = "password";
    input.autocomplete = "off";
    input.spellcheck = false;
    input.placeholder = "Paste the code or the address";
    const submit = el("button", "logbtn", "Submit");
    submit.type = "button";
    const go = () => {
      const value = input.value;
      input.value = "";
      submit.disabled = true;
      onSubmit(value);
    };
    submit.addEventListener("click", go);
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") go();
    });
    const row = el("div", "logopts");
    row.appendChild(input);
    row.appendChild(submit);
    box.appendChild(row);
    if (flow.attempts > 1) {
      box.appendChild(el("div", "lognote", "That wasn't accepted. Paste it again."));
    }
  } else {
    box.appendChild(el("div", "lognote", "Waiting for the sign-in page…"));
  }
  const cancel = el("button", "logbtn", "Cancel");
  cancel.type = "button";
  cancel.addEventListener("click", onCancel);
  box.appendChild(cancel);
  return box;
}

/**
 * The section, or null when this app has no provider login. `loadProviders`
 * is called again after a sign-in or sign-out so the status stays true.
 */
export async function providerLoginSection() {
  const first = await call("GET", appUrl("agent/login/providers"));
  if (first.status === 404 || first.status === 0) return null;

  const section = el("section", "loginsection");
  section.appendChild(el("h3", undefined, "Model providers"));
  const note = el("div", "lognote");
  const filter = el("input", "setinput");
  filter.placeholder = "Filter providers";
  filter.type = "search";
  const list = el("div", "loglist");
  const flowBox = el("div");
  section.append(note, filter, flowBox, list);

  let providers = [];
  let flowName = "";

  const say = (text) => { note.textContent = text || ""; };

  const renderFlow = (flow) => {
    flowBox.textContent = "";
    if (!flow || (flow.state !== "starting" && flow.state !== "awaiting_code")) return;
    flowBox.appendChild(flowPanel({ ...flow, name: flowName }, {
      onSubmit: async (value) => {
        const result = await call("POST", appUrl("agent/login/submit"), { code: value });
        await settle(result);
      },
      onCancel: async () => {
        await call("POST", appUrl("agent/login/cancel"), {});
        renderFlow(null);
        say("");
      },
    }));
  };

  const settle = async (result) => {
    const flow = result.payload && result.payload.ok ? result.payload : null;
    if (!flow) {
      renderFlow(null);
      say(failureText(result));
      return;
    }
    if (flow.state === "done") {
      renderFlow(null);
      say("Signed in to " + flowName + ". The agent picks it up the next time it starts.");
      await reload();
    } else if (flow.state === "failed") {
      renderFlow(null);
      say("Sign-in failed: " + (flow.error || "omp gave no reason") + ".");
    } else {
      say("");
      renderFlow(flow);
    }
  };

  const draw = () => {
    const needle = filter.value.trim().toLowerCase();
    list.textContent = "";
    const rows = providers
      .filter((p) => p.available)
      .filter((p) => !needle || p.name.toLowerCase().includes(needle) || p.id.includes(needle))
      .sort((a, b) => Number(b.authenticated) - Number(a.authenticated));
    for (const provider of rows) {
      const row = el("div", "logrow");
      const head = el("div", "loghead");
      head.appendChild(el("span", "logname", provider.name));
      head.appendChild(el("span", "lognote",
        provider.authenticated ? "signed in" : "not signed in"));
      const signIn = el("button", "logbtn", provider.authenticated ? "Sign in again" : "Sign in");
      signIn.type = "button";
      signIn.addEventListener("click", async () => {
        flowName = provider.name;
        say("Starting the sign-in…");
        renderFlow(null);
        signIn.disabled = true;
        const result = await call("POST", appUrl("agent/login/begin"), { provider_id: provider.id });
        signIn.disabled = false;
        await settle(result);
      });
      head.appendChild(signIn);
      if (provider.authenticated) {
        const signOut = el("button", "logbtn", "Sign out");
        signOut.type = "button";
        signOut.addEventListener("click", async () => {
          signOut.disabled = true;
          const listed = await call("GET",
            appUrl("agent/login/accounts") + "?provider_id=" + encodeURIComponent(provider.id));
          const accounts = listed.payload && listed.payload.ok ? listed.payload.accounts : null;
          if (!accounts) {
            say(failureText(listed));
            signOut.disabled = false;
            return;
          }
          const ids = accounts.map((a) => (a && (a.credentialId || a.id)) || null);
          let failed = null;
          for (const id of ids.length ? ids : [null]) {
            const body = { provider_id: provider.id };
            if (id) body.credential_id = String(id);
            const result = await call("POST", appUrl("agent/login/logout"), body);
            if (!(result.payload && result.payload.ok)) failed = failureText(result);
          }
          say(failed ? "Sign-out failed: " + failed : "Signed out of " + provider.name + ".");
          await reload();
        });
        head.appendChild(signOut);
      }
      row.appendChild(head);
      list.appendChild(row);
    }
    if (!rows.length) list.appendChild(el("div", "lognote", "No provider matches."));
  };

  const reload = async () => {
    const result = await call("GET", appUrl("agent/login/providers"));
    if (result.payload && result.payload.ok) {
      providers = result.payload.providers;
      draw();
    } else {
      say(failureText(result));
    }
  };

  filter.addEventListener("input", draw);
  if (first.payload && first.payload.ok) {
    providers = first.payload.providers;
    draw();
  } else {
    say(failureText(first));
  }
  // A flow begun before this window was opened (or in another tab).
  const current = await call("GET", appUrl("agent/login/state"));
  if (current.payload && current.payload.ok) {
    if (current.payload.provider_id) {
      const known = providers.find((p) => p.id === current.payload.provider_id);
      flowName = known ? known.name : current.payload.provider_id;
    }
    renderFlow(current.payload);
  }
  return section;
}

/**
 * The page-side composition every product repeated: the chat pane, the one
 * WebSocket, the review client, settings and pause, wired together.
 *
 * `bootstrapAgentPage` expects the markup `embed.render_agent_page` (or
 * `embed.chat_markup`) writes, or any page that has the same ids. A product
 * with a view of its own passes hooks for it and keeps the rest; one that only
 * wants the chat passes nothing (see agent-page.js, the entry that does so
 * from `data-ember-*` attributes on the pane).
 *
 * The one WebSocket controller: `initWs` has no dispose, owns reconnect and
 * replay state, and a second one would leave two reconnect loops. It is
 * created once, on the first successful connect, and reused for the life of
 * the page; a hosted worker that ends and a session that is resumed go through
 * the same controller.
 *
 * Hosted differs in two ways. The connection starts from a button (Start
 * session, then Resume session when the connection falls back to polling
 * because a finite worker ended), and each start first makes sure a worker
 * exists (admission.js: one POST, never repeated). Standalone connects at
 * once and shows no button, with the token transport ws.js already uses.
 */

import { initChat } from "./chat.js";
import { initPause } from "./pause.js";
import { initReview } from "./review.js";
import { initSettings } from "./settings.js";
import { appUrl } from "./url.js";
import { store } from "./store.js";
import { initWs } from "./ws.js";
import { ensureHostedSession } from "./admission.js";

function byId(root, id) {
  return root.querySelector ? root.querySelector("#" + id) : root.getElementById(id);
}

// The header's "Working m:ss": chat.js keeps data-working and
// data-turn-started on the pane, the clock is drawn here.
function workingClock(pane, clock) {
  if (!pane || !clock) return;
  let ticker = null;
  function tick() {
    const started = Number(pane.dataset.turnStarted);
    const s = started ? Math.max(0, Math.floor((Date.now() - started) / 1000)) : 0;
    clock.textContent = started ? Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0") : "";
  }
  new MutationObserver(() => {
    tick();
    const working = "working" in pane.dataset;
    if (working && !ticker) ticker = setInterval(tick, 1000);
    else if (!working && ticker) {
      clearInterval(ticker);
      ticker = null;
    }
  }).observe(pane, { attributes: true, attributeFilter: ["data-working", "data-turn-started"] });
}

// The header's More menu closes on a pick, an outside click and Escape.
function moreMenu(root, more) {
  if (!more) return;
  more.addEventListener("click", (event) => {
    if (event.target.closest(".menu button")) more.open = false;
  });
  document.addEventListener("click", (event) => {
    if (!more.contains(event.target)) more.open = false;
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") more.open = false;
  });
}

/**
 * @param root      where the pane's elements are looked up (default: the document)
 * @param hosted    a finite hosted worker: start/resume from a button, admit first
 * @param start     "auto" (connect at once) or "button"; default "button" when hosted
 * @param sessionUrl  the hosted session route as a string, already prefix-safe;
 *                  default `appUrl("agent/session")`
 * @param fetcher   the fetch the admission requests use, so a page that
 *                  carries its own auth passes one that adds it
 * @param ensureSession  replaces admission.js's `ensureHostedSession({url, fetcher})`
 * @param chat      options for `initChat` besides `send` (`modelControl`, `agentTitles`, `ids`)
 * @param ws        options for `initWs` besides the callbacks below (`allowModelChange`, `connTitles`)
 * @param review    false for a product with no review, or `{onChange, onError}`
 * @param settings  false for none, or `{onLoad}`
 * @param liveWaitMs  how long a hosted start waits for the socket to say live before
 *                  offering Resume session again (a controller that already
 *                  reported its downtime will not report it twice)
 * @param hooks     the product's own: `onEvent` (name -> handler, with the review's),
 *                  `onLive`, `onFallback`, `onHello`, `onAgentEvent`, `onPaused`,
 *                  `onRefused`, each called after the shared one
 * Returns `{chat, pause, review, connect, socket}`; `socket()` is null until the
 * first connect.
 */
export function bootstrapAgentPage({
  root = document,
  hosted = false,
  start = hosted ? "button" : "auto",
  sessionUrl = null,
  fetcher = undefined,
  ensureSession = ensureHostedSession,
  chat: chatOptions = {},
  ws: wsOptions = {},
  review: reviewOptions = {},
  settings: settingsOptions = {},
  hooks = {},
  liveWaitMs = 15000,
  gapMs = 2000,
} = {}) {
  const pane = byId(root, "chat");
  const errorEl = byId(root, "err");
  const button = byId(root, "sessionStart");
  workingClock(pane, byId(root, "chatWorkingTime"));
  moreMenu(root, byId(root, "chatMore"));

  let socket = null;
  // A hosted worker that finishes its turn is shut down on purpose, so a
  // socket that closes with every turn answered is an idle session, not a
  // failure. `idle` is that state; a message sent into it is held in
  // `resuming` while the session is admitted again, then goes out once the
  // socket is live, or goes back into the composer if that does not happen.
  let idle = false;
  let resuming = null;
  // Idle means the last turn really succeeded (answered, no error on it, not
  // rejected or lost to an exiting backend) and the server says the worker
  // ended: GET agent/session answers 503 job_ended. A failed start, a failed
  // turn or any other lost relay keeps the red state and says why.
  const FAILED_STOPS = new Set(["rejected", "backend_exited", "not_sent"]);
  const lastTurnSucceeded = () => {
    const c = store.getState().chat;
    const last = c.turns[c.turns.length - 1];
    return !!last && c.pendingUser.length === 0
      && last.complete && last.errors.length === 0
      && !!last.stopReason && !FAILED_STOPS.has(last.stopReason);
  };
  // The worker can still be shutting down when the socket falls back, so the
  // answer is asked for a few times (bounded) before it counts as no.
  async function workerEndedNormally(attempts = 3, gapMs = 2000) {
    if (!lastTurnSucceeded()) return false;
    for (let i = 0; i < attempts && !live; i++) {
      if (i > 0) await new Promise((resolve) => setTimeout(resolve, gapMs));
      try {
        const response = await (fetcher || fetch)(sessionUrl || appUrl("agent/session"), { cache: "no-store" });
        if (response.status === 503) {
          const body = await response.json();
          if (body && body.error && body.error.type === "job_ended") return true;
        }
      } catch (failure) {
        // asked again, or no
      }
    }
    return false;
  }
  const send = (frame) => {
    if (hosted && idle && !live && frame.type === "turn") {
      if (resuming) return false;
      resuming = frame;
      connect();
      return true;
    }
    return socket && socket.send(frame);
  };
  function settleResume(reason) {
    const frame = resuming;
    resuming = null;
    if (!frame) return;
    if (reason) chat.handleRefused(reason, frame.client_id);
    else {
      chat.adoptSent(frame.client_id);
      socket.send(frame);
    }
  }
  const chat = initChat({ root, send, ...chatOptions });
  const pause = initPause({ send, button: byId(root, "pauseBtn") });
  const review = reviewOptions === false ? null : initReview({
    onChange: () => {},
    ...reviewOptions,
    onError: (message) => {
      if (errorEl) errorEl.textContent = message;
      if (reviewOptions.onError) reviewOptions.onError(message);
    },
  });
  if (settingsOptions !== false) initSettings(settingsOptions).load();

  function showError(message) {
    if (errorEl) errorEl.textContent = message;
  }
  function setButton(text, { hidden = false, disabled = false } = {}) {
    if (!button) return;
    button.textContent = text;
    button.hidden = hidden;
    button.disabled = disabled;
  }

  // Whether the socket is open and live now, from its own callbacks: a
  // reused controller that is still reconnecting is not, however well the
  // admission just went, and a send then would be refused.
  const restartLabel = hosted ? "Resume session" : "Connect";
  let live = false;
  let liveTimer = null;
  function openSocket() {
    socket = initWs({
      allowModelChange: true,
      ...wsOptions,
      onEvent: { ...(review ? review.onEvent : {}), ...(hooks.onEvent || {}) },
      onLive: () => {
        live = true;
        idle = false;
        clearTimeout(liveTimer);
        settleResume(null);
        if (review) review.onLive();
        setButton(restartLabel, { hidden: true });
        if (hooks.onLive) hooks.onLive();
      },
      onFallback: () => {
        live = false;
        clearTimeout(liveTimer);
        if (review) review.onFallback();
        // A finite worker that ended looks like a lost connection: offer the
        // way back, which admits a new one if there is none.
        settleResume("The session could not be resumed");
        if (hosted) {
          setButton("Resume session");
          idle = false;
          // The worker reports itself unavailable as it exits, so that status
          // alone says nothing; only a worker that did not end after a
          // successful turn is a failed start.
          workerEndedNormally(3, gapMs).then((ended) => {
            if (live) return;
            if (ended) {
              idle = true;
              store.setChatAgentStatus("idle");
              store.setConnection("idle");
              return;
            }
            const c = store.getState().chat;
            if (c.agentStatus === "unavailable") {
              const why = c.banner && c.banner.text ? ": " + c.banner.text : "";
              showError("Could not start the session" + why + ". Use Resume session to retry.");
            }
          });
        }
        if (hooks.onFallback) hooks.onFallback();
      },
      onHello: (frame) => {
        chat.handleHello(frame);
        if (hooks.onHello) hooks.onHello(frame);
      },
      onAgentEvent: (event, meta) => {
        chat.handleEvent(event, meta);
        if (hooks.onAgentEvent) hooks.onAgentEvent(event, meta);
      },
      onPaused: (paused) => {
        pause.setPausedFromServer(paused);
        if (hooks.onPaused) hooks.onPaused(paused);
      },
      onRefused: (reason) => {
        chat.handleRefused(reason);
        if (hooks.onRefused) hooks.onRefused(reason);
      },
    });
  }

  let connecting = false;
  async function connect() {
    if (connecting) return;
    connecting = true;
    setButton("Starting…", { disabled: true });
    showError("");
    try {
      if (hosted) {
        await ensureSession({ url: sessionUrl || appUrl("agent/session"), fetcher });
      }
      if (!socket) openSocket();
      // Starting, disabled, until the socket is live (onLive hides it,
      // onFallback offers Resume when hosted), since a send before then is
      // refused. Same for a standalone page started from its button.
      if (live) {
        setButton(restartLabel, { hidden: true });
        idle = false;
        settleResume(null);
      } else {
        setButton("Starting…", { disabled: true });
        clearTimeout(liveTimer);
        liveTimer = setTimeout(() => {
          if (live) return;
          setButton(restartLabel);
          settleResume("The session did not come back in time");
        }, liveWaitMs);
      }
    } catch (failure) {
      showError(failure instanceof Error ? failure.message : String(failure));
      setButton("Resume connection");
      settleResume("The session could not be resumed");
    } finally {
      connecting = false;
    }
  }

  if (button) button.addEventListener("click", connect);
  if (start === "auto") connect();
  else setButton(hosted ? "Start session" : "Connect");
  return { chat, pause, review, connect, socket: () => socket };
}

/**
 * The single writer for the page's application state: the agent layer's own
 * keys (the chat pane, the connection, the pause flag) and every
 * key a product adds through `defineSlice`.
 *
 * Every other module reads state through `store.getState()` or a subscriber
 * callback and changes it only by calling a mutator: one of those exported on
 * `store` below, or one a product builds on the `update` function its
 * `defineSlice` call returned. No line outside this file ever assigns to
 * `state`, and each top-level key has exactly one owner: this file for the
 * keys declared here, the slice that declared it for a product's. `update`
 * refuses to touch a key its slice does not own, so two slices cannot both
 * write one key and a value with two writers cannot come back by accident.
 *
 * There is one state object and one notification pass for all of it, not a
 * store per slice, because modules read across the boundary (a product's
 * pause control is disabled by the chat slice's `agentStatus`) and because
 * the re-entrancy rule below only holds if every mutator, whoever owns it,
 * goes through the same queue.
 *
 * State is plain, serialisable data: numbers, strings, booleans, plain arrays
 * and plain objects only. No DOM node and no rendering-library object is ever
 * a state value; a module that draws a piece of state keeps its own side
 * table of whatever it drew and reconciles it against the store from a
 * subscriber. That is what lets an event arriving over the socket and a
 * click change state the same way, through a mutator, with no second writer
 * to reconcile against.
 *
 * State shape (this file's keys; a product documents its own slice):
 *   paused        boolean
 *                 whether the human has paused the agent's control of this
 *                 page. Written only from what the server reports (the
 *                 hello frame's `session.paused` and every `pause_changed`
 *                 event), never optimistically from the click that asked for
 *                 the change: the flag is enforced in the server, where the
 *                 tools it gates run, so a local value could show "paused"
 *                 while they were still running.
 *   connection    'connecting' | 'live' | 'polling' | 'idle' | 'refused' | 'failed'
 *                 ws.js's view of the /ws socket, read by the topbar
 *                 indicator. 'live' means updates arrive by push; 'polling'
 *                 means ws.js has handed over to the product's fallback poll
 *                 (its `onFallback` hook), whether because the socket has not
 *                 yet reconnected or because it never will again this page
 *                 load (a protocol-version mismatch); 'idle' means a hosted
 *                 worker ended normally after a finished turn and a message
 *                 resumes it; 'refused' means a pre-handshake 403 was
 *                 confirmed, most likely a stale token from a server
 *                 restart, and the fallback runs under this state too;
 *                 'failed' means the page has been visible for a while
 *                 without getting back to live, or a definite failure
 *                 (`connectionFailure` says which). 'connecting', 'live' and
 *                 'polling' are not announced: reconnecting is invisible.
 *   hostedJob     {state, reason, at}
 *                 the hosted agent job as the page's host sees it, set by
 *                 `setHostedJob`: `state` is 'none' | 'starting' | 'running' |
 *                 'succeeded' | 'failed' | 'lost', `reason` words for a failure
 *                 (or null) and `at` when the state was reached, epoch ms (or
 *                 null). A page with no hosted job leaves it at 'none'.
 *                 `agentActivity` (chat.js) reads it: a job that has ended is
 *                 never "working", whatever turn the transcript still holds
 *                 open.
 *   starting      boolean
 *                 true while the page is starting the agent to send a message
 *                 (start-on-send), set by `setStarting`.
 *   connectionFailure
 *                 null, or `{kind, message, action}` set by
 *                 `setConnectionFailure`: `kind` is 'signed_out' | 'protocol' |
 *                 'unreachable' | 'offline' | 'job_failed', `action` what the
 *                 banner offers, 'sign_in' | 'reload' | 'retry' | 'try_again'
 *                 | 'none'. chat.js draws it in the banner and dispatches
 *                 `ember:failure-action` on `window` when the button is
 *                 pressed; the host acts on it. Cleared with null.
 *   chat          {turns, pendingUser, pending, agentStatus, model, models,
 *                  banner, attachments, usage}
 *                 the whole chat pane's state.
 *                 `{turn, user, segments, errors, stopReason, costUsd, complete,
 *                 inputState, inputDetail}`, built by conversation and
 *                 input_state events. `segments` is the ordered transcript:
 *                 `{kind: "text", text}`, `{kind: "thinking", text, truncated}` or `{kind: "tools", tools}`, grouping
 *                 only adjacent activity (a tools segment is a run of one tool). Each tool has `{tool_use_id, name,
 *                 input, result}`; a result updates that original tool without
 *                 changing segment order. `user` is the blocks the human sent as
 *                 that turn, from its `user_turn` event: the one source of
 *                 a turn's human side, in the tab that sent it, every
 *                 other tab and every replay alike, so it shows once
 *                 wherever it is seen. Null for a turn with no such event
 *                 (a history from before the server logged them).
 *                 `errors` retains `{remediation, stderr}` records on their
 *                 original turn, including replay, independently of the banner.
 *                 `pendingUser` is `[{clientId, blocks, state, reason, resent,
 *                 sentAt}]`, the turns this tab has sent that the server has
 *                 neither logged (a `user_turn` with the same `client_id`)
 *                 nor refused (a `refused` frame naming it). `state` is
 *                 'sending' (the default) or 'failed' (`reason` says why; the
 *                 message was handed back to the composer and its row offers
 *                 a retry). Only 'sending' entries make the pane read as
 *                 busy. chat.js draws every entry as a "You" row at the end of
 *                 the feed from the moment of sending, and the row becomes
 *                 the confirmed one when the `user_turn` arrives. An entry
 *                 still sending when a new connection opens is settled by
 *                 that connection's replay: its user_turn retires it, and
 *                 one the replay does not have is sent once more on the new
 *                 connection with the same `client_id` (`resent`; the server
 *                 drops a duplicate).
 *                 A turn also carries `clientId`, `startedAt` (its
 *                 `user_turn`'s `at`, epoch ms, or null) and `endedAt` (its
 *                 `turn_end`'s `at`, or null); older logs have no `at`.
 *                 Every other feed item carries the server's `at` of its own
 *                 event the same way (epoch ms, or null from an older log):
 *                 a text or thinking segment the first delta's, a tool its
 *                 `tool_use`'s (and its `result.at` the `tool_result`'s), an
 *                 `errors` record its `agent_error`'s, a pending permission
 *                 request its `permission_request`'s, an `actionResults`
 *                 entry its `upload_action`'s. `pendingUser[].sentAt` is this
 *                 tab's own clock at the moment of sending.
 *                 `closed` is the last `agent_closed` event, `{reason,
 *                 detail, at}`, until the agent does anything again, or null.
 *                 `pending` is outstanding permission requests, one entry
 *                 per live `request_id`, `{request_id, tool, input,
 *                 suggestions}`; a replayed duplicate of a still-unanswered
 *                 request is not added twice.
 *                 `agentStatus` mirrors the hello frame's `session.agent`
 *                 ('connecting' | 'ready' | 'unavailable'), `agentKnown` being
 *                 false until a hello or an `agent_status` has set it (before
 *                 then 'connecting' is only the default, and `agentActivity`
 *                 shows "Not connected" rather than "Starting"). `banner` is the most
 *                 recent `session_reset`, `agent_error` or `attention` event
 *                 (or an info line the pane raised itself) as
 *                 `{kind, text, detail, source}`, or null once dismissed; an
 *                 agent error (`source: "agent"`) clears on live readiness,
 *                 input acknowledgement or model activity, not a user send.
 *                 An `attention` clears when the human's next turn arrives.
 *                 `detail` is
 *                 the backend's own text under the message (an
 *                 `agent_error`'s stderr), or null. A `session_reset`
 *                 also clears `turns` (`resetChatTurns`), because the new
 *                 session's turn numbers start over from 1.
 *                 `model` mirrors the hello frame's `session.model` (the
 *                 CLI-configured starting model, possibly null) until an
 *                 `agent_model_changed` event corrects it to whatever a live
 *                 `set_model` frame actually took effect as: the LLM
 *                 backend's active model, read and written only by the chat
 *                 pane.
 *                 `models` are the models the backend listed, for the model
 *                 field to suggest (the hello's `session.models`, then any
 *                 live `agent_models_available`); empty when it listed none.
 *                 `usage` is the conversation's usage so far, as the hello
 *                 frame's `session.usage` and each live `usage` event carry
 *                 it: `{cost_usd, tokens: {input, output, cache_read,
 *                 cache_write}, context: {used_tokens, window_tokens} |
 *                 null}`, any figure null where the backend does not say,
 *                 or null before it has said anything.
 *                 `attachments` is one entry per image attached to the
 *                 message being composed, in the order they were attached,
 *                 whatever state each is in: `[{id, kind, state, path, url,
 *                 bytes, mediaType, message}, ...]` where `state` is
 *                 'uploading' | 'done' | 'error'. The slot is appended before
 *                 the upload starts and filled in when it lands, so the order
 *                 is the human's, not the order the network happened to
 *                 answer in, and both the chips and the `image_path` blocks a
 *                 turn carries follow it. `id` is the identity, assigned here
 *                 and monotonic: `path` cannot be, since an entry has no path
 *                 until its upload has already succeeded. `path` is the
 *                 "images/<name>" string a turn frame carries and `url` is
 *                 "/asset/<name>" under the app's prefix (as the server
 *                 answered it), what the chip's thumbnail and the
 *                 sent-message thumbnail both fetch; both are null until
 *                 `state` is 'done', as `message` is until it is 'error'.
 *                 An upload in flight is therefore visible as a state on the
 *                 entry itself rather than as a separate count, which is what
 *                 keeps the cap, the strip and Send reading one record of one
 *                 fact. `uploads.js` is the only module that writes an image
 *                 entry, since it is the only module that performs an upload.
 *                 A `kind: 'context'` entry is the other sort: `{id, key,
 *                 kind, state: 'done', label, data}`, something the product
 *                 page attached (`setChatContext`/`dropChatContext`, through
 *                 chat.js's `setContext`), keyed by the product's own `key`
 *                 so it can be replaced in place; it is sent as a `context`
 *                 block and does not count towards MAX_CHAT_ATTACHMENTS.
 *
 *   toolCardsCollapsed
 *                 whether a tool card is created closed. A saved preference
 *                 read once at page load from `GET /settings`, held here
 *                 rather than in `chat.js` because `settings.js` writes it
 *                 and `chat.js` reads it, and a value two modules touch has
 *                 exactly one writer by living in this file.
 */

let state = Object.freeze({
  paused: false,
  connection: "connecting",
  toolCardsCollapsed: true,
  hostedJob: Object.freeze({ state: "none", reason: null, at: null }),
  starting: false,
  connectionFailure: null,
  chat: Object.freeze({
    turns: Object.freeze([]),
    pendingUser: Object.freeze([]),
    closed: null,
    pending: Object.freeze([]),
    agentStatus: "connecting",
    // Whether anything has said what the agent's status is (a hello or an
    // agent_status event). Until then `agentStatus` is only its default.
    agentKnown: false,
    model: null,
    models: Object.freeze([]),
    banner: null,
    attachments: Object.freeze([]),
    usage: null,
    uploadActions: Object.freeze([]),
    documents: Object.freeze([]),
    actionResults: Object.freeze([]),
  }),
});

// How many images one message may carry. Enforced in exactly one place,
// `reserveChatAttachment` below, and against uploads in flight as well as
// finished ones, since both hold a slot: every caller (the composer's own
// paste, drop and picker paths, and whatever a product attaches through
// uploads.js, such as Mesh's sketch overlay) is refused the same way, before
// its request is sent, once a message already has four.
export const MAX_CHAT_ATTACHMENTS = 4;

// Identity for an attachment slot, assigned in attach order and never reused
// within a page's lifetime. Not the server's path: a slot exists, and is
// already drawn as a chip, before any path is known.
let nextAttachmentId = 1;

const listeners = new Map(); // key (or '*') -> Set<fn(state)>

// True for the duration of a notify pass (the forEach in `apply`). Guards
// against a mutator being called synchronously from inside a subscriber,
// which is a real shape: a subscriber may re-validate its own state against
// the change it was told about and correct it from inside that same
// notification (Mesh's measure.js drops a dropdown selection whose pin has
// gone). Running that call inline would notify a second time from inside the
// first pass's own listener loop, so a listener later in the pass would
// observe a state change its earlier siblings never saw. Queueing defers it,
// so every listener sees one complete state change at a time, in the order
// the mutators were actually called, whichever slice's mutators they are.
let notifying = false;
const pending = [];

let batchDepth = 0;
const batchKeys = new Set();

// History pages are one observable change, not thousands of partial renders.
function batch(fn) {
  batchDepth++;
  try {
    fn();
  } finally {
    batchDepth--;
    if (!batchDepth && batchKeys.size) {
      const keys = [...batchKeys];
      batchKeys.clear();
      commit(() => {}, keys);
    }
  }
}

function commit(mutate, changedKeys) {
  if (notifying) {
    pending.push([mutate, changedKeys]);
    return;
  }
  apply(mutate, changedKeys);
  // Drained here, by the outermost commit, as a flat loop. A queued mutator
  // that queues another one appends to this same array rather than nesting a
  // commit inside a commit, so a long chain of re-entrant changes iterates
  // instead of growing the stack.
  while (pending.length) {
    const next = pending.shift();
    apply(next[0], next[1]);
  }
}

function apply(mutate, changedKeys) {
  mutate();
  state = Object.freeze(state);
  if (batchDepth) {
    changedKeys.forEach((key) => batchKeys.add(key));
    return;
  }
  notifying = true;
  try {
    const keys = new Set([...changedKeys, "*"]);
    keys.forEach((k) => {
      const set = listeners.get(k);
      if (set) set.forEach((fn) => fn(state));
    });
  } finally {
    notifying = false;
  }
}

function getState() {
  return state;
}

function subscribe(key, fn) {
  if (!listeners.has(key)) listeners.set(key, new Set());
  listeners.get(key).add(fn);
  return () => listeners.get(key).delete(fn);
}

// Every key declared so far, this file's and every slice's, so a second
// declaration of one is refused rather than silently giving it two owners.
const declared = new Set(Object.keys(state));

/**
 * Adds a product's slice of state and returns the one function that may
 * change it: `update(keys, fn)`.
 *
 * `initial` is a plain object of the slice's top-level keys and their
 * starting values; a key this file or an earlier slice already declared is
 * refused. Called once, from the product's own store module, at module
 * evaluation, so every key exists before any subscriber can read it.
 *
 * `update(keys, fn)` runs `fn(state)` through the same commit as every other
 * mutator, and notifies the subscribers of `keys`. `fn` returns the new
 * values of the keys it changes (a shallow patch of top-level keys), or
 * nothing to leave state as it is; subscribers of `keys` are notified either
 * way, as they are for this file's own mutators. Every key in `keys` and in
 * the patch must belong to this slice, and every patched key must be named
 * in `keys`, since a key changed without being named would leave its own
 * subscribers unaware of the change.
 */
export function defineSlice(initial) {
  const owned = new Set(Object.keys(initial));
  owned.forEach((key) => {
    if (declared.has(key)) throw new Error("store key " + JSON.stringify(key) + " is already declared");
  });
  owned.forEach((key) => declared.add(key));
  state = Object.freeze({ ...state, ...initial });

  return function update(keys, fn) {
    keys.forEach((key) => {
      if (!owned.has(key)) throw new Error("store key " + JSON.stringify(key) + " is not this slice's");
    });
    commit(() => {
      const patch = fn(state);
      if (!patch) return;
      Object.keys(patch).forEach((key) => {
        if (!keys.includes(key)) {
          throw new Error("store key " + JSON.stringify(key) + " changed without being notified");
        }
      });
      state = { ...state, ...patch };
    }, keys);
  };
}

function setToolCardsCollapsed(collapsed) {
  commit(() => {
    state = { ...state, toolCardsCollapsed: !!collapsed };
  }, ["toolCardsCollapsed"]);
}

function setConnection(v) {
  commit(() => {
    state = { ...state, connection: v };
  }, ["connection"]);
}

const HOSTED_JOB_STATES = new Set(["none", "starting", "running", "succeeded", "failed", "lost"]);

function setHostedJob({ state: jobState = "none", reason = null, at = null } = {}) {
  const next = Object.freeze({
    state: HOSTED_JOB_STATES.has(jobState) ? jobState : "none",
    reason: reason || null,
    at: typeof at === "number" && Number.isFinite(at) ? at : null,
  });
  const now = state.hostedJob;
  if (now.state === next.state && now.reason === next.reason && now.at === next.at) return;
  commit(() => {
    state = { ...state, hostedJob: next };
  }, ["hostedJob"]);
}

function setStarting(starting) {
  if (state.starting === !!starting) return;
  commit(() => {
    state = { ...state, starting: !!starting };
  }, ["starting"]);
}

function setConnectionFailure(failure) {
  const next = failure
    ? Object.freeze({
      kind: String(failure.kind),
      message: String(failure.message || ""),
      action: failure.action || "none",
    })
    : null;
  const now = state.connectionFailure;
  if (now === next || (now && next && now.kind === next.kind
      && now.message === next.message && now.action === next.action)) return;
  commit(() => {
    state = { ...state, connectionFailure: next };
  }, ["connectionFailure"]);
}

function setPaused(v) {
  commit(() => {
    state = { ...state, paused: !!v };
  }, ["paused"]);
}

// Finds the turn record for `turn` in `chat.turns`, or builds an empty one at
// the end. Callers must fold the returned `turns` back into a new chat object
// themselves; this only computes the array, it does not touch `state`.
function ensureTurn(chat, turn) {
  const idx = chat.turns.findIndex((t) => t.turn === turn);
  if (idx !== -1) {
    return { turns: chat.turns, idx };
  }
  const record = Object.freeze({
    turn,
    user: null,
    segments: Object.freeze([]),
    errors: Object.freeze([]),
    stopReason: null,
    costUsd: null,
    usageEpoch: null,
    usageSource: null,
    complete: turn === 0,
    clientId: null,
    startedAt: null,
    endedAt: null,
    inputState: null,
    inputDetail: null,
  });
  const next = [...chat.turns];
  const insertion = next.findIndex((t) => t.turn > turn);
  const at = insertion === -1 ? next.length : insertion;
  next.splice(at, 0, record);
  return { turns: Object.freeze(next), idx: at };
}

function setChatInputState(turn, stateName, detail = null) {
  commit(() => {
    const chat = state.chat;
    const { turns, idx } = ensureTurn(chat, turn);
    const nextTurns = Object.freeze(
      turns.map((t, i) => (
        i === idx
          ? Object.freeze({ ...t, inputState: stateName, inputDetail: detail })
          : t
      )),
    );
    state = { ...state, chat: Object.freeze({ ...chat, turns: nextTurns }) };
  }, ["chat"]);
}

// `pendingUser` without the entry for `clientId`, or unchanged when it has
// none (another tab's message, or one already retired).
function withoutPending(pendingUser, clientId) {
  const kept = pendingUser.filter((p) => p.clientId !== clientId);
  return kept.length === pendingUser.length ? pendingUser : Object.freeze(kept);
}

// A `user_turn` event: a human-authored turn or an explicitly sourced
// automatic turn. Its origin is retained beside the blocks through replay.
function setChatUserTurn(
  turn, blocks, clientId = null, automationSource = null, redeliveryOf = null, at = null,
) {
  commit(() => {
    const chat = state.chat;
    const { turns, idx } = ensureTurn(chat, turn);
    const nextTurns = Object.freeze(
      turns.map((t, i) => (i === idx
        ? Object.freeze({
          ...t, user: blocks, automationSource, redeliveryOf, clientId,
          startedAt: typeof at === "number" ? at : t.startedAt,
        })
        : t)),
    );
    const pendingUser = withoutPending(chat.pendingUser, clientId);
    state = { ...state, chat: Object.freeze({ ...chat, turns: nextTurns, pendingUser }) };
  }, ["chat"]);
}

// A server `at`, or null when the event carried none (an older log).
function stampOf(at) {
  return typeof at === "number" && Number.isFinite(at) ? at : null;
}

// Adjacent activity of the same kind extends its existing segment; tool calls
// only extend a run of the same tool, so a different tool starts its own
// segment (and its own line in the chat). Results never enter this stream:
// they update the original tool at its original slot. A merged segment keeps
// the `at` of its first piece.
function appendSegment(segments, segment) {
  const last = segments[segments.length - 1];
  if (last?.kind !== segment.kind
      || (segment.kind === "tools"
        && last.tools[last.tools.length - 1].name !== segment.tools[0].name)) {
    return Object.freeze([...segments, Object.freeze(segment)]);
  }
  const merged = segment.kind === "text"
    ? { kind: "text", text: last.text + segment.text, at: last.at }
    : segment.kind === "thinking"
      ? { kind: "thinking", text: last.text + segment.text, truncated: last.truncated || segment.truncated, at: last.at }
      : { kind: "tools", tools: Object.freeze([...last.tools, ...segment.tools]) };
  return Object.freeze([...segments.slice(0, -1), Object.freeze(merged)]);
}

function appendChatTextDelta(turn, text, at = null) {
  if (!text) return;
  commit(() => {
    const chat = state.chat;
    const { turns, idx } = ensureTurn(chat, turn);
    const nextTurns = Object.freeze(
      turns.map((t, i) => (i === idx ? Object.freeze({
        ...t, segments: appendSegment(t.segments, { kind: "text", text, at: stampOf(at) }),
      }) : t)),
    );
    state = { ...state, chat: Object.freeze({ ...chat, turns: nextTurns }) };
  }, ["chat"]);
}

// Thinking is its own segment kind so it keeps its place between text and
// tool runs; `truncated` is sticky once the server says the cap was hit.
function appendChatThinkingDelta(turn, text, truncated = false, at = null) {
  if (!text && !truncated) return;
  commit(() => {
    const chat = state.chat;
    const { turns, idx } = ensureTurn(chat, turn);
    const nextTurns = Object.freeze(
      turns.map((t, i) => (i === idx ? Object.freeze({
        ...t,
        segments: appendSegment(t.segments, { kind: "thinking", text: text || "", truncated, at: stampOf(at) }),
      }) : t)),
    );
    state = { ...state, chat: Object.freeze({ ...chat, turns: nextTurns }) };
  }, ["chat"]);
}

// Kept on its original turn even after the active banner has recovered.
// Reconnecting replay may revisit an error already shown in this tab.
function addChatTurnError(turn, remediation, stderr, at = null) {
  commit(() => {
    const chat = state.chat;
    const { turns, idx } = ensureTurn(chat, turn);
    const errors = turns[idx].errors;
    if (errors.some((e) => e.remediation === remediation && e.stderr === stderr)) return;
    const error = Object.freeze({ remediation, stderr, at: stampOf(at) });
    const nextTurns = Object.freeze(turns.map((t, i) => (
      i === idx ? Object.freeze({ ...t, errors: Object.freeze([...errors, error]) }) : t
    )));
    state = { ...state, chat: Object.freeze({ ...chat, turns: nextTurns }) };
  }, ["chat"]);
}

function addChatToolUse(turn, toolUseId, name, input, at = null) {
  commit(() => {
    const chat = state.chat;
    const { turns, idx } = ensureTurn(chat, turn);
    if (turns[idx].segments.some((segment) => segment.kind === "tools"
        && segment.tools.some((tool) => tool.tool_use_id === toolUseId))) return;
    const tool = Object.freeze({ tool_use_id: toolUseId, name, input, result: null, at: stampOf(at) });
    const nextTurns = Object.freeze(
      turns.map((t, i) =>
        i === idx ? Object.freeze({
          ...t,
          segments: appendSegment(t.segments, { kind: "tools", tools: Object.freeze([tool]) }),
        }) : t,
      ),
    );
    state = { ...state, chat: Object.freeze({ ...chat, turns: nextTurns }) };
  }, ["chat"]);
}

// A result updates only the segment that owns its tool_use_id, even after
// later text, another tool run, or a newer turn has arrived.
function setChatToolResult(toolUseId, isError, text, at = null) {
  commit(() => {
    const chat = state.chat;
    const turns = Object.freeze(
      chat.turns.map((t) => {
        let changed = false;
        const segments = t.segments.map((segment) => {
          if (segment.kind !== "tools"
              || !segment.tools.some((tool) => tool.tool_use_id === toolUseId)) return segment;
          changed = true;
          const tools = Object.freeze(segment.tools.map((tool) => (
            tool.tool_use_id === toolUseId
              ? Object.freeze({ ...tool, result: Object.freeze({ isError: !!isError, text, at: stampOf(at) }) })
              : tool
          )));
          return Object.freeze({ ...segment, tools });
        });
        return changed ? Object.freeze({ ...t, segments: Object.freeze(segments) }) : t;
      }),
    );
    state = { ...state, chat: Object.freeze({ ...chat, turns }) };
  }, ["chat"]);
}

function endChatTurn(
  turn, stopReason, costUsd, tokens = null, usageEpoch = null, usageSource = null, at = null,
) {
  commit(() => {
    const chat = state.chat;
    const { turns, idx } = ensureTurn(chat, turn);
    const nextTurns = Object.freeze(
      turns.map((t, i) => (
        i === idx
          ? Object.freeze({
            ...t,
            stopReason,
            costUsd,
            tokens,
            usageEpoch,
            usageSource,
            complete: true,
            endedAt: typeof at === "number" ? at : t.endedAt,
          })
          : t
      )),
    );
    state = { ...state, chat: Object.freeze({ ...chat, turns: nextTurns }) };
  }, ["chat"]);
}

// Old full-turn pages may predate turn_end (or include abandoned queued input).
// Such input is transcript, never a new running turn.
function completeChatHistory(turnNumbers) {
  commit(() => {
    const chat = state.chat;
    const turns = Object.freeze(chat.turns.map((turn) => (
      turnNumbers.has(turn.turn) && !turn.complete
        ? Object.freeze({ ...turn, complete: true }) : turn
    )));
    state = { ...state, chat: Object.freeze({ ...chat, turns }) };
  }, ["chat"]);
}

// A turn this tab has just sent, held until the server logs or refuses it.
function queueChatUserTurn(clientId, blocks) {
  commit(() => {
    const chat = state.chat;
    const entry = Object.freeze({
      clientId, blocks, state: "sending", reason: null, resent: false, sentAt: Date.now(),
    });
    state = {
      ...state,
      chat: Object.freeze({ ...chat, pendingUser: Object.freeze([...chat.pendingUser, entry]) }),
    };
  }, ["chat"]);
}

// Replaces the entry for `clientId` with `change(entry)`.
function changePending(clientId, change) {
  commit(() => {
    const chat = state.chat;
    const pendingUser = Object.freeze(chat.pendingUser.map(
      (p) => (p.clientId === clientId ? Object.freeze({ ...p, ...change(p) }) : p)));
    state = { ...state, chat: Object.freeze({ ...chat, pendingUser }) };
  }, ["chat"]);
}

// A turn that will not appear (the server refused it, or a new connection's
// replay shows it never arrived): its pending entry goes, and its blocks come
// back to the caller (null when this tab holds no such entry), so the message
// can be offered again rather than lost.
function dropChatUserTurn(clientId) {
  const entry = state.chat.pendingUser.find((p) => p.clientId === clientId) || null;
  if (entry) {
    commit(() => {
      const chat = state.chat;
      state = {
        ...state,
        chat: Object.freeze({ ...chat, pendingUser: withoutPending(chat.pendingUser, clientId) }),
      };
    }, ["chat"]);
  }
  return entry ? entry.blocks : null;
}

// A turn the server will not take (refused, or its start failed): the entry
// stays, marked failed with `reason`, so its row can say "Not sent" and offer a
// retry. Its blocks come back to the caller, null when this tab holds no such
// entry (or it had already failed), so the message can go back in the composer.
function failChatUserTurn(clientId, reason) {
  const entry = state.chat.pendingUser.find((p) => p.clientId === clientId) || null;
  if (!entry || entry.state === "failed") return null;
  changePending(clientId, () => ({ state: "failed", reason: reason || null }));
  return entry.blocks;
}

// The entry was sent once more on a new connection.
function markChatUserTurnResent(clientId) {
  changePending(clientId, () => ({ resent: true }));
}

// Failed entries go (another conversation's, or dismissed).
function clearChatFailedUserTurns() {
  if (!state.chat.pendingUser.some((p) => p.state === "failed")) return;
  commit(() => {
    const chat = state.chat;
    const pendingUser = Object.freeze(chat.pendingUser.filter((p) => p.state !== "failed"));
    state = { ...state, chat: Object.freeze({ ...chat, pendingUser }) };
  }, ["chat"]);
}

// The agent session closed (`agent_closed`), or null once it does anything.
function setChatClosed(closed) {
  if (!closed && !state.chat.closed) return;
  commit(() => {
    state = {
      ...state,
      chat: Object.freeze({
        ...state.chat,
        closed: closed ? Object.freeze({
          reason: closed.reason || "other", detail: closed.detail || null,
          at: typeof closed.at === "number" ? closed.at : null,
        }) : null,
      }),
    };
  }, ["chat"]);
}

// Adds a permission_request to the pending list unless its request_id is
// already there. Replay re-emits every still-unanswered request on
// reconnect, so this call is not proof of a first sighting.
// `rememberable` is false for a request the server will never remember an
// "always allow" for, so its card offers no such button. `origin`, for a call
// the human started from the page (an upload action), is `{action, by}`: the
// action's label and the login of who started it (null for the token holder).
function addChatPermissionRequest(
  requestId, tool, input, suggestions, rememberable = true, origin = null, at = null,
) {
  commit(() => {
    const chat = state.chat;
    if (chat.pending.some((p) => p.request_id === requestId)) return;
    const entry = Object.freeze({
      request_id: requestId,
      tool,
      input,
      suggestions: suggestions || null,
      rememberable: rememberable !== false,
      origin: origin ? Object.freeze({ ...origin }) : null,
      at: stampOf(at),
    });
    state = {
      ...state,
      chat: Object.freeze({ ...chat, pending: Object.freeze([...chat.pending, entry]) }),
    };
  }, ["chat"]);
}

// Records that this view has sent a decision for `requestId` and is waiting to
// hear that it landed. The card stays on screen, showing that it is in flight,
// because it is the server's `permission_resolved` that says a request is over,
// and only that event distinguishes a decision that took effect from one
// another view had already answered.
// Every card answerable again (a new connection: see chat.js's handleHello).
function clearChatPermissionSubmitted() {
  commit(() => {
    const chat = state.chat;
    if (!chat.pending.some((p) => p.submitted)) return;
    const pending = Object.freeze(chat.pending.map(
      (p) => (p.submitted ? Object.freeze({ ...p, submitted: null }) : p)));
    state = { ...state, chat: Object.freeze({ ...chat, pending }) };
  }, ["chat"]);
}

// No card at all (another conversation's were showing).
function clearChatPending() {
  commit(() => {
    state = { ...state, chat: Object.freeze({ ...state.chat, pending: Object.freeze([]) }) };
  }, ["chat"]);
}

function markChatPermissionSubmitted(requestId, decision) {
  commit(() => {
    const chat = state.chat;
    let changed = false;
    const pending = Object.freeze(
      chat.pending.map((p) => {
        if (p.request_id !== requestId || p.submitted) return p;
        changed = true;
        return Object.freeze({ ...p, submitted: decision });
      }),
    );
    if (!changed) return;
    state = { ...state, chat: Object.freeze({ ...chat, pending }) };
  }, ["chat"]);
}

function removeChatPermissionRequest(requestId) {
  commit(() => {
    const chat = state.chat;
    const pending = Object.freeze(chat.pending.filter((p) => p.request_id !== requestId));
    state = { ...state, chat: Object.freeze({ ...chat, pending }) };
  }, ["chat"]);
}


function setChatAgentStatus(status) {
  commit(() => {
    state = {
      ...state,
      chat: Object.freeze({ ...state.chat, agentStatus: status, agentKnown: true }),
    };
  }, ["chat"]);
}

function setChatModel(model) {
  commit(() => {
    state = { ...state, chat: Object.freeze({ ...state.chat, model: model || null }) };
  }, ["chat"]);
}

function setChatModels(models) {
  commit(() => {
    state = { ...state, chat: Object.freeze({ ...state.chat, models: Object.freeze([...(models || [])]) }) };
  }, ["chat"]);
}

function setChatUsage(usage) {
  commit(() => {
    state = { ...state, chat: Object.freeze({ ...state.chat, usage: usage || null }) };
  }, ["chat"]);
}

function setChatBanner(kind, text, detail = null, source = null) {
  commit(() => {
    const banner = Object.freeze({ kind, text, detail: detail || null, source });
    state = { ...state, chat: Object.freeze({ ...state.chat, banner }) };
  }, ["chat"]);
}

function clearChatBanner() {
  commit(() => {
    state = { ...state, chat: Object.freeze({ ...state.chat, banner: null }) };
  }, ["chat"]);
}

// Empties `turns`. Called when a session_reset event reports the SDK started
// a fresh session, and when a new connection's hello names a different
// session than the last one: either way the turns shown belong to another
// conversation, and leaving them risks a later ensureTurn call finding a
// stale record under the same turn number and folding new content into it.
// `pendingUser` is left alone: its entries are matched by client id, not by
// turn number.
function resetChatTurns() {
  commit(() => {
    state = {
      ...state,
      chat: Object.freeze({
        ...state.chat, turns: Object.freeze([]), actionResults: Object.freeze([]),
      }),
    };
  }, ["chat"]);
}

// The app's upload actions, from the hello: `[{name, label, accepts}]`.
function setChatUploadActions(actions) {
  commit(() => {
    state = {
      ...state,
      chat: Object.freeze({ ...state.chat, uploadActions: Object.freeze([...(actions || [])]) }),
    };
  }, ["chat"]);
}

// A document uploaded for an upload action, in `documents`: `{id, name,
// state, upload, bytes, actions, message}`, `state` 'uploading' | 'done' |
// 'sent' | 'error'. Never part of a turn; the human starts an action on it.
function reserveChatDocument(name) {
  const id = nextAttachmentId++;
  commit(() => {
    const entry = Object.freeze({
      id, name, state: "uploading", upload: null, bytes: null, actions: [], message: null,
    });
    state = {
      ...state,
      chat: Object.freeze({ ...state.chat, documents: Object.freeze([...state.chat.documents, entry]) }),
    };
  }, ["chat"]);
  return id;
}

function updateChatDocument(id, fields) {
  commit(() => {
    const documents = Object.freeze(state.chat.documents.map(
      (d) => (d.id === id ? Object.freeze({ ...d, ...fields }) : d)));
    state = { ...state, chat: Object.freeze({ ...state.chat, documents }) };
  }, ["chat"]);
}

// An upload action on the document the server knows as `upload` ended:
// the document is gone from the server, so it offers no action any more,
// and `message` says how it ended.
function endChatDocument(upload, message) {
  commit(() => {
    const documents = Object.freeze(state.chat.documents.map((d) => (
      d.upload === upload ? Object.freeze({ ...d, state: "ended", message }) : d)));
    state = { ...state, chat: Object.freeze({ ...state.chat, documents }) };
  }, ["chat"]);
}

function dropChatDocument(id) {
  commit(() => {
    const documents = Object.freeze(state.chat.documents.filter((d) => d.id !== id));
    state = { ...state, chat: Object.freeze({ ...state.chat, documents }) };
  }, ["chat"]);
}

// How an upload action ended (an `upload_action` event), once per id: part
// of the conversation, so a replay adds what a live event already added only
// once.
function addChatActionResult(event) {
  commit(() => {
    const chat = state.chat;
    if (chat.actionResults.some((r) => r.id === event.id)) return;
    const entry = Object.freeze({
      id: event.id, label: event.label, file: event.file, bytes: event.bytes,
      tool: event.tool, outcome: event.outcome, text: event.text || "", by: event.by || null,
      at: stampOf(event.at),
    });
    state = {
      ...state,
      chat: Object.freeze({ ...chat, actionResults: Object.freeze([...chat.actionResults, entry]) }),
    };
  }, ["chat"]);
}

// Appends an entry for an upload about to start, in the 'uploading' state,
// and returns its id, or returns null and changes nothing when the message
// already carries MAX_CHAT_ATTACHMENTS. Reserving the slot before the request
// rather than on its response is what keeps the strip and the sent blocks in
// the order the human attached things, and what lets the cap refuse a file
// before a byte leaves the page or a file is written into the project.
function reserveChatAttachment(kind) {
  let id = null;
  commit(() => {
    const chat = state.chat;
    if (imageCount(chat.attachments) >= MAX_CHAT_ATTACHMENTS) return;
    id = nextAttachmentId++;
    const entry = Object.freeze({
      id,
      kind,
      state: "uploading",
      path: null,
      url: null,
      bytes: null,
      mediaType: null,
      message: null,
    });
    state = {
      ...state,
      chat: Object.freeze({ ...chat, attachments: Object.freeze([...chat.attachments, entry]) }),
    };
  }, ["chat"]);
  return id;
}

// Fills a reserved entry in place, so it keeps the position it was attached
// at. An id no longer present (the human removed the chip, or sent the
// message, while the upload was still in flight) matches nothing and changes
// nothing, rather than reappearing: the bytes are on the server either way,
// but a turn references only what is still in this list when it is sent.
function updateChatAttachment(id, fields) {
  commit(() => {
    const chat = state.chat;
    const attachments = Object.freeze(chat.attachments.map(
      (a) => (a.id === id ? Object.freeze({ ...a, ...fields }) : a)));
    state = { ...state, chat: Object.freeze({ ...chat, attachments }) };
  }, ["chat"]);
}

function completeChatAttachment(id, { path, url, bytes, mediaType }) {
  updateChatAttachment(id, { state: "done", path, url, bytes, mediaType });
}

function failChatAttachment(id, message) {
  updateChatAttachment(id, { state: "error", message });
}

function dropChatAttachment(id) {
  commit(() => {
    const chat = state.chat;
    const attachments = Object.freeze(chat.attachments.filter((a) => a.id !== id));
    state = { ...state, chat: Object.freeze({ ...chat, attachments }) };
  }, ["chat"]);
}

function clearChatAttachments() {
  commit(() => {
    state = { ...state, chat: Object.freeze({ ...state.chat, attachments: Object.freeze([]) }) };
  }, ["chat"]);
}

// How many of `attachments` are images, which MAX_CHAT_ATTACHMENTS caps: a
// context entry is a few hundred bytes of JSON, not a picture the model is sent.
function imageCount(attachments) {
  return attachments.filter((a) => a.kind !== "context").length;
}

// Adds a context attachment under the product's own `key`, or replaces the
// one already under it in place (its position and id kept), so a page can
// keep one chip in step with what is selected. `label` is the chip's text and
// `data` the product's JSON, sent as the message's `context` block.
function setChatContext(key, label, data) {
  commit(() => {
    const chat = state.chat;
    const existing = chat.attachments.find((a) => a.kind === "context" && a.key === key);
    const entry = Object.freeze({
      id: existing ? existing.id : nextAttachmentId++,
      key,
      kind: "context",
      state: "done",
      label,
      data,
    });
    const attachments = existing
      ? chat.attachments.map((a) => (a === existing ? entry : a))
      : [...chat.attachments, entry];
    state = { ...state, chat: Object.freeze({ ...chat, attachments: Object.freeze(attachments) }) };
  }, ["chat"]);
}

function dropChatContext(key) {
  commit(() => {
    const chat = state.chat;
    if (!chat.attachments.some((a) => a.kind === "context" && a.key === key)) return;
    const attachments = Object.freeze(
      chat.attachments.filter((a) => !(a.kind === "context" && a.key === key)));
    state = { ...state, chat: Object.freeze({ ...chat, attachments }) };
  }, ["chat"]);
}

export const store = Object.freeze({
  getState,
  subscribe,
  batch,
  setToolCardsCollapsed,
  setConnection,
  setHostedJob,
  setStarting,
  setConnectionFailure,
  setPaused,
  setChatUserTurn,
  setChatInputState,
  appendChatTextDelta,
  appendChatThinkingDelta,
  addChatTurnError,
  addChatToolUse,
  setChatToolResult,
  endChatTurn,
  completeChatHistory,
  queueChatUserTurn,
  dropChatUserTurn,
  failChatUserTurn,
  markChatUserTurnResent,
  clearChatFailedUserTurns,
  setChatClosed,
  addChatPermissionRequest,
  markChatPermissionSubmitted,
  clearChatPermissionSubmitted,
  clearChatPending,
  removeChatPermissionRequest,
  setChatAgentStatus,
  setChatModel,
  setChatModels,
  setChatUsage,
  setChatUploadActions,
  reserveChatDocument,
  updateChatDocument,
  dropChatDocument,
  endChatDocument,
  addChatActionResult,
  setChatBanner,
  clearChatBanner,
  resetChatTurns,
  reserveChatAttachment,
  completeChatAttachment,
  failChatAttachment,
  dropChatAttachment,
  clearChatAttachments,
  setChatContext,
  dropChatContext,
});

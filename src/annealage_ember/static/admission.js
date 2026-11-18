/**
 * Getting a hosted agent session to exist before the page connects to it.
 *
 * A hosted front door runs the agent as a finite worker: a project has no
 * session until one is admitted, and one ends when its worker does. The
 * session route answers a GET with the session's state:
 *
 *   200                                  a worker is up; connect.
 *   404 {error: {type: "no_job"}}        no session yet.
 *   503 {error: {type: "job_ended"}}     the last worker finished.
 *   503 {error: {type: "worker_unavailable"}, Retry-After}
 *                                        admitted, still starting.
 *
 * Only the first two 4xx/5xx kinds are answered by a POST. The rules:
 *
 *   - A POST with a lost or failed response is never repeated: it may have
 *     started a worker. The person can use Resume to check.
 *   - A session that already exists is read, not admitted: no POST.
 *   - Everything after the POST is a GET, bounded in count and by one
 *     overall deadline, and it waits exactly as long as the server's
 *     `Retry-After` says (seconds or an HTTP date): never less, since polling
 *     early is the load the header exists to prevent. A wait that would end
 *     after the deadline is refused up front rather than started.
 *   - Every request is bounded by what is left of that deadline (aborted, and
 *     given up on even if the fetch ignores the abort), the POST included: an
 *     aborted POST is as ambiguous as a lost one, so it is reported, never
 *     repeated.
 *   - A 409 on the POST started nothing. A busy job is retried after a delay;
 *     `settings_changed` fetches a fresh preview before retrying. Both are
 *     bounded by BUSY_RETRIES and the deadline.
 *   - A refusal (401, 403) ends the call before any mutation.
 */

export const POLL_LIMIT = 30;
export const DEADLINE_MS = 60000;
// A 409 on the POST is `busy`: the last job is still publishing, nothing
// started, so asking again is safe, a couple of times and not for long.
export const BUSY_RETRIES = 2;
const BUSY_GAP_MS = 1500;
const MIN_DELAY_MS = 100;
const DEFAULT_DELAY_MS = 1000;

class Expired extends Error {}

// Retry-After as milliseconds: delay-seconds or an HTTP date (RFC 9110
// 10.2.3); null when absent or not either.
export function retryAfterMs(value, now) {
  if (value === null || value === undefined || String(value).trim() === "") return null;
  const text = String(value).trim();
  if (/^\d+(\.\d+)?$/.test(text)) return Number(text) * 1000;
  const when = Date.parse(text);
  return Number.isNaN(when) ? null : Math.max(0, when - now());
}

/**
 * Resolves once a worker is up; rejects with an Error whose message is for
 * the person (the page shows it) otherwise.
 *
 * `url` is the session route, already resolved against whatever base and
 * prefix the page lives under. `settingsPreviewUrl` opts into hosted resolved
 * settings; its snapshot hash is bound to each admission. `fetcher` carries
 * the page's auth and is given `signal`. `sleep`, `now` and `after(fn, ms)`
 * (which returns a cancel function) are the clock, for tests.
 */
export async function ensureHostedSession({
  url,
  settingsPreviewUrl = null,
  fetcher = (...args) => globalThis.fetch(...args),
  sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
  now = () => Date.now(),
  after = (fn, ms) => {
    const id = setTimeout(fn, ms);
    return () => clearTimeout(id);
  },
  pollLimit = POLL_LIMIT,
  deadlineMs = DEADLINE_MS,
}) {
  const deadline = now() + deadlineMs;

  // One request and the reading of its body, abandoned at the deadline.
  async function request(init, requestUrl = url) {
    const left = deadline - now();
    if (left <= 0) throw new Expired();
    const controller = new AbortController();
    let cancel = () => {};
    const expiry = new Promise((_, reject) => {
      cancel = after(() => {
        controller.abort();
        reject(new Expired());
      }, left);
    });
    try {
      return await Promise.race([
        (async () => {
          const response = await fetcher(requestUrl, { ...init, signal: controller.signal });
          const body = await response.clone().json().catch(() => null);
          return { response, body, kind: body && body.error && body.error.type };
        })(),
        expiry,
      ]);
    } finally {
      cancel();
    }
  }
  const read = () => request({ cache: "no-store" });
  const refused = (status) => new Error("You are not signed in to this project (" + status + ").");
  const stillStarting = () => new Error("The agent is still starting; use Resume session to check again.");

  let current;
  try {
    current = await read();
  } catch (failure) {
    if (failure instanceof Expired) throw stillStarting();
    throw failure;
  }
  let { response, kind } = current;
  if (response.status === 401 || response.status === 403) throw refused(response.status);
  if ((response.status === 404 && kind === "no_job") || (response.status === 503 && kind === "job_ended")) {
    let admitted;
    let snapshotHash = null;
    async function preview() {
      const result = await request({ cache: "no-store" }, settingsPreviewUrl);
      if (result.response.status === 401 || result.response.status === 403) throw refused(result.response.status);
      if (!result.response.ok || typeof result.body?.snapshot_hash !== "string" || !result.body.snapshot_hash) {
        throw new Error("Could not read agent settings; your message has not been sent.");
      }
      return result.body.snapshot_hash;
    }
    if (settingsPreviewUrl) snapshotHash = await preview();
    for (let busy = 0; ; busy++) {
      try {
        admitted = await request({
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify(snapshotHash === null ? {} : { settings_snapshot_hash: snapshotHash }),
        });
      } catch (failure) {
        throw new Error(
          "The agent session request failed and was not repeated; it may or may not have started. "
          + "Use Resume session to check.",
        );
      }
      if (admitted.response.status === 409 && admitted.kind === "settings_changed") {
        if (!settingsPreviewUrl || busy >= BUSY_RETRIES) break;
        snapshotHash = await preview();
        continue;
      }
      if (admitted.response.status !== 409 || busy >= BUSY_RETRIES) break;
      if (now() + BUSY_GAP_MS > deadline) throw stillStarting();
      await sleep(BUSY_GAP_MS);
    }
    if (admitted.response.status === 401 || admitted.response.status === 403) {
      throw refused(admitted.response.status);
    }
    if (admitted.response.status !== 202) {
      throw new Error("Agent session was not started (" + admitted.response.status + ").");
    }
    try {
      ({ response, kind } = await read());
    } catch (failure) {
      if (failure instanceof Expired) throw stillStarting();
      throw failure;
    }
  }
  for (let attempt = 0; !response.ok; attempt++) {
    if (response.status === 401 || response.status === 403) throw refused(response.status);
    if (response.status !== 503 || kind !== "worker_unavailable" || attempt >= pollLimit) {
      throw new Error("Agent session is unavailable (" + response.status + "); your message has not been sent.");
    }
    const asked = retryAfterMs(response.headers.get("Retry-After"), now);
    const delay = Math.max(MIN_DELAY_MS, asked === null ? DEFAULT_DELAY_MS : asked);
    if (now() + delay > deadline) throw stillStarting();
    await sleep(delay);
    try {
      ({ response, kind } = await read());
    } catch (failure) {
      if (failure instanceof Expired) throw stillStarting();
      throw failure;
    }
  }
}

/**
 * Toast and the error banner: two small pieces of DOM feedback any module
 * uses, the agent layer's (an upload refused, a stale link) and the
 * product's. They write to the page's `#toast` and `#err` elements, which the
 * page (or its theme) positions, since only it knows what they must stay clear of.
 */

/** Transient toast, auto-dismissed after 3.5s. */
export function toast(msg, ok) {
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.className = ok ? "ok" : "err";
  t.style.display = "block";
  clearTimeout(toast._t);
  toast._t = setTimeout(() => {
    t.style.display = "none";
  }, 3500);
}

/** Persistent error line, for failures the user should keep seeing. */
export function showError(msg) {
  const e = document.getElementById("err");
  e.textContent = msg;
  e.style.display = "block";
}

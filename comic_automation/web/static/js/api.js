// Fetch wrapper for the console API (design §10, §11). It sends the session's
// browser key on every request and the CSRF token on every state-changing one,
// and turns error bodies of the form {error, message, details} into thrown
// ApiError objects.
let csrfToken = null;

// The browser key arrives once, in the sign-in redirect's fragment (#k=...).
// Fragments never reach a server or its log. Move it to this tab's
// sessionStorage and take it out of the address bar and history entry.
const KEY_SLOT = "cbz-console-key";
const fromFragment = new URLSearchParams(location.hash.slice(1)).get("k");
if (fromFragment) {
  sessionStorage.setItem(KEY_SLOT, fromFragment);
  history.replaceState(null, "", location.pathname + location.search);
}
const browserKey = sessionStorage.getItem(KEY_SLOT);

export class ApiError extends Error {
  constructor(status, code, message, details) {
    super(message || code || `HTTP ${status}`);
    this.status = status;
    this.code = code;
    this.details = details || {};
  }
}

export async function api(method, path, body) {
  const headers = { Accept: "application/json" };
  if (browserKey) headers["X-Session-Key"] = browserKey;
  const init = { method, headers, credentials: "same-origin" };
  if (method !== "GET" && method !== "HEAD") {
    headers["Content-Type"] = "application/json";
    if (csrfToken) headers["X-CSRF-Token"] = csrfToken;
    init.body = JSON.stringify(body ?? {});
  }
  const response = await fetch(path, init);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new ApiError(response.status, data.error, data.message, data.details);
  return data;
}

// Loads the session (operator, CSRF token, server version). Call once at start.
export async function loadSession() {
  const session = await api("GET", "/api/session");
  csrfToken = session.csrf_token;
  return session;
}

/**
 * A password login: a single password field, a session cookie that is an
 * HMAC-signed expiry, and the signing key derived from the password itself so
 * that changing the password ends every session without needing anywhere to
 * store them.
 *
 * This replaced a secret token in the URL path. That worked, but it is the
 * weakest form of access control there is: it cannot be revoked for one device,
 * it leaks through browser history and through any link ever shared, and it made
 * the bare address useless. A password can be changed, a session can be ended,
 * and the front door is the front door.
 *
 * Two choices worth noting:
 *   - failure counts live in D1 rather than KV, because D1 is already bound here
 *     and a second binding for one counter is not worth it;
 *   - machine callers use a bearer token instead of a session, because the local
 *     `signal pull` has no browser and must not be given the password.
 */

const SESSION_DAYS = 30;
const SESSION_COOKIE = "sigarb_session";
const FAIL_LIMIT = 8;
const FAIL_WINDOW_S = 900;
// A wrong password answers slowly, so guessing costs wall-clock time as well as
// attempts. Well inside the request budget and outside the CPU budget, since
// sleeping is not compute.
const WRONG_PASSWORD_DELAY_MS = 1000;

const enc = new TextEncoder();

async function hmacKey(password) {
  const material = await crypto.subtle.digest("SHA-256", enc.encode(`${password}|session-v1`));
  return crypto.subtle.importKey(
    "raw", material, { name: "HMAC", hash: "SHA-256" }, false, ["sign", "verify"],
  );
}

const hex = (buf) => [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");

export async function signSession(password, expiry) {
  const key = await hmacKey(password);
  const signature = await crypto.subtle.sign("HMAC", key, enc.encode(String(expiry)));
  return `${expiry}.${hex(signature)}`;
}

export async function validSession(password, cookieHeader) {
  if (!password) return false;
  const match = new RegExp(`(?:^|;\\s*)${SESSION_COOKIE}=([^;]+)`).exec(cookieHeader || "");
  if (!match) return false;
  const [expiryText, signatureHex] = match[1].split(".");
  const expiry = Number(expiryText);
  if (!Number.isFinite(expiry) || expiry * 1000 < Date.now()) return false;
  const bytes = new Uint8Array((signatureHex || "").match(/.{2}/g)?.map((h) => parseInt(h, 16)) || []);
  if (bytes.length !== 32) return false;
  return crypto.subtle.verify("HMAC", await hmacKey(password), bytes, enc.encode(String(expiry)));
}

/** Compare digests rather than the strings, so length cannot leak either.
 *
 * `timingSafeEqual` is a Workers extension to WebCrypto, not standard, so this
 * function is exercised against the deployed Worker rather than under node.
 */
export async function timingSafeEqualStr(a, b) {
  const da = await crypto.subtle.digest("SHA-256", enc.encode(a || ""));
  const db = await crypto.subtle.digest("SHA-256", enc.encode(b || ""));
  return crypto.subtle.timingSafeEqual(da, db);
}

export function sessionCookie(value, maxAgeSeconds) {
  // Host-only: no Domain attribute, so the cookie belongs to this subdomain
  // alone. That matters on workers.dev, where a shared parent domain would be
  // the wrong scope even if the browser allowed it.
  return `${SESSION_COOKIE}=${value}; Max-Age=${maxAgeSeconds}; Path=/; HttpOnly; Secure; SameSite=Lax`;
}

export const SESSION_SECONDS = SESSION_DAYS * 86400;

// --------------------------------------------------------------------------
// Rate limiting
// --------------------------------------------------------------------------

export async function failureCount(db, ip) {
  const row = await db.prepare(
    "SELECT n FROM login_failures WHERE ip = ? AND expires_at > ?",
  ).bind(ip, Math.floor(Date.now() / 1000)).first();
  return row?.n || 0;
}

export async function recordFailure(db, ip) {
  const now = Math.floor(Date.now() / 1000);
  await db.prepare(`
    INSERT INTO login_failures (ip, n, expires_at) VALUES (?, 1, ?)
    ON CONFLICT (ip) DO UPDATE SET
      n = CASE WHEN login_failures.expires_at > ? THEN login_failures.n + 1 ELSE 1 END,
      expires_at = ?
  `).bind(ip, now + FAIL_WINDOW_S, now, now + FAIL_WINDOW_S).run();
}

export async function clearFailures(db, ip) {
  await db.prepare("DELETE FROM login_failures WHERE ip = ?").bind(ip).run();
}

export { FAIL_LIMIT, FAIL_WINDOW_S, WRONG_PASSWORD_DELAY_MS, SESSION_COOKIE };

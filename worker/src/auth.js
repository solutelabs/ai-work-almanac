const encoder = new TextEncoder();

export async function sha256(text) {
  return new Uint8Array(await crypto.subtle.digest("SHA-256", encoder.encode(text)));
}

export const hex = (bytes) => [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");

// Hashing first gives equal-length inputs, so the comparison leaks nothing about the secret.
export async function safeEqual(a, b) {
  if (!a || !b) return false;
  return crypto.subtle.timingSafeEqual(await sha256(a), await sha256(b));
}

export const bearer = (request) => request.headers.get("authorization")?.match(/^Bearer (.+)$/)?.[1] ?? "";

// No configured secret means no access, never open access.
export const adminAuthorized = (request, env) => safeEqual(bearer(request), env.ADMIN_TOKEN);

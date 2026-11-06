const encoder = new TextEncoder();

const fromBase64Url = (text) => Uint8Array.from(atob(text.replaceAll("-", "+").replaceAll("_", "/")), (c) => c.charCodeAt(0));
const decodeJson = (text) => JSON.parse(new TextDecoder().decode(fromBase64Url(text)));

// Checks the token Cloudflare Access adds after sign-in, so a request that reached the Worker
// without passing through Access, such as on a preview URL, is still refused.
export async function validAccessToken(token, team, audience) {
  if (!token || !team || !audience) return false;
  try {
    const [header, payload, signature] = token.split(".");
    const { alg, kid } = decodeJson(header);
    if (alg !== "RS256") return false;

    const response = await fetch(`https://${team}.cloudflareaccess.com/cdn-cgi/access/certs`);
    if (!response.ok) return false;
    const jwk = (await response.json()).keys?.find((key) => key.kid === kid);
    if (!jwk) return false;

    const key = await crypto.subtle.importKey("jwk", jwk, { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" }, false, ["verify"]);
    const signed = await crypto.subtle.verify("RSASSA-PKCS1-v1_5", key, fromBase64Url(signature), encoder.encode(`${header}.${payload}`));
    if (!signed) return false;

    const { aud, iss, exp, nbf } = decodeJson(payload);
    const now = Date.now() / 1000;
    return [aud].flat().includes(audience)
      && iss === `https://${team}.cloudflareaccess.com`
      && typeof exp === "number" && exp > now
      && !(nbf > now);
  } catch {
    return false; // A damaged token or an unreachable key list means no access, never open access.
  }
}

// Who the token is for. Only call this after validAccessToken has accepted the token.
export function accessEmail(token) {
  return String(decodeJson(token.split(".")[1]).email ?? "").toLowerCase();
}

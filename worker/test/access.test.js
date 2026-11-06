import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { accessEmail, validAccessToken } from "../src/access.js";

const TEAM = "your-org";
const AUD = "app-audience";
const ISS = `https://${TEAM}.cloudflareaccess.com`;
const now = () => Math.floor(Date.now() / 1000);
const b64 = (bytes) => Buffer.from(bytes).toString("base64url");
const part = (value) => b64(JSON.stringify(value));

const algorithm = { name: "RSASSA-PKCS1-v1_5", modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: "SHA-256" };
let keys;
let otherKeys;
const realFetch = globalThis.fetch;
let certs;

async function sign(claims, { header = { alg: "RS256", kid: "k1" }, signer = keys.privateKey } = {}) {
  const body = `${part(header)}.${part(claims)}`;
  const signature = await crypto.subtle.sign("RSASSA-PKCS1-v1_5", signer, new TextEncoder().encode(body));
  return `${body}.${b64(new Uint8Array(signature))}`;
}

const good = () => ({ aud: [AUD], iss: ISS, exp: now() + 60, nbf: now() - 60, email: "a@example.com" });

before(async () => {
  keys = await crypto.subtle.generateKey(algorithm, true, ["sign", "verify"]);
  otherKeys = await crypto.subtle.generateKey(algorithm, true, ["sign", "verify"]);
  const jwk = await crypto.subtle.exportKey("jwk", keys.publicKey);
  certs = () => new Response(JSON.stringify({ keys: [{ ...jwk, kid: "k1" }] }));
  globalThis.fetch = async (url) => {
    assert.equal(url, `${ISS}/cdn-cgi/access/certs`);
    return certs();
  };
});

after(() => {
  globalThis.fetch = realFetch;
});

test("accepts a token Access signed for this app", async () => {
  assert.equal(await validAccessToken(await sign(good()), TEAM, AUD), true);
  assert.equal(await validAccessToken(await sign({ ...good(), aud: AUD }), TEAM, AUD), true);
});

test("refuses a token meant for something else", async () => {
  assert.equal(await validAccessToken(await sign({ ...good(), aud: ["other-app"] }), TEAM, AUD), false);
  assert.equal(await validAccessToken(await sign({ ...good(), iss: "https://evil.cloudflareaccess.com" }), TEAM, AUD), false);
});

test("refuses a token outside its time window", async () => {
  assert.equal(await validAccessToken(await sign({ ...good(), exp: now() - 1 }), TEAM, AUD), false);
  assert.equal(await validAccessToken(await sign({ ...good(), exp: undefined }), TEAM, AUD), false);
  assert.equal(await validAccessToken(await sign({ ...good(), nbf: now() + 60 }), TEAM, AUD), false);
});

test("refuses a forged token", async () => {
  assert.equal(await validAccessToken(await sign(good(), { signer: otherKeys.privateKey }), TEAM, AUD), false);
  assert.equal(await validAccessToken(await sign(good(), { header: { alg: "RS256", kid: "unknown" } }), TEAM, AUD), false);

  const [header, , signature] = (await sign(good())).split(".");
  const changed = part({ ...good(), email: "someone-else@example.com" });
  assert.equal(await validAccessToken(`${header}.${changed}.${signature}`, TEAM, AUD), false);

  assert.equal(await validAccessToken(`${part({ alg: "none", kid: "k1" })}.${part(good())}.`, TEAM, AUD), false);
});

test("refuses when anything is missing or broken, never opening access", async () => {
  const token = await sign(good());
  assert.equal(await validAccessToken(null, TEAM, AUD), false);
  assert.equal(await validAccessToken("not-a-token", TEAM, AUD), false);
  assert.equal(await validAccessToken("a.b.c", TEAM, AUD), false);
  assert.equal(await validAccessToken(token, undefined, AUD), false);
  assert.equal(await validAccessToken(token, TEAM, ""), false);

  const working = certs;
  certs = () => new Response("down", { status: 503 });
  assert.equal(await validAccessToken(token, TEAM, AUD), false);
  certs = () => { throw new Error("network"); };
  assert.equal(await validAccessToken(token, TEAM, AUD), false);
  certs = working;
});

test("reads the signed-in email in lower case", async () => {
  const token = await sign({ ...good(), email: "Asha.Rao@Example.com" });
  assert.equal(accessEmail(token), "asha.rao@example.com");
  assert.equal(accessEmail(await sign({ ...good(), email: undefined })), "");
});

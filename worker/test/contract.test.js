import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import test from "node:test";
import { MAX_BODY_BYTES, validateCapture, validateHarness, validateIngest, validateLaptopCommits } from "../src/validate.js";

// The server rejects a whole batch over one bad value, and that laptop would then stop reporting for good.
// So anything the collector can produce, even from damaged logs, has to pass these same checks.
const script = fileURLToPath(new URL("../../collector/fuzz_payload.py", import.meta.url));
const python = process.env.PYTHON ?? (process.platform === "win32" ? "python" : "python3");

for (const seed of [1, 2, 3]) {
  test(`every batch built from damaged logs is accepted, seed ${seed}`, () => {
    const output = execFileSync(python, [script, String(seed)], { maxBuffer: 1 << 30, encoding: "utf8", env: { ...process.env, PYTHONUTF8: "1" } });
    const { ingest, capture, commits, harness } = JSON.parse(output);
    assert.ok(ingest.length && capture.length && commits.length && harness.length);
    for (const [name, batches, validate] of [["ingest", ingest, validateIngest], ["capture", capture, validateCapture], ["commits", commits, validateLaptopCommits], ["harness", harness, validateHarness]]) {
      for (const batch of batches) {
        const result = validate(batch);
        assert.equal(result.error, undefined, `${name}: ${result.error}`);
        assert.ok(JSON.stringify(batch).length <= MAX_BODY_BYTES, `${name} batch is too large`);
      }
    }
  });
}

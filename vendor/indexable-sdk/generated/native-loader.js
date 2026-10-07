// This action vendors the SDK artifact generated from the ix monorepo because
// the public npm registry still serves the older 0.7.2 wire decoder. The
// compressed native addon keeps the action checkout small while preserving the
// platform-specific Node ABI that GitHub-hosted runners load.
"use strict";
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const zlib = require("node:zlib");

const platformKey = `${process.platform}-${process.arch}`;
const compressed = path.join(__dirname, `../native/ix_sdk-${platformKey}.node.gz`);
const native = path.join(__dirname, `../native/ix_sdk-${platformKey}.node`);
if (!fs.existsSync(compressed)) {
  throw new Error(`@indexable/sdk: unsupported platform ${platformKey}`);
}
// Linux's public SDK addon links the vendored aws-lc symbols through a small
// set of sibling shared objects. Keeping them beside the addon lets its
// $ORIGIN RUNPATH resolve without relying on runner-wide installations.
if (process.platform === "linux") {
  for (const name of ["libaws_lc_0_41_0_crypto.so", "libblake3.so"]) {
    const compressedSidecar = path.join(__dirname, `../native/${name}.gz`);
    const sidecar = path.join(__dirname, `../native/${name}`);
    if (!fs.existsSync(sidecar)) {
      fs.writeFileSync(sidecar, zlib.gunzipSync(fs.readFileSync(compressedSidecar)), { mode: 0o755 });
    }
  }
}
if (!fs.existsSync(native)) {
  fs.writeFileSync(native, zlib.gunzipSync(fs.readFileSync(compressed)), { mode: 0o755 });
}
module.exports = require(native);

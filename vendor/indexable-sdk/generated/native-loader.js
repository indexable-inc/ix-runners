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
const compressedCrypto = path.join(__dirname, "../native/libaws_lc_0_41_0_crypto.so.gz");
const crypto = path.join(__dirname, "../native/libaws_lc_0_41_0_crypto.so");
if (!fs.existsSync(compressed)) {
  throw new Error(`@indexable/sdk: unsupported platform ${platformKey}`);
}
// Linux's public SDK addon links the vendored aws-lc symbols through a small
// sibling shared object. Keeping it beside the addon lets its $ORIGIN RUNPATH
// resolve without relying on a runner-wide library installation.
if (process.platform === "linux" && !fs.existsSync(crypto)) {
  fs.writeFileSync(crypto, zlib.gunzipSync(fs.readFileSync(compressedCrypto)), { mode: 0o755 });
}
if (!fs.existsSync(native)) {
  fs.writeFileSync(native, zlib.gunzipSync(fs.readFileSync(compressed)), { mode: 0o755 });
}
module.exports = require(native);

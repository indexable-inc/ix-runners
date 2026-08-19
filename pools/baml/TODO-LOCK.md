# TODO: generate pools/baml/flake.lock

This subflake has no flake.lock yet (no nix runs on the machine that wrote
it). On a fleet node (hil-compute-3 or a dev node, never spark), from a
checkout of this branch:

    cd <checkout>/pools/baml
    nix flake lock

Then commit the resulting `pools/baml/flake.lock` and delete this file.

Notes for the coordinator:

- The `ix-runners` input pins 4751cbbab884173b3a3bcee7c19808e89a18bb37, the
  current main tip (v2 + OIDC vend). It is pushed and the repo is public,
  so the lock resolves anonymously. When this branch merges, repin the
  input to the merge rev on main (main revs only, per the comment in
  flake.nix) and re-lock - the module this pool boots should be the same
  code the action pin carries.
- `nixpkgs` tracks nixos-unstable; the lock is what pins the actual rev.
  Keep it fresh: the GitHub Actions runner package must stay current or
  registered runners are refused.

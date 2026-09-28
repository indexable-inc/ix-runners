# The e2e pool's runner template, and the derivation the e2e job builds.
#
# The template is the stock mechanism plus nothing: what a customer gets from
# `ix-runners.lib.mkRunner` with an empty policy. If the e2e job goes green
# here, a Nix-first customer job goes green on the defaults.
{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

    # The mechanism under test comes from this pin, not from the enclosing
    # checkout: a subflake input cannot name "the tree I was fetched from".
    # Bump it (and the lock) to a main rev after a module.nix change lands,
    # so the next e2e run exercises it. MAIN REVS ONLY, as in pools/baml.
    ix-runners.url = "github:indexable-inc/ix-runners/12bcae3bbcc7f53837ee3ef6db9853632a291398";
  };

  outputs =
    {
      nixpkgs,
      ix-runners,
      ...
    }:
    let
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};
    in
    {
      nixosConfigurations.ci-runner = ix-runners.lib.mkRunner {
        inherit nixpkgs system;
        modules = [ { system.stateVersion = "25.05"; } ];
      };

      # Compiles and runs a C program, so the job proves a working stdenv
      # toolchain in the guest store rather than a cache download of a
      # finished output. The output text is what the e2e job asserts on.
      packages.${system}.e2e-probe = pkgs.runCommandCC "ixr-e2e-probe" { } ''
        cat > probe.c <<'C'
        #include <stdio.h>
        int main(void) { puts("ixr-e2e-probe ok"); return 0; }
        C
        $CC probe.c -o probe
        ./probe > $out
      '';
    };
}

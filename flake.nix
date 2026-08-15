{
  description = "Self-hosted GitHub Actions runner pools on ix VMs";

  outputs =
    { self }:
    {
      nixosModules.runner = import ./module.nix;
      nixosModules.default = self.nixosModules.runner;

      lib = {
        # One flake attr per pool member (ci-runner-1..N) so hostnames and
        # runner names are unique fleet-wide. The consuming flake calls:
        #
        #   nixosConfigurations = ix-runners.lib.mkPool {
        #     nixpkgs = <a nixpkgs input>;
        #     configRev = self.rev or null;
        #     spec = builtins.fromTOML (builtins.readFile ./nix/ix-pool.toml);
        #     modules = [ ./nix/ci-runner.nix ];   # the repo's policy
        #   };
        #
        # Use a FRESH nixpkgs (nixos-unstable): GitHub deprecates Actions
        # runner versions aggressively, and the runner package must stay
        # current or registered runners are refused.
        # `spec` is the pool spec both sides read - the same nix/ix-pool.toml
        # the reconcile is pointed at. Passing it is what keeps the flake and
        # the workflow from disagreeing about the pool's size, which used to
        # be two numbers in two files with a comment asking you to keep them
        # equal. Explicit arguments still win, so an existing call that does
        # not pass a spec behaves exactly as before.
        mkPool =
          {
            nixpkgs,
            modules,
            spec ? { },
            size ? spec."pool-size" or 8,
            system ? "x86_64-linux",
            configRev ? null,
            attrPrefix ? spec."attr-prefix" or "ci-runner",
          }:
          nixpkgs.lib.listToAttrs (
            map (n: {
              name = "${attrPrefix}-${toString n}";
              value = nixpkgs.lib.nixosSystem {
                inherit system;
                specialArgs = {
                  poolIndex = n;
                };
                modules = [
                  self.nixosModules.runner
                  (
                    { config, lib, ... }:
                    {
                      # Named, so an option-definition conflict points here
                      # instead of at an anonymous "<unknown-file>".
                      _file = "ix-runners/flake.nix#mkPool";
                      # mkDefault throughout: the repo's own policy modules
                      # may pin any of these.
                      services.ix-runner.configRev = lib.mkDefault configRev;
                      # mkIf, not a fallback expression: reading the option
                      # back to default it to itself is infinite recursion.
                      services.ix-runner.poolName = lib.mkIf (spec ? "pool-name") (
                        lib.mkDefault spec."pool-name"
                      );
                      # The spec names ONE label for the reconcile to match
                      # jobs against while the pool has nothing registered
                      # yet. If the policy does not actually advertise it,
                      # the bootstrap matches nothing and the pool never
                      # scales up - silently, because zero servable jobs and
                      # zero jobs look identical. Catch it at build time.
                      assertions = lib.optional (spec ? "runner-label") {
                        assertion = builtins.elem spec."runner-label" config.services.ix-runner.labels;
                        message = ''
                          ix-pool.toml sets runner-label "${spec."runner-label"}",
                          but services.ix-runner.labels is
                          [ ${builtins.concatStringsSep " " config.services.ix-runner.labels} ].
                          The reconcile matches queued jobs against that label
                          before any runner has registered, so a label nothing
                          advertises means the pool never wakes for a wave.
                        '';
                      };
                    }
                  )
                ]
                ++ modules;
              };
            }) (nixpkgs.lib.range 1 size)
          );
      };
    };
}

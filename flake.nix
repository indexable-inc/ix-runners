{
  description = "Ephemeral fork-per-job GitHub Actions runners on ix VMs";

  outputs =
    { self }:
    {
      nixosModules.runner = import ./module.nix;
      nixosModules.default = self.nixosModules.runner;

      lib = {
        # The one template every runner of every lineage boots from (cold) or
        # descends from (via its lineage's seed snapshot). The consuming
        # flake exposes it under the attr the pool spec names
        # (`template-attr`, default "ci-runner"):
        #
        #   nixosConfigurations.ci-runner = ix-runners.lib.mkRunner {
        #     nixpkgs = <a nixpkgs input>;      # keep it FRESH: GitHub
        #     modules = [ ./nix/ci-runner.nix ];  # deprecates old runner
        #   };                                    # versions aggressively
        #
        # There is deliberately no size, no member list and no label option
        # here: capacity is the reconcile's job, and labels ride each job's
        # JIT credential, so nix never needs to know them.
        mkRunner =
          {
            nixpkgs,
            modules,
            system ? "x86_64-linux",
          }:
          nixpkgs.lib.nixosSystem {
            inherit system;
            modules = [
              self.nixosModules.runner
              {
                # Named, so an option-definition conflict points here
                # instead of at an anonymous "<unknown-file>".
                _file = "ix-runners/flake.nix#mkRunner";
                services.ix-runner.enable = nixpkgs.lib.mkDefault true;
              }
            ]
            ++ modules;
          };
      };
    };
}

{
  description = "Self-hosted GitHub Actions runner pools on ix VMs";

  outputs =
    { self }:
    {
      nixosModules.runner = import ./module.nix;
      nixosModules.default = self.nixosModules.runner;

      lib = {
        # ONE configuration for the whole pool, exposed under every member
        # attr the reconcile asks for (#18). Members differ only in which
        # member they are, and that is resolved at boot from the machine's
        # identity - so one build serves the fleet, every member after the
        # first boots on a cache hit, and a roll compiles one template
        # instead of `size` of them.
        #
        # The consuming flake calls:
        #
        #   nixosConfigurations = ix-runners.lib.mkPool {
        #     nixpkgs = <a nixpkgs input>;
        #     configRev = self.rev or null;
        #     modules = [ ./nix/ci-runner.nix ];   # the repo's policy
        #   };
        #
        # Use a FRESH nixpkgs (nixos-unstable): GitHub deprecates Actions
        # runner versions aggressively, and the runner package must stay
        # current or registered runners are refused.
        mkPool =
          {
            nixpkgs,
            modules,
            size ? 8,
            system ? "x86_64-linux",
            configRev ? null,
            attrPrefix ? "ci-runner",
          }:
          let
            pool = nixpkgs.lib.nixosSystem {
              inherit system;
              modules = [
                self.nixosModules.runner
                {
                  # Named, so an option-definition conflict points here
                  # instead of at an anonymous "<unknown-file>".
                  _file = "ix-runners/flake.nix#mkPool";
                  # mkDefault: the repo's own policy modules may pin a rev.
                  services.ix-runner.configRev = nixpkgs.lib.mkDefault configRev;
                }
              ]
              ++ modules;
            };
          in
          # The numbered attrs are ALIASES of one configuration, not copies:
          # same derivation, so the template cache keyed on (rev, attr) hits
          # after the first member builds. They exist because the reconcile
          # creates each member from `<rev>#<attrPrefix>-<N>`; the bare
          # `<attrPrefix>` attr is what that should collapse to once the
          # reconcile stops numbering. `size` therefore only bounds how many
          # aliases exist - it must be >= the workflow's pool-size, and it no
          # longer costs an evaluation per member.
          {
            ${attrPrefix} = pool;
          }
          // nixpkgs.lib.listToAttrs (
            map (n: nixpkgs.lib.nameValuePair "${attrPrefix}-${toString n}" pool) (nixpkgs.lib.range 1 size)
          );
      };
    };
}

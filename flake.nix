{
  description = "Self-hosted GitHub Actions runner pools on ix VMs";

  outputs =
    { self }:
    {
      nixosModules.runner = import ./module.nix;

      lib = {
        # One flake attr per pool member (ci-runner-1..N) so hostnames and
        # runner names are unique fleet-wide. The consuming flake calls:
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
                  { services.ix-runner.configRev = configRev; }
                ]
                ++ modules;
              };
            }) (nixpkgs.lib.range 1 size)
          );
      };
    };
}

# BAML's runner template as a self-contained subflake of the ix-runners
# repository. The reconcile cold-boots lineages from
# github:indexable-inc/ix-runners/<rev>?dir=pools/baml#ci-runner, where
# <rev> is the commit the customer's workflow pins the action to - so the
# whole pool definition (this flake, ix-runners.toml, ci-runner.nix) ships
# with the action, and the customer repo carries nothing but the workflow.
{
  inputs = {
    # Fresh nixos-unstable for the runner machines: GitHub deprecates
    # Actions runner versions aggressively, and the runner package must
    # stay current or registered runners are refused. The lock pins the
    # actual rev; bump it when GitHub starts refusing the locked version.
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

    # The runner mechanism (module.nix + lib.mkRunner). This subflake lives
    # INSIDE that same repository, but a flake input cannot say "the
    # enclosing checkout at whatever rev I was fetched at", so the mechanism
    # comes from this pin, not from the surrounding tree; bumping it means
    # bumping this rev and the lock. MAIN REVS ONLY: a branch pin reverts
    # every fix main has that the branch lacks (2026-08-16: an app-auth
    # branch pin time-traveled past a region fix and recreated machines in
    # the wrong region).
    ix-runners.url = "github:indexable-inc/ix-runners/4751cbbab884173b3a3bcee7c19808e89a18bb37";
  };

  outputs =
    {
      nixpkgs,
      ix-runners,
      ...
    }:
    {
      # The one template every lineage cold-boots from; the attr name is
      # what ix-runners.toml's template-attr points at.
      nixosConfigurations.ci-runner = ix-runners.lib.mkRunner {
        inherit nixpkgs;
        modules = [ ./ci-runner.nix ];
      };
    };
}

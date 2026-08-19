# BAML's runner policy: what every machine of every lineage carries beyond
# the mechanism in this repository's module.nix. This file is deliberately
# only the three things a v2 policy can be - packages on the job PATH, job
# environment, and plain NixOS settings for the image. Capacity, labels and
# lifecycle are the reconcile's business; warmth is the seed snapshot's.
#
# Toolchain provisioning is rustup + mise (per baml's repo-pinned
# rust-toolchain.toml and mise.toml), not nix: what is listed here is
# Ubuntu-image parity - the things GitHub's hosted images preinstall that
# neither rustup nor mise provides on a NixOS guest - plus the env those
# FHS-flavored tools need to find nix's split-output openssl.
{
  lib,
  pkgs,
  ...
}:
{
  # module.nix deliberately sets no stateVersion; the policy owns it.
  system.stateVersion = "25.05";

  # The fleet substitutes through ix's public binary cache. cache.ix.dev is
  # a pull-through cache (ncps in front of the ix fleet cache and
  # cache.nixos.org): any path one machine pulls is cached fleet-side, so a
  # config-rev roll re-warms from datacenter bandwidth instead of upstream
  # registries. Pull is anonymous; there is no push from these machines.
  # This must live in the image: job code is an untrusted nix client
  # (module.nix pins trusted-users to root), so nothing a job passes at
  # runtime can add a substituter.
  nix.settings = {
    substituters = [
      "https://cache.ix.dev"
      "https://cache.nixos.org/"
    ];
    trusted-public-keys = [
      # ix fleet cache key (narinfos signed server-side) + the ncps front's
      # own re-signing key, then the nixpkgs default. Setting this option
      # replaces the default list, so cache.nixos.org-1 must be restated.
      "ix-workspace:JuAaeOPfR3GL3nUICpEz/88/+S3BzGF3L6bPYFy0GwI="
      "hil-stor-2:UYyDQcJ/iepiePK/ptHRqR2t98okIpsfOVqE0Pm5CwY="
      "cache.nixos.org-1:6NCHdD59X431o0gWypbMrAURkbJ16ZPMQFGspcDShjY="
    ];
    # Reactive headroom on top of the module's weekly time-based GC, which a
    # busy store can outrun: collect down to 100 GiB free whenever free
    # space drops under 50 GiB. Store growth rides seed snapshots across
    # fork generations, so this fires on the lineages that live long enough.
    min-free = 50 * 1024 * 1024 * 1024;
    max-free = 100 * 1024 * 1024 * 1024;
    # nix's upstream default caches a MISSING narinfo for 3600s - an hour of
    # refusing to see a path the cache has since gained, snapshot-carried
    # into every fork of the lineage. 60s is what the ix fleet pins.
    narinfo-cache-negative-ttl = 60;
  };

  # Prefer IPv4 for every destination: some regions' guests hold global v6
  # addresses whose upstream gateway does not yet answer NDP, so any
  # AAAA-bearing destination dies with EHOSTUNREACH before the client falls
  # back (this killed the cargo-xwin lanes via download.visualstudio's AAAA).
  # glibc-level so every client is covered; remove once v6 delivery lands.
  environment.etc."gai.conf".text = ''
    precedence ::ffff:0:0/96 100
  '';

  services.ix-runner = {
    # Ubuntu-image parity: what BAML's jobs expect preinstalled and neither
    # rustup nor mise provides on a NixOS guest. jq is NOT here: the
    # module's base userland already ships it.
    extraPackages = with pkgs; [
      rustup # jobs run `rustup show` to pull the repo-pinned toolchain
      gcc
      gnumake
      cmake
      ninja
      pkg-config
      python3
      openssl
      git-lfs
      glibc.bin # mise-action probes `ldd` to pick its binary
      ruby # release-metadata packaging tests
      go # sdkgen_go's build script shells out to gofmt
      nodejs_22 # pyright runs on the PATH node
      # Ubuntu ships perl; mise does not install it. The python sdk build
      # lanes and openssl-sys' vendored configure both run it.
      perl
      # musl leg: the full cross gcc, under the name setup-musl-cross probes
      # for (the thin musl libc wrapper links broken static-PIE binaries).
      (writeShellScriptBin "musl-gcc" ''
        exec ${pkgsCross.musl64.stdenv.cc}/bin/x86_64-unknown-linux-musl-gcc "$@"
      '')
    ];

    jobEnvironment = {
      PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS = "true";
      PYRIGHT_PYTHON_GLOBAL_NODE = "1";
      # mise would source-build node/python on NixOS; prebuilts run fine.
      MISE_NODE_COMPILE = "0";
      MISE_PYTHON_COMPILE = "0";
      # openssl-sys (baml_language workspace) probes these; nix splits the
      # outputs Ubuntu ships together.
      OPENSSL_LIB_DIR = "${lib.getLib pkgs.openssl}/lib";
      OPENSSL_INCLUDE_DIR = "${pkgs.openssl.dev}/include";
      PKG_CONFIG_PATH = lib.makeSearchPath "lib/pkgconfig" [ pkgs.openssl.dev ];
      # The link line above resolves at runtime too: test binaries link
      # libssl dynamically against the nix openssl, and without this the
      # loader answers "libssl.so.3: cannot open shared object file" (hit
      # live when the binaries first ran outside a nix shell).
      LD_LIBRARY_PATH = "${lib.getLib pkgs.openssl}/lib";
      # setup-dotnet defaults to /usr/share/dotnet, read-only here. The
      # module's one runner user homes at /home/runner, and HOME is what the
      # seed snapshot carries - so the runtime installed by a green run is
      # already in place on every later fork of the lineage.
      DOTNET_INSTALL_DIR = "/home/runner/.dotnet";
      # Test parallelism, sized to the 16-vCPU envelope these suites were
      # tuned on; per-lane workflow env may override it.
      NEXTEST_TEST_THREADS = "16";
      RUST_TEST_THREADS = "16";
      # Builds too: cargo defaults -j to the guest's full elastic core count
      # (64), and every concurrent fork doing that oversubscribes the host
      # it shares. Measured 1.19x slower unpinned at full concurrency.
      CARGO_BUILD_JOBS = "16";
      # vitest sizes its worker pool off availableParallelism() - 1 and
      # vitest-pool-workers spawns one workerd process per worker, which
      # OOMs the 64-vCPU elastic guests unpinned. Note this variable also
      # overrides the browser pool's upstream min(12, cores-1) cap (vitest
      # #7871) verbatim, so it lowers the Playwright leg to 4 as well -
      # accepted, that leg passes comfortably and correctness beats
      # parallelism here.
      VITEST_MAX_WORKERS = "4";
    };
  };
}

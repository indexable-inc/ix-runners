# Persistent self-hosted GitHub Actions runner pool on an ix VM.
#
# MECHANISM ONLY, maintained by ix. Repo policy - toolchain packages, job
# env, labels, pool size - lives in the customer's importing config. The
# ix-platform workarounds below each carry a short why and an issue link on
# this repository; workaround code is deleted centrally as platform fixes
# land, and customers pick that up as a rev bump of their flake input.
#
# Design: the VM is PERSISTENT. Jobs run directly on the host, so everything
# the first run pays for - mise toolchains, rustup, ~/.cargo registry,
# sccache - stays warm for every later run. That persistence is the entire
# point vs stateless runners; do not make these runners ephemeral. Foreign
# dynamically-linked FHS binaries (mise, rustup toolchains, prebuilt node,
# cargo-binstall downloads) run unmodified via nix-ld + envfs.
{
  config,
  lib,
  pkgs,
  poolIndex ? 1,
  ...
}:
let
  inherit (lib)
    mkDefault
    mkEnableOption
    mkIf
    mkOption
    types
    ;
  cfg = config.services.ix-runner;
  idx = toString poolIndex;
  runnerNames = map (i: "${cfg.poolName}-r${idx}-${toString i}") (lib.range 1 cfg.slots);

  # The Ubuntu-ish userland FHS-assuming job steps expect on PATH. Jobs
  # inherit the runner UNIT's PATH - on image-booted template guests
  # /run/current-system/sw/bin is empty (ix platform, see issue #1), so
  # systemPackages alone never reaches job steps.
  basePackages = with pkgs; [
    bashInteractive
    coreutils
    findutils
    gnugrep
    gnused
    gawk
    which
    file
    git
    curl
    wget
    jq
    rsync
    gnutar
    gzip
    unzip
    zip
    zstd
    xz
    bzip2
  ];

  # GitHub's runner hardcodes node20 as its internal runtime; nixpkgs ships
  # node24 only (node20 is EOL), which breaks every hashFiles() expression.
  # Serve node24 under the node20 name inside a copied package: a copy keeps
  # the binary-cache hit (an override would rebuild the runner from source),
  # and the bin-wrapper rewrite makes the copy the runner's root so it finds
  # the shim. makeOverridable because the NixOS module calls `pkg.override`.
  runnerPackage = lib.makeOverridable (
    {
      nodeRuntimes ? [ "node24" ],
    }:
    let
      base = pkgs.github-runner.override { inherit nodeRuntimes; };
    in
    pkgs.runCommand "github-runner-with-node20" { } ''
      cp -a ${base} $out
      chmod -R u+w $out
      for f in $out/bin/*; do
        sed -i "s|${base}|$out|g" "$f"
      done
      ln -sfn ${pkgs.nodejs_24} $out/lib/externals/node20
    ''
  ) { };
in
{
  options.services.ix-runner = {
    enable = mkEnableOption "persistent GitHub Actions runner pool on this ix VM";

    url = mkOption {
      type = types.str;
      description = "Repository URL the runners register against.";
    };

    poolName = mkOption {
      type = types.str;
      description = ''
        Pool name, usually the repository name. Everything derives from it:
        VM hostname `<poolName>-runner-<N>`, runner names
        `<poolName>-r<N>-<slot>`. Runner names must be unique fleet-wide -
        GitHub rejects duplicates, and `replace = true` would otherwise let
        one VM silently steal another's registration.
      '';
    };

    slots = mkOption {
      type = types.ints.positive;
      default = 4;
      description = "Runner daemons on this VM = concurrent job slots.";
    };

    labels = mkOption {
      type = types.listOf types.str;
      default = [ "ix" ];
      description = "Extra labels; workflows target `self-hosted` plus these.";
    };

    tokenFile = mkOption {
      type = types.str;
      default = "/run/secrets/runner-token";
      description = ''
        Runner registration credential, attached at machine create through
        the ix secret store (the reconcile's `secret_files` option) - never
        baked into the image, so the template cache stays shareable. Always
        a short-lived (1 h) registration token, never a PAT: registration
        only happens at configure time, so nothing long-lived sits on the
        VM. Units condition-skip while the file is absent, so a boot
        without the secret still switches cleanly.
      '';
    };

    configRev = mkOption {
      type = types.nullOr types.str;
      default = null;
      description = ''
        Git rev of the runner config this image was built from (mkPool
        passes `self.rev`). Written to /etc/ix-runner/rev; the reconciler
        compares it against the last commit touching the runner config to
        decide replacement.
      '';
    };

    extraPackages = mkOption {
      type = types.listOf types.package;
      default = [ ];
      description = "Toolchain packages on each job's PATH, on top of the base userland.";
    };

    jobEnvironment = mkOption {
      type = types.listOf types.str;
      default = [ ];
      description = "Extra Environment= lines for every runner unit.";
    };
  };

  config = mkIf cfg.enable {
    networking.hostName = mkDefault "${cfg.poolName}-runner-${idx}";

    services.github-runners = lib.genAttrs runnerNames (name: {
      enable = true;
      package = runnerPackage;
      inherit (cfg) url tokenFile;
      # Persistence is the point (see header).
      ephemeral = false;
      replace = true;
      # Persistent disk, not the module's /run tmpfs default: checkouts and
      # target dirs live here between jobs; HOME carries the caches.
      workDir = "/var/lib/ix-runner-work/${name}";
      extraLabels = cfg.labels;
      extraPackages = basePackages ++ cfg.extraPackages;
      user = "ci-runner";
      group = "ci-runner";
      serviceOverrides = {
        # A stable writable HOME keeps toolchains and caches warm across
        # runs; the nixpkgs unit hardens it away.
        DynamicUser = lib.mkForce false;
        ProtectHome = lib.mkForce false;
        # ProtectSystem=strict: the shared TMPDIR must stay writable.
        ReadWritePaths = [ "/var/lib/ix-runner-tmp" ];
        # Defaults cap thread creation below what concurrent build jobs
        # need; see also the sysctl pins below.
        TasksMax = "infinity";
        LimitNPROC = "infinity";
        # Per-slot fairness: without an explicit weight the cpu/io
        # controllers stay off and threads compete GLOBALLY, so one job
        # spawning hundreds of compiler threads starves a co-tenant slot's
        # few threads for whole seconds (observed live: upstream-green
        # wall-clock test bounds missed only under slot contention).
        # Equal weights guarantee every busy slot its 1/slots share of the
        # guest - the dedicated-runner envelope jobs were tuned for - while
        # a lone job still bursts to every vCPU the guest advertises.
        CPUWeight = "100";
        IOWeight = "100";
        Environment = [
          # Disk-backed job temp, off the small boot-path /tmp (issue #2);
          # wiped by the tmpfiles age rule below.
          "TMPDIR=/var/lib/ix-runner-tmp"
        ]
        ++ cfg.jobEnvironment;
      };
    });

    environment.etc = lib.mkIf (cfg.configRev != null) {
      "ix-runner/rev".text = cfg.configRev;
    };

    users.users.ci-runner = {
      isSystemUser = true;
      group = "ci-runner";
      home = "/home/ci-runner";
      createHome = true;
      shell = pkgs.bashInteractive;
    };
    users.groups.ci-runner = { };

    systemd.tmpfiles.rules = [
      "d /var/lib/ix-runner 0700 root root -"
      "d /var/lib/ix-runner-tmp 1777 root root 3d"
    ]
    ++ map (name: "d /var/lib/ix-runner-work/${name} 0750 ci-runner ci-runner -") runnerNames;

    systemd.services =
      # Without the token the units SKIP instead of FAIL: the first boot of a
      # fresh template rev switches cleanly. The reconcile attaches the token
      # at create, so it is present from the first boot.
      lib.genAttrs (map (n: "github-runner-${n}") runnerNames) (_: {
        unitConfig.ConditionPathExists = cfg.tokenFile;
      })
      // {
        # /tmp is a tmpfs sized off BOOT-time RAM (1.5G observed, 100% full
        # under concurrent jobs) and closure tmpfs settings do not reach
        # image boots - a live remount does (ix platform, see issue #2).
        # 16G virtual is safe: tmpfs pages only cost when used, and
        # virtio-mem plugs RAM on demand.
        ix-runner-tmp-resize = {
          description = "resize the boot-path /tmp tmpfs";
          wantedBy = [ "multi-user.target" ];
          before = map (n: "github-runner-${n}.service") runnerNames;
          unitConfig.ConditionPathIsMountPoint = "/tmp";
          serviceConfig = {
            Type = "oneshot";
            RemainAfterExit = true;
            ExecStart = "${pkgs.util-linux}/bin/mount -o remount,size=16G /tmp";
          };
        };
      };

    # /run defaults to 50% of BOOT-time RAM; ix VMs boot small and grow
    # elastically. Virtual size: only used pages cost RAM.
    boot.runSize = "16G";
    # kernel.threads-max (and the RLIMIT_NPROC default derived from it) is
    # computed from BOOT-time RAM and never recomputed when virtio-mem grows
    # the guest; concurrent cargo jobs seeing the elastic vCPU ceiling hit
    # pthread_create EAGAIN. Pin real numbers.
    boot.kernel.sysctl = {
      "kernel.threads-max" = 1048576;
      "kernel.pid_max" = 4194304;
    };
    boot.tmp.cleanOnBoot = true;

    programs.nix-ld = {
      enable = true;
      libraries = with pkgs; [
        stdenv.cc.cc.lib
        zlib
        openssl
        curl
        expat
        libgcc
        icu
        libxml2
        libsecret
        krb5
        lttng-ust
        xz
        bzip2
        # Playwright-downloaded chromium/firefox/webkit runtime set: jobs
        # install browsers into their persistent HOME exactly as on Ubuntu
        # images, and nix-ld makes them executable here.
        glib
        nss
        nspr
        atk
        at-spi2-atk
        cups
        dbus
        libdrm
        mesa
        # libgbm was split out of mesa in nixpkgs; chromium's headless shell
        # dlopens libgbm.so.1 (its absence was the one failure after browsers
        # started executing from HOME).
        libgbm
        pango
        cairo
        libxkbcommon
        alsa-lib
        systemd # libudev
        xorg.libX11
        xorg.libXcomposite
        xorg.libXdamage
        xorg.libXext
        xorg.libXfixes
        xorg.libXrandr
        xorg.libxcb
      ];
    };
    services.envfs.enable = true;

    # For interactive `ix shell` debugging; job steps get their PATH from the
    # unit's extraPackages (see issue #1), never from here.
    environment.systemPackages = basePackages ++ cfg.extraPackages;

    nix.settings = {
      experimental-features = [
        "nix-command"
        "flakes"
      ];
      trusted-users = [
        "root"
        "ci-runner"
      ];
    };
  };
}

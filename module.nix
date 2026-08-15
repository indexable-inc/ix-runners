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
# sccache - stays warm for every later run. That warmth lives in a per-slot
# HOME under /var/lib/ix-runner-home, which nothing in the boot or restart
# path clears: the upstream unit wipes its workDir on EVERY start, so a cache
# under HOME=workDir (the upstream default) would survive nothing, not even
# the reconcile restarting a slot. That persistence is the entire point vs
# stateless runners; do not make these runners ephemeral. Foreign
# dynamically-linked FHS binaries (mise, rustup toolchains, prebuilt node,
# cargo-binstall downloads) run unmodified via nix-ld + envfs.
#
# Each slot is its own UNIX user, so co-tenant slots cannot read each other's
# runner credentials or caches. Consumer policy must therefore reference $HOME
# rather than any fixed home path.
{
  config,
  lib,
  pkgs,
  poolIndex ? 1,
  ...
}:
let
  inherit (lib)
    genAttrs
    makeOverridable
    mkDefault
    mkEnableOption
    mkForce
    mkIf
    mkOption
    range
    types
    ;
  cfg = config.services.ix-runner;
  idx = toString poolIndex;
  runnerNames = map (i: "${cfg.poolName}-r${idx}-${toString i}") (range 1 cfg.slots);
  runnerUnits = map (name: "github-runner-${name}") runnerNames;

  # One UNIX user per slot, named after the slot. A shared uid would make the
  # per-slot HOME/TMPDIR cosmetic: any job could read a co-tenant slot's
  # runner .credentials (and re-register that runner elsewhere), its steps'
  # /proc/<pid>/environ, and its at-rest ~/.npmrc, ~/.docker, ~/.cargo.
  # Warm caches stay per-slot either way, so nothing is lost by splitting.
  #
  # Per-slot, off the workDir the upstream unit wipes on every start.
  homeOf = name: "/var/lib/ix-runner-home/${name}";
  tmpOf = name: "/var/lib/ix-runner-tmp/${name}";

  # Already on the upstream unit's PATH (bashInteractive coreutils git gnutar
  # gzip), so job steps get them without us re-listing them per runner.
  upstreamUnitPackages = with pkgs; [
    bashInteractive
    coreutils
    git
    gnutar
    gzip
  ];

  # The rest of the Ubuntu-ish userland FHS-assuming job steps expect on PATH.
  # Jobs inherit the runner UNIT's PATH - on image-booted template guests
  # /run/current-system/sw/bin is empty (ix platform, see issue #1), so
  # systemPackages alone never reaches job steps.
  extraUserland = with pkgs; [
    findutils
    gnugrep
    gnused
    gawk
    which
    file
    curl
    wget
    jq
    rsync
    unzip
    zip
    zstd
    xz
    bzip2
  ];

  basePackages = upstreamUnitPackages ++ extraUserland;

  # nix-ld exports these through environment.sessionVariables, which rides PAM
  # and therefore never reaches a system unit; without them the loader falls
  # back to its compiled-in /run/current-system/sw path, the very tree issue #1
  # says is unreliable at unit start. Same values, resolved to the store.
  nixLdEnvironment = {
    NIX_LD = "${config.system.path}/share/nix-ld/lib/ld.so";
    NIX_LD_LIBRARY_PATH = "${config.system.path}/share/nix-ld/lib";
  };

  # GitHub's runner hardcodes node20 as its internal runtime; nixpkgs ships
  # node24 only (node20 is EOL), which breaks every hashFiles() expression.
  # Serve node24 under the node20 name inside a copied package: a copy keeps
  # the binary-cache hit (an override would rebuild the runner from source),
  # and the base-path rewrite makes the copy the runner's root so it finds
  # the shim. makeOverridable because the NixOS module calls `pkg.override`.
  runnerPackage = makeOverridable (
    {
      nodeRuntimes ? [ "node24" ],
    }:
    let
      base = pkgs.github-runner.override { inherit nodeRuntimes; };
    in
    pkgs.runCommand "github-runner-with-node20"
      {
        # A local copy of an already-substituted closure: shipping it through a
        # remote builder and a cache costs more than the `cp` it replaces.
        preferLocalBuild = true;
        allowSubstitutes = false;
      }
      ''
        cp -a ${base} $out
        chmod -R u+w $out
        # Every reference, not just $out/bin: one missed path sends the runner
        # back into ${base}, where lib/externals/node20 does not exist.
        grep -rlIZ -- "${base}" $out | xargs -0r sed -i "s|${base}|$out|g" \
          || { echo "rewriting ${base} references failed (nothing matched, or sed did)" >&2; exit 1; }
        if grep -rqI -- "${base}" $out; then
          echo "github-runner copy still references ${base}" >&2
          exit 1
        fi
        ln -sfn ${pkgs.nodejs_24} $out/lib/externals/node20
        # A nixpkgs layout change must fail the build here, not at hashFiles().
        test -x $out/lib/externals/node20/bin/node \
          || { echo "no node20 shim at $out/lib/externals/node20/bin/node" >&2; exit 1; }
      ''
  ) { };
in
{
  options.services.ix-runner = {
    enable = mkEnableOption "the persistent GitHub Actions runner pool on this ix VM";

    url = mkOption {
      type = types.str;
      example = "https://github.com/indexable-inc/ix-runners";
      description = "Repository URL the runners register against.";
    };

    poolName = mkOption {
      type = types.str;
      example = "ix-runners";
      description = ''
        Pool name, usually the repository name. Everything derives from it:
        VM hostname `<poolName>-runner-<N>`, runner names
        `<poolName>-r<N>-<slot>`. Runner names must be unique fleet-wide -
        GitHub rejects duplicates, and `replace = true` would otherwise let
        one VM silently steal another's registration. Must be hostname-safe.
      '';
    };

    slots = mkOption {
      type = types.ints.positive;
      default = 4;
      description = ''
        Runner daemons on this VM = concurrent job slots. Growing this is
        safe; shrinking it leaves the removed slots registered on GitHub as
        permanently offline runners, which must be deleted by hand (or by
        replacing the VM, which deregisters everything it registered).
      '';
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

        The file must exist with UNCHANGED content for the whole life of the
        VM. The upstream unit purges runner state and re-registers whenever
        the token content or any registration input (url, labels, name,
        workDir) changes, and by then the 1 h token has expired, so the slot
        cannot come back. Reconfiguring a live runner VM is therefore
        unsupported: replace the VM instead.
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
      type = types.attrsOf types.str;
      default = { };
      example = {
        CARGO_INCREMENTAL = "0";
      };
      description = "Extra environment variables set for every runner unit, and so for every job step.";
    };
  };

  config = mkIf cfg.enable {
    assertions = [
      {
        assertion = builtins.match "[a-z0-9]([a-z0-9-]*[a-z0-9])?" cfg.poolName != null;
        message = ''
          services.ix-runner.poolName is "${cfg.poolName}", which is not
          hostname-safe. It becomes this VM's hostname and every runner name,
          so it must be lowercase alphanumerics and hyphens, starting and
          ending with an alphanumeric.
        '';
      }
    ];

    networking.hostName = mkDefault "${cfg.poolName}-runner-${idx}";

    services.github-runners = genAttrs runnerNames (name: {
      enable = true;
      package = runnerPackage;
      inherit (cfg) url tokenFile;
      # Persistence is the point (see header).
      ephemeral = false;
      replace = true;
      # Persistent disk, not the module's /run tmpfs default: the checkout and
      # its build outputs live here. Only the checkout - upstream wipes this
      # directory on every service start, so caches live in HOME instead.
      workDir = "/var/lib/ix-runner-work/${name}";
      extraLabels = cfg.labels;
      extraPackages = extraUserland ++ cfg.extraPackages;
      # One user per slot, not one shared uid - see the users.users block.
      user = name;
      group = name;
      extraEnvironment = {
        # Upstream defaults HOME to workDir, which it wipes on every start;
        # a warm toolchain/registry cache needs a directory nothing clears.
        HOME = homeOf name;
        # Disk-backed job temp, off the small boot-path /tmp (issue #2);
        # aged out by the tmpfiles rule below.
        TMPDIR = tmpOf name;
      }
      // nixLdEnvironment
      // cfg.jobEnvironment;
      serviceOverrides = {
        # ProtectHome=true masks /home entirely; this slot's home is its
        # passwd home under /var/lib, and tooling that resolves ~ from passwd
        # rather than $HOME must reach the same directory.
        ProtectHome = mkForce false;
        # ProtectSystem=strict: this slot's HOME and TMPDIR must stay
        # writable (upstream already BindPaths= its workDir).
        ReadWritePaths = [
          (homeOf name)
          (tmpOf name)
        ];
        # The kernel OOM-killing one compiler process must not take the slot
        # down with it; systemd's default stop policy plus upstream's
        # Restart="no" for non-ephemeral runners made that permanent, and
        # invisibly so - the reconcile reads any-slot-online as healthy.
        OOMPolicy = "continue";
        Restart = mkForce "always";
        RestartSec = "10s";
        # Hosted runners give jobs 65536; the systemd default is 1024.
        LimitNOFILE = 1048576;
        # One runaway job must not exhaust system PIDs and take the
        # co-tenant slots with it. TasksMax bounds the slot's cgroup whatever
        # uid the tasks end up under (containers, setuid helpers), which
        # per-UID LimitNPROC cannot.
        TasksMax = 65536;
        LimitNPROC = "infinity";
        # Turns the cpu controller ON for the slice, so busy slots get fair
        # shares instead of competing globally (observed live: upstream-green
        # wall-clock test bounds missed only under slot contention). 100 is
        # systemd's default weight - this line is here for the controller,
        # not for the number.
        CPUWeight = "100";
        # Hosted-runner parity: 0066 leaves job artifacts unreadable to other
        # UIDs, which breaks containers and bind mounts reading the checkout.
        UMask = "0022";
        # chromium and playwright ship their own sandbox, which needs user
        # and pid namespaces. The VM is the isolation boundary here.
        RestrictNamespaces = mkForce false;
        PrivateUsers = mkForce false;
        # dockerd resolves `-v /tmp/...` in the host namespace, so a private
        # /tmp silently hands the container an empty directory. The VM is the
        # boundary, and TMPDIR points elsewhere anyway.
        PrivateTmp = mkForce false;
      };
    });

    environment.etc = mkIf (cfg.configRev != null) {
      "ix-runner/rev".text = cfg.configRev;
    };

    # One user and group per slot: the slot boundary is a uid boundary, so a
    # job cannot read a co-tenant slot's credentials, environ, or caches.
    # passwd home == $HOME, so tooling that resolves ~ from passwd agrees
    # with the unit environment.
    users.users = genAttrs runnerNames (name: {
      isSystemUser = true;
      group = name;
      home = homeOf name;
      createHome = true;
      shell = pkgs.bashInteractive;
    });
    users.groups = genAttrs runnerNames (_: { });

    systemd.tmpfiles.rules = [
      # 0700 root: the reconcile's strike marker lives here, and job code
      # must not be able to forge or clear it.
      "d /var/lib/ix-runner 0700 root root -"
      "d /var/lib/ix-runner-home 0755 root root -"
      "d /var/lib/ix-runner-tmp 0755 root root -"
    ]
    # Owned by the slot's own user, so 0750/0700 is a real boundary between
    # slots rather than decoration over one shared uid.
    ++ map (name: "d /var/lib/ix-runner-work/${name} 0750 ${name} ${name} -") runnerNames
    # Caches, so never aged out.
    ++ map (name: "d ${homeOf name} 0700 ${name} ${name} -") runnerNames
    # Per-slot rather than one shared 1777 dir: a shared temp is a cross-slot
    # channel that the sticky bit does not close, while per-slot dirs make a
    # job's own cleanup step safe and bound what one leaking job fills.
    ++ map (name: "d ${tmpOf name} 0700 ${name} ${name} 1d") runnerNames
    # The runner's _diag logs (LogsDirectory=) are never rotated and this VM
    # is long-lived. The dirs themselves are systemd's; only age the contents.
    ++ map (name: "e /var/log/github-runner/${name} - - - 30d") runnerNames;

    systemd.services =
      # Without the token the units SKIP instead of FAIL: the first boot of a
      # fresh template rev switches cleanly. The reconcile attaches the token
      # at create, so it is present from the first boot.
      genAttrs runnerUnits (_: {
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
          before = map (unit: "${unit}.service") runnerUnits;
          unitConfig.ConditionPathIsMountPoint = "/tmp";
          serviceConfig = {
            Type = "oneshot";
            RemainAfterExit = true;
            ExecStart = "${pkgs.util-linux}/bin/mount -o remount,size=16G /tmp";
          };
        };
      };

    # kernel.threads-max (and the RLIMIT_NPROC default derived from it) is
    # computed from BOOT-time RAM and never recomputed when virtio-mem grows
    # the guest; concurrent cargo jobs seeing the elastic vCPU ceiling hit
    # pthread_create EAGAIN. Pin real numbers.
    boot.kernel.sysctl = {
      "kernel.threads-max" = 1048576;
      "kernel.pid_max" = 4194304;
    };

    # Guests boot small and grow elastically: a job that outruns virtio-mem
    # should swap and slow down, not hard-OOM. Compressed swap also keeps
    # resident (billed) RAM down between jobs.
    zramSwap.enable = true;

    # The store on a persistent VM only ever grows: every job's `nix build`
    # output stays until something collects it.
    nix.gc = {
      automatic = true;
      dates = "weekly";
      options = "--delete-older-than 30d";
    };

    programs.nix-ld = {
      enable = true;
      # Deliberately GENEROUS. Every entry is one less loader failure for a
      # foreign FHS binary a job downloads at runtime (mise/rustup
      # toolchains, prebuilt node, cargo-binstall artifacts, playwright
      # browsers, AppImages) - the reactive add-a-lib-per-failure loop this
      # replaces cost one runner-fleet debugging round per new binary.
      # Baseline: the community nix-ld set (wiki.nixos.org/wiki/Nix-ld)
      # minus desktop-app one-offs (SDL/game runtimes, gtk2/gnome2 legacy,
      # EOL libpng12/glew110) plus the dotnet-runner set, and minus what the
      # nixos nix-ld module concatenates in on its own (zlib zstd
      # stdenv.cc.cc curl openssl attr libssh bzip2 libxml2 acl libsodium
      # util-linux xz systemd). The cost is image closure size only; the
      # list dies when nixpkgs#354513 (nix-ld resolves against the whole
      # system closure) lands.
      libraries = with pkgs; [
        # Core toolchain and compression
        libgcc
        libxcrypt
        libxcrypt-legacy
        gmp
        libelf
        # Crypto, TLS, network
        expat
        libgcrypt
        libgpg-error
        krb5
        # System plumbing
        dbus
        libcap
        libusb1
        fuse # AppImages
        e2fsprogs
        icu
        # dotnet-based tools (the Actions runner itself, omnisharp class)
        lttng-ust
        libsecret
        # X11
        libx11
        libxext
        libxcomposite
        libxdamage
        libxfixes
        libxrandr
        libxcursor
        libxi
        libxinerama
        libxrender
        libxscrnsaver
        libxtst
        libxt
        libxmu
        libxft
        libsm
        libice
        libxshmfence
        libxxf86vm
        libxcb
        libxcb-util
        libxcb-wm
        libxcb-image
        libxcb-keysyms
        libxcb-render-util
        libxcb-cursor
        libxkbcommon
        # Graphics and rendering
        libGL
        libGLU
        vulkan-loader
        mesa
        # libgbm was split out of mesa in nixpkgs; chromium's headless shell
        # dlopens libgbm.so.1 (its absence was the one failure after
        # browsers started executing from HOME).
        libgbm
        libdrm
        libva
        libvdpau
        pixman
        libjpeg
        libpng
        libtiff
        librsvg
        fontconfig
        freetype
        harfbuzz
        fribidi
        gdk-pixbuf
        # GUI toolkits (electron, prebuilt GUI test tools)
        glib
        gtk3
        pango
        cairo
        atk
        at-spi2-atk
        at-spi2-core
        gsettings-desktop-schemas
        libnotify
        # Playwright-downloaded chromium/firefox/webkit runtime set: jobs
        # install browsers into their persistent HOME exactly as on Ubuntu
        # images, and nix-ld makes them executable here.
        nss
        nspr
        cups
        alsa-lib
        # Audio and media
        libpulseaudio
        pipewire
        flac
        libvorbis
        libogg
        speex
        libsamplerate
        ffmpeg
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
      # trusted-users is root-equivalent (it can point the daemon at any
      # substituter or import any path). Job code runs as its own per-slot
      # user on a machine that outlives the job, so it must not have it.
      trusted-users = [ "root" ];
    };
  };
}

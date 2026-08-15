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
# rather than any fixed home path, and must not put these users in wheel,
# docker, or nix trusted-users (asserted below).
#
# ONE template serves the whole pool (#18). Nothing here knows which member
# this VM is at build time: the member index arrives at boot and the runner
# names are assembled from it (see the identity unit). That is also why the
# runner units are written out here instead of coming from
# `services.github-runners` - the upstream module wants every runner name at
# eval time, which is exactly the coupling that made the pool N templates.
{
  config,
  lib,
  pkgs,
  ...
}:
let
  inherit (lib)
    any
    concatStringsSep
    elem
    escapeShellArg
    escapeShellArgs
    genAttrs
    listToAttrs
    mkEnableOption
    mkIf
    mkOption
    nameValuePair
    optional
    range
    types
    ;
  cfg = config.services.ix-runner;

  # Slot count is static policy, so everything keyed on a SLOT is build-time:
  # units, users, directories. Only the member index - and so the runner
  # names, which embed it - is boot-time.
  slotNumbers = range 1 cfg.slots;
  # The shell side of the runner units derives the same name from $SLOT, so
  # the prefix is one binding rather than a literal in two languages.
  slotUserPrefix = "ci-slot-";
  slotUser = n: "${slotUserPrefix}${toString n}";
  slotUnit = n: "github-runner-slot-${toString n}";
  # Keyed by slot, so systemd owns stable per-slot state, runtime and log
  # directories even though the runner NAME inside them is boot-derived.
  systemdDirOf = n: "github-runner/slot-${toString n}";
  slotUsers = map slotUser slotNumbers;
  slotUnits = map slotUnit slotNumbers;
  slotUnitServices = map (unit: "${unit}.service") slotUnits;
  tokenPermsUnit = "ix-runner-token-perms";
  identityUnit = "ix-runner-identity";

  # One UNIX user per slot, named after the slot. A shared uid would make the
  # per-slot HOME/TMPDIR cosmetic: any job could read a co-tenant slot's
  # runner .credentials (and re-register that runner elsewhere), its steps'
  # /proc/<pid>/environ, and its at-rest ~/.npmrc, ~/.docker, ~/.cargo.
  # Warm caches stay per-slot either way, so nothing is lost by splitting.
  # A VM hosts exactly one member, so slot-keyed users draw the same boundary
  # the member-keyed ones did.
  #
  # Per-slot, off the workDir that is wiped on every start.
  homeOf = name: "/var/lib/ix-runner-home/${name}";
  tmpOf = name: "/var/lib/ix-runner-tmp/${name}";
  workOf = name: "/var/lib/ix-runner-work/${name}";

  # Consumer footguns that are silently root-equivalent on a machine running
  # untrusted job code. Read from the FINAL config, so a policy module adding
  # them anywhere fails the build rather than shipping quietly.
  extraGroupsOf = name: config.users.users.${name}.extraGroups or [ ];
  inGroup =
    group: name:
    elem name (config.users.groups.${group}.members or [ ]) || elem group (extraGroupsOf name);

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
  # the shim.
  runnerPackage =
    let
      base = pkgs.github-runner.override { nodeRuntimes = [ "node24" ]; };
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
      '';

  # --- member identity -----------------------------------------------------
  #
  # Written by the identity unit at boot. The pin is the authority and lives
  # in the root-only state dir; the published copy is what the unprivileged
  # configure step reads. Both are root-owned: a job that could forge either
  # would rename its own runner, and with `--replace` that means taking over
  # another member's registration.
  pinnedIndexFile = "/var/lib/ix-runner/member-index";
  publishedIndexFile = "/run/ix-runner/member-index";

  # Resolve this VM's member index N, pin it, publish it. Refuse loudly
  # rather than guess: a wrong index produces another member's runner names.
  #
  # Sources, in order:
  #   1. cfg.memberIndexFile - attached per machine at create through the ix
  #      secret store, the same channel the registration token rides.
  #   2. the hostname, when the platform names the guest after the machine
  #      (`<poolName>-runner-<N>`). ix does NOT do this today: a guest's
  #      hostname comes from its image closure, so every member booted from
  #      one shared template reads the same name. Kept because it is the
  #      channel this design wants and it costs nothing to accept.
  #   3. the pin from an earlier boot, which also OVERRIDES a source that
  #      disagrees - identity may never change while registrations exist.
  memberIndexScript = pkgs.writeShellScript "ix-runner-member-index" ''
    set -euo pipefail
    export PATH=${pkgs.coreutils}/bin

    valid() { [[ "$1" =~ ^[1-9][0-9]*$ ]]; }

    candidate=""
    origin=""
    host=$(cat /proc/sys/kernel/hostname)
    if [[ -e ${escapeShellArg cfg.memberIndexFile} ]]; then
      origin=${escapeShellArg cfg.memberIndexFile}
      # Present but unreadable or malformed is a refusal, never a fall-through
      # to a weaker source: something meant to name this VM and failed.
      candidate=$(tr -d '[:space:]' < ${escapeShellArg cfg.memberIndexFile})
      if ! valid "$candidate"; then
        echo "REFUSING TO START THE RUNNER POOL: $origin holds [$candidate]," >&2
        echo "which is not a member index (a positive integer, no leading zeros)." >&2
        exit 1
      fi
    elif [[ "$host" =~ ^${cfg.poolName}-runner-([1-9][0-9]*)$ ]]; then
      candidate="''${BASH_REMATCH[1]}"
      origin="hostname [$host]"
    fi

    install -d -m 0700 -o root -g root "$(dirname ${pinnedIndexFile})"
    if [[ -e ${pinnedIndexFile} ]]; then
      index=$(tr -d '[:space:]' < ${pinnedIndexFile})
      valid "$index" || {
        echo "REFUSING TO START THE RUNNER POOL: the pinned member index" >&2
        echo "${pinnedIndexFile} holds [$index], which is not a member index." >&2
        exit 1
      }
      if [[ -n "$candidate" && "$candidate" != "$index" ]]; then
        echo "REFUSING TO START THE RUNNER POOL: this VM registered runners as member" >&2
        echo "[$index] and now $origin says it is member [$candidate]. Renaming a live" >&2
        echo "member orphans its registrations on GitHub and, because runners register" >&2
        echo "with --replace, would take over member [$candidate]'s slots. Replace the VM" >&2
        echo "instead; the reconcile does that for you." >&2
        exit 1
      fi
    elif [[ -n "$candidate" ]]; then
      index="$candidate"
      printf '%s\n' "$index" > ${pinnedIndexFile}.tmp
      chmod 0400 ${pinnedIndexFile}.tmp
      mv -f ${pinnedIndexFile}.tmp ${pinnedIndexFile}
    else
      echo "REFUSING TO START THE RUNNER POOL: this VM does not know which pool member" >&2
      echo "it is. One template serves the whole pool, so the member index is delivered" >&2
      echo "at boot rather than baked. Checked, in order:" >&2
      echo "  - ${cfg.memberIndexFile}: absent" >&2
      echo "  - hostname [$host]: does not match ${cfg.poolName}-runner-<N>" >&2
      echo "Attach the member index as a secret file at machine create, the same way the" >&2
      echo "registration token is attached. Guessing is not an option: the index picks the" >&2
      echo "runner names, and the wrong one takes over another member's registrations." >&2
      exit 1
    fi

    install -d -m 0755 -o root -g root "$(dirname ${publishedIndexFile})"
    printf '%s\n' "$index" > ${publishedIndexFile}.tmp
    chmod 0444 ${publishedIndexFile}.tmp
    mv -f ${publishedIndexFile}.tmp ${publishedIndexFile}
    echo "pool member index $index"
  '';

  # Shared prologue for the per-slot ExecStartPre scripts: same argument
  # contract as the upstream unit (state, work, logs directories) plus the
  # slot number, since one script now serves every slot.
  slotScript =
    name: lines:
    pkgs.writeShellScript "github-runner-slot-${name}.sh" ''
      set -euo pipefail

      STATE_DIRECTORY="$1"
      WORK_DIRECTORY="$2"
      LOGS_DIRECTORY="$3"
      SLOT="$4"
      SLOT_USER="${slotUserPrefix}''${SLOT}"
      SLOT_GROUP="$SLOT_USER"

      MEMBER=""
      if [[ -r ${publishedIndexFile} ]]; then
        MEMBER=$(${pkgs.coreutils}/bin/tr -d '[:space:]' < ${publishedIndexFile})
      fi
      [[ "$MEMBER" =~ ^[1-9][0-9]*$ ]] || {
        echo "no usable member index at ${publishedIndexFile} [$MEMBER];" >&2
        echo "${identityUnit}.service should have refused before this ran" >&2
        exit 1
      }
      # The one name the reconcile matches on: `<poolName>-r<member>-<slot>`.
      RUNNER_NAME="${cfg.poolName}-r''${MEMBER}-''${SLOT}"

      # Registration inputs, rendered rather than baked because the name is
      # only known now. Compared against the recorded copy to decide whether
      # this start has to re-register (upstream compares a config JSON).
      render_config() {
        printf 'url=%s\nlabels=%s\nname=%s\nwork=%s\nephemeral=false\nreplace=true\n' \
          ${escapeShellArg cfg.url} \
          ${escapeShellArg (concatStringsSep "," cfg.labels)} \
          "$RUNNER_NAME" \
          "$WORK_DIRECTORY"
      }

      ${lines}
    '';

  currentConfigFile = "$STATE_DIRECTORY/.ix-runner-config";
  newTokenFile = "$STATE_DIRECTORY/.new-token";
  currentTokenFile = "$STATE_DIRECTORY/.current-token";

  # Runs as ROOT (the `+` prefix below): it reads the registration token.
  # Purges runner state when a registration input or the token changed, so
  # the next step re-registers; always wipes the checkout directory.
  unconfigureScript = slotScript "unconfigure" ''
    copy_tokens() {
      # The runner reads the token as the slot user, so hand it to that user
      # alone (upstream installs it 0666 inside the state dir).
      install -m 0400 -o "$SLOT_USER" -g "$SLOT_GROUP" ${escapeShellArg cfg.tokenFile} "${newTokenFile}"
      # Root-only copy, kept for the next start's comparison.
      install -m 0400 -o root -g root ${escapeShellArg cfg.tokenFile} "${currentTokenFile}"
    }
    clean_state() {
      ${pkgs.findutils}/bin/find "$STATE_DIRECTORY/" -mindepth 1 -delete
      copy_tokens
    }
    if [[ -n "$(ls -A "$STATE_DIRECTORY")" ]]; then
      changed=0
      [[ -f "${currentConfigFile}" ]] \
        && render_config | ${pkgs.diffutils}/bin/diff -q - "${currentConfigFile}" >/dev/null 2>&1 \
        || changed=1
      [[ -f "${currentTokenFile}" ]] \
        && ${pkgs.diffutils}/bin/diff -q "${currentTokenFile}" ${escapeShellArg cfg.tokenFile} >/dev/null 2>&1 \
        || changed=1
      if [[ "$changed" -eq 1 ]]; then
        echo "registration inputs changed; discarding this slot's runner state."
        echo "The old runner stays in the GitHub Actions UI until it is removed there."
        clean_state
      fi
    else
      copy_tokens
    fi
    # The checkout, never a cache: wiped on every start (see the header).
    ${pkgs.findutils}/bin/find -H "$WORK_DIRECTORY" -mindepth 1 -delete
  '';

  # Runs as the SLOT USER. Registration is the only step that needs the
  # token, and it happens once per VM life.
  configureScript = slotScript "configure" ''
    if [[ -e "${newTokenFile}" ]]; then
      echo "registering $RUNNER_NAME"
      token=$(<"${newTokenFile}")
      # tokenFile is contractually a 1 h registration token. A PAT would work
      # here and then sit on a machine that runs untrusted jobs for weeks.
      if [[ "$token" =~ ^gh[a-z]+_ || "$token" =~ ^github_pat_ ]]; then
        echo "REFUSING TO REGISTER: ${cfg.tokenFile} holds a personal access token." >&2
        echo "This pool takes short-lived registration tokens only - a PAT on a VM that" >&2
        echo "runs untrusted job code is a standing credential for the whole repository." >&2
        exit 1
      fi
      ${runnerPackage}/bin/Runner.Listener configure \
        --unattended \
        --disableupdate \
        --replace \
        --work "$WORK_DIRECTORY" \
        --url ${escapeShellArg cfg.url} \
        --labels ${escapeShellArg (concatStringsSep "," cfg.labels)} \
        --name "$RUNNER_NAME" \
        --token "$token"
      # Move the _diag the runner just created into the logs directory.
      mkdir -p "$STATE_DIRECTORY/_diag"
      cp -r "$STATE_DIRECTORY/_diag/." "$LOGS_DIRECTORY/"
      rm -rf "$STATE_DIRECTORY/_diag/"
      rm "${newTokenFile}"
    fi
  '';

  # Runs as the SLOT USER, on the freshly wiped work directory.
  setupWorkDirScript = slotScript "setup-work-dirs" ''
    ln -s "$LOGS_DIRECTORY" "$WORK_DIRECTORY/_diag"
    ln -s "$STATE_DIRECTORY"/{.credentials,.credentials_rsaparams,.runner} "$WORK_DIRECTORY/"
  '';

  # Runs as ROOT, and only after a successful configure - recording earlier
  # would make a failed registration look current on the next start, and the
  # slot would never retry it.
  recordConfigScript = slotScript "record-config" ''
    render_config > "${currentConfigFile}".tmp
    chmod 0400 "${currentConfigFile}".tmp
    mv -f "${currentConfigFile}".tmp "${currentConfigFile}"
  '';

  # One runner daemon = one job slot. This is the upstream
  # `services.github-runners` unit rewritten so the runner NAME can come from
  # boot rather than from eval; every upstream hardening default is repeated
  # here explicitly, with this pool's overrides folded in rather than layered
  # as mkForce over an inherited default.
  slotService =
    n:
    let
      user = slotUser n;
      systemdDir = systemdDirOf n;
      # %S, %L: state and log directory roots; see systemd.unit(5).
      stateDir = "%S/${systemdDir}";
      logsDir = "%L/${systemdDir}";
      workDir = workOf user;
      preArgs = escapeShellArgs [
        stateDir
        workDir
        logsDir
        (toString n)
      ];
    in
    {
      description = "GitHub Actions runner slot ${toString n}";

      wantedBy = [ "multi-user.target" ];
      wants = [ "network-online.target" ];
      after = [
        "network.target"
        "network-online.target"
        "${identityUnit}.service"
        "${tokenPermsUnit}.service"
      ];
      # Requires, not just After: an ordering edge alone would let the slot
      # start anyway when the identity or the permission check fails. A check
      # that condition-skips (no token) still satisfies this.
      requires = [
        "${identityUnit}.service"
        "${tokenPermsUnit}.service"
      ];
      # Without the token the unit SKIPS instead of FAILING: the first boot of
      # a fresh template rev switches cleanly. The reconcile attaches the
      # token at create, so it is present from the first boot.
      unitConfig.ConditionPathExists = cfg.tokenFile;

      environment = {
        # Upstream defaults HOME to workDir, which it wipes on every start;
        # a warm toolchain/registry cache needs a directory nothing clears.
        HOME = homeOf user;
        RUNNER_ROOT = stateDir;
        # Disk-backed job temp, off the small boot-path /tmp (issue #2);
        # aged out by the tmpfiles rule below.
        TMPDIR = tmpOf user;
      }
      // nixLdEnvironment
      // cfg.jobEnvironment;

      path = basePackages ++ [ config.nix.package ] ++ cfg.extraPackages;

      serviceConfig = {
        ExecStart = "${runnerPackage}/bin/Runner.Listener run --startuptype service";
        # In order: purge state if a registration input changed (as root, it
        # reads the token), register if there is a token to register with,
        # link the work dir, then record what we registered with.
        ExecStartPre = [
          "+${unconfigureScript} ${preArgs}"
          "${configureScript} ${preArgs}"
          "${setupWorkDirScript} ${preArgs}"
          "+${recordConfigScript} ${preArgs}"
        ];

        User = user;
        Group = user;
        DynamicUser = false;

        LogsDirectory = [ systemdDir ];
        RuntimeDirectory = [ systemdDir ];
        StateDirectory = [ systemdDir ];
        StateDirectoryMode = "0700";
        # systemd's 0755 default would publish this slot's runner _diag logs
        # and runtime dir to every other slot's user.
        LogsDirectoryMode = "0700";
        RuntimeDirectoryMode = "0700";
        WorkingDirectory = workDir;

        InaccessiblePaths = [
          # The registration token, and the root-only copy of it kept in the
          # state directory for the next start's comparison.
          "-${cfg.tokenFile}"
          "${stateDir}/.current-token"
        ];
        KillSignal = "SIGINT";

        # The kernel OOM-killing one compiler process must not take the slot
        # down with it; systemd's default stop policy plus upstream's
        # Restart="no" for non-ephemeral runners made that permanent, and
        # invisibly so - the reconcile reads any-slot-online as healthy.
        OOMPolicy = "continue";
        Restart = "always";
        RestartSec = "10s";
        # Hosted runners give jobs 65536; the systemd default is 1024.
        LimitNOFILE = 1048576;
        # One runaway job must not exhaust system PIDs and take the co-tenant
        # slots with it. TasksMax bounds the slot's cgroup whatever uid the
        # tasks end up under (containers, setuid helpers), which per-UID
        # LimitNPROC cannot.
        TasksMax = 65536;
        LimitNPROC = "infinity";
        # Turns the cpu controller ON for the slice, so busy slots get fair
        # shares instead of competing globally (observed live: upstream-green
        # wall-clock test bounds missed only under slot contention). 100 is
        # systemd's default weight - this line is here for the controller,
        # not for the number.
        CPUWeight = "100";

        # Upstream's hardening set, verbatim except where noted.
        AmbientCapabilities = [ "" ];
        CapabilityBoundingSet = [ "" ];
        DeviceAllow = [ "" ];
        NoNewPrivileges = true;
        PrivateDevices = true;
        PrivateMounts = true;
        ProtectClock = true;
        ProtectControlGroups = true;
        ProtectHostname = true;
        ProtectKernelLogs = true;
        ProtectKernelModules = true;
        ProtectKernelTunables = true;
        ProtectProc = "invisible";
        ProtectSystem = "strict";
        RemoveIPC = true;
        RestrictRealtime = true;
        RestrictSUIDSGID = true;
        # Needs network access.
        PrivateNetwork = false;
        # Cannot be true due to Node.
        MemoryDenyWriteExecute = false;
        # "pid" makes `nix` commands emit "GC Warning: Couldn't read /proc/stat".
        ProcSubset = "all";
        # Coverage tooling (cargo-tarpaulin and friends) disables ASLR, which
        # needs the personality syscall.
        LockPersonality = false;
        SystemCallFilter = [
          "~@clock"
          "~@cpu-emulation"
          "~@module"
          "~@mount"
          "~@obsolete"
          "~@raw-io"
          "~@reboot"
          "~capset"
          "~setdomainname"
          "~sethostname"
        ];
        RestrictAddressFamilies = [
          "AF_INET"
          "AF_INET6"
          "AF_UNIX"
          "AF_NETLINK"
        ];

        # ProtectHome=true masks /home entirely; this slot's home is its
        # passwd home under /var/lib, and tooling that resolves ~ from passwd
        # rather than $HOME must reach the same directory.
        ProtectHome = false;
        # ProtectSystem=strict: the work dir, this slot's HOME and its TMPDIR
        # must stay writable.
        BindPaths = [ workDir ];
        ReadWritePaths = [
          (homeOf user)
          (tmpOf user)
        ];
        # Hosted-runner parity: 0066 leaves job artifacts unreadable to other
        # UIDs, which breaks containers and bind mounts reading the checkout.
        # /tmp is read-only under ProtectSystem=strict and $TMPDIR is the
        # per-slot writable temp; do not add /tmp to ReadWritePaths (a shared
        # writable /tmp would reintroduce a cross-slot channel).
        UMask = "0022";
        # chromium and playwright ship their own sandbox, which needs user
        # and pid namespaces. The VM is the isolation boundary here.
        RestrictNamespaces = false;
        PrivateUsers = false;
        # dockerd resolves `-v /tmp/...` in the host namespace, so a private
        # /tmp silently hands the container an empty directory. The VM is the
        # boundary, and TMPDIR points elsewhere anyway.
        PrivateTmp = false;
      };
    };
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
        Pool name, usually the repository name. Every runner name derives
        from it: `<poolName>-r<N>-<slot>`, where N is this VM's member index
        (resolved at boot, see `memberIndexFile`). Runner names must be
        unique fleet-wide - GitHub rejects duplicates, and `replace = true`
        would otherwise let one VM silently steal another's registration.
        Must be hostname-safe: the platform names the VM
        `<poolName>-runner-<N>`, which is also the fallback the member index
        is read from.
      '';
    };

    memberIndexFile = mkOption {
      type = types.str;
      default = "/run/secrets/member-index";
      description = ''
        File holding this VM's member index N - a positive integer, no
        leading zeros - attached per machine at create through the ix secret
        store, the same channel as `tokenFile`.

        ONE template serves the whole pool, so N cannot be baked. It is
        resolved at boot, pinned to /var/lib/ix-runner/member-index, and
        never allowed to change while the VM lives: the index picks the
        runner names, those names are registered on GitHub, and a member
        that renamed itself would orphan its registrations and (registering
        with `--replace`) take over the member it renamed itself to.

        The fallback source is the hostname, `<poolName>-runner-<N>`. ix
        does not name guests after their machine today - a guest hostname
        comes from its image closure, so every member of a shared template
        reads the same name - which is why this file is the live path. With
        neither source the slots stay down behind one loud refusal, rather
        than several members registering the same runner names.
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
          hostname-safe. It prefixes every runner name and the VM hostname
          the member index is read back from, so it must be lowercase
          alphanumerics and hyphens, starting and ending with an
          alphanumeric.
        '';
      }
    ]
    ++ map (name: {
      assertion = !(elem name config.nix.settings.trusted-users);
      message = ''
        The runner user "${name}" is in nix.settings.trusted-users. A trusted
        user can point the nix daemon at any substituter and import any store
        path into the system's store, which is root-equivalent - and this user
        runs untrusted job code on a VM that outlives the job. Remove it from
        trusted-users; jobs do not need it to run `nix build`.
      '';
    }) slotUsers
    ++ map (name: {
      assertion = !(inGroup "wheel" name);
      message = ''
        The runner user "${name}" is in the wheel group (via
        users.groups.wheel.members or users.users."${name}".extraGroups).
        wheel means sudo, so every job on this slot is root on the VM and can
        read every other slot's runner credentials. Remove it.
      '';
    }) slotUsers;

    warnings = optional (any (inGroup "docker") slotUsers) ''
      A runner user is in the "docker" group. docker group membership is
      root-equivalent on a machine that runs untrusted CI; the pool is then
      only as safe as the workflow runs-on gating that keeps untrusted events
      off it.
    '';

    environment.etc = mkIf (cfg.configRev != null) {
      "ix-runner/rev".text = cfg.configRev;
    };

    # One user and group per slot: the slot boundary is a uid boundary, so a
    # job cannot read a co-tenant slot's credentials, environ, or caches.
    # passwd home == $HOME, so tooling that resolves ~ from passwd agrees
    # with the unit environment.
    users.users = genAttrs slotUsers (name: {
      isSystemUser = true;
      group = name;
      home = homeOf name;
      createHome = true;
      shell = pkgs.bashInteractive;
    });
    users.groups = genAttrs slotUsers (_: { });

    systemd.tmpfiles.rules = [
      # 0700 root: the reconcile's strike marker lives here, and job code
      # must not be able to forge or clear it.
      "d /var/lib/ix-runner 0700 root root -"
      "d /var/lib/ix-runner-home 0755 root root -"
      "d /var/lib/ix-runner-tmp 0755 root root -"
    ]
    # Owned by the slot's own user, so 0750/0700 is a real boundary between
    # slots rather than decoration over one shared uid.
    ++ map (name: "d ${workOf name} 0750 ${name} ${name} -") slotUsers
    # Caches, so never aged out.
    ++ map (name: "d ${homeOf name} 0700 ${name} ${name} -") slotUsers
    # Per-slot rather than one shared 1777 dir: a shared temp is a cross-slot
    # channel that the sticky bit does not close, while per-slot dirs make a
    # job's own cleanup step safe and bound what one leaking job fills.
    ++ map (name: "d ${tmpOf name} 0700 ${name} ${name} 1d") slotUsers
    # The runner's _diag logs (LogsDirectory=) are never rotated and this VM
    # is long-lived. The dirs themselves are systemd's; only age the contents.
    ++ map (n: "e /var/log/${systemdDirOf n} - - - 30d") slotNumbers;

    systemd.services =
      listToAttrs (map (n: nameValuePair (slotUnit n) (slotService n)) slotNumbers)
      // {
        # Which member of the pool this VM is - the one fact a shared
        # template cannot carry. Ordered before every slot and REQUIRED by
        # them, so an unresolvable identity keeps the pool down instead of
        # registering runner names that belong to another member.
        ${identityUnit} = {
          description = "resolve which pool member this VM is";
          wantedBy = [ "multi-user.target" ];
          before = slotUnitServices;
          # No ConditionPathExists twin of the token units: a VM that cannot
          # say who it is has nothing to skip cleanly into.
          serviceConfig = {
            Type = "oneshot";
            RemainAfterExit = true;
            ExecStart = memberIndexScript;
          };
        };

        # The registration token is delivered root-only by the platform; a
        # job-readable one would let any slot re-register this whole pool
        # somewhere else, so verify rather than trust. Refusing here keeps
        # the slots down instead of starting them around a leaked secret.
        ${tokenPermsUnit} = {
          description = "verify the runner registration token is root-only";
          wantedBy = [ "multi-user.target" ];
          before = slotUnitServices;
          # Same skip-not-fail contract as the runner units: a boot without
          # the secret switches cleanly.
          unitConfig.ConditionPathExists = cfg.tokenFile;
          serviceConfig = {
            Type = "oneshot";
            RemainAfterExit = true;
            ExecStart = pkgs.writeShellScript "ix-runner-token-perms" ''
              set -euo pipefail
              perms=$(${pkgs.coreutils}/bin/stat -c '%U %G %a' ${cfg.tokenFile})
              case "$perms" in
                'root root 600' | 'root root 400') ;;
                *)
                  echo "REFUSING TO START THE RUNNER POOL: ${cfg.tokenFile} is [$perms]," >&2
                  echo "expected [root root 600] (or 400). The GitHub registration token must be" >&2
                  echo "readable by root only - anything else exposes it to job code, which can" >&2
                  echo "then register runners for this repository on any machine it controls." >&2
                  exit 1
                  ;;
              esac
            '';
          };
        };

        # /tmp is a tmpfs sized off BOOT-time RAM (1.5G observed, 100% full
        # under concurrent jobs) and closure tmpfs settings do not reach
        # image boots - a live remount does (ix platform, see issue #2).
        # 16G virtual is safe: tmpfs pages only cost when used, and
        # virtio-mem plugs RAM on demand.
        ix-runner-tmp-resize = {
          description = "resize the boot-path /tmp tmpfs";
          wantedBy = [ "multi-user.target" ];
          before = slotUnitServices;
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
      # Defense in depth for the co-tenant slots: without Yama scope, one
      # slot's job could ptrace-attach to another slot's steps. ix guests do
      # not get the closure's lsm= kernel cmdline, so Yama may be inactive
      # and this sysctl is the only place its scope is guaranteed.
      "kernel.yama.ptrace_scope" = 1;
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
      # user on a machine that outlives the job, so it must not have it -
      # asserted above, since a policy module could add it back.
      trusted-users = [ "root" ];
    };
  };
}

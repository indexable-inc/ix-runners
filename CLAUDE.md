# CLAUDE.md

Fork-per-job GitHub Actions runners on ix VMs. Read README.md for the
mechanism (seed/fork/promote lifecycle) and docs/design.md for why.

## Platform bugs get fixed in ix, never duct-taped here

When a failure traces to the hypervisor or the ix platform (memory
elasticity, vCPU advertising, guest device/network behavior, snapshot/fork
semantics), the fix goes into ix itself — not into this repo's module,
pool policies, or job environment pins. (Directive, 2026-08-25.)

Evidence: guests are provisioned at 4 GiB/vCPU, yet a customer lane was
OOM-killed at ~60 GiB resident on a guest advertising ~100 vCPUs — a
platform-side memory bug. The duct-tape response was per-pool parallelism
pins (CARGO_BUILD_JOBS / NEXTEST_TEST_THREADS / VITEST_MAX_WORKERS et al.)
compensating in the image for what the platform owes: advertised width x
4 GiB/vCPU, deliverable at allocation speed. Those pins are debt, deleted
when the platform invariant is verified.

Rules:

- Diagnose to the boundary first: guest cgroup limits + dmesg OOM lines +
  host-side VM logs say which side broke. Do not guess from the symptom.
- Platform side broke: fix it in ix. Prefer a red pool over a
  green-but-pinned one — a workaround that makes jobs green hides the bug
  from the people who own the platform.
- A workaround is acceptable only when the ix fix is decided and in
  flight, the workaround's comment names that fix, and deleting the
  workaround is part of the fix's definition of done. platform.nix's
  gai.conf IPv4 preference is the model: "remove once v6 delivery lands."

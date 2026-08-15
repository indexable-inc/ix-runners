# Design notes

Why the reconcile behaves the way it does. The [README](../README.md) says
what it does; this says why, and records the failures that shaped it. None of
it is needed to run a pool.

The code itself carries the same reasoning at a finer grain - each module's
docstring explains its own job, and `reconcile/planner.py` is the whole
decision procedure as one readable function.

## Level-based, and stateless on purpose

Every tick observes the world fresh - machine rows, runner registrations,
GitHub's job queue - computes a level, and moves toward it. Nothing is
remembered between runs: no idle counters, no pending-action list, no event
log to replay.

The property this buys is that a missed tick costs latency and never
correctness, and two reconciles racing cannot hold different beliefs about
the pool. An earlier version counted consecutive idle ticks on the VM itself;
it worked, but the grace silently changed length whenever the cron did, and
the count could survive a stop and be spent the moment the machine came back.
Deriving the same thing from GitHub's own timestamps removed a whole class of
question.

## Why "servable" is strict

A job counts toward demand only when some runner in this pool advertises
*every* label in its `runs-on`. GitHub ANDs those labels and checks them
against a single runner, so two machines cannot combine to cover one job.

This is not pedantry. Repos routinely have jobs queued indefinitely against
labels nothing in the pool carries - `blacksmith-*`, `macos-*`, hosted-only
labels - work that will never be served from here. Counting them is not a
conservative error, it is a permanent maximum: the pool sits at `max-online`
forever, running nothing, and it *looks* like the feature working. The pilot
repo had exactly this, three runs deep, before the matching rule was
tightened.

Labels are read off the runners' own registrations rather than configured, so
what the pool advertises and what it is matched on cannot drift apart.
`runner-label` in `ix-pool.toml` is only the fallback for a pool that has not
registered anything yet, and `mkPool` asserts at build time that it is one of
`services.ix-runner.labels` - a label nothing advertises means the pool never
wakes for a wave, silently, because zero *servable* jobs and zero jobs look
identical.

## The two rules that stop a wave throttling itself

**Only a scheduled tick may scale down.** An event tick fires when a run is
*requested*, which is the moment before its jobs appear in the queue. The
pool therefore looks idle at exactly the moment a wave is landing, and a
scale-down there would switch machines off at the start of it. Event ticks
may only add capacity.

**A tick that cannot read GitHub makes no scaling decision at all.** Missing
data is not zero demand, and it is not zero idleness either: guessing up
costs money forever, guessing down stops machines that are about to be handed
a job. Healing still runs; only scaling is skipped. An earlier version scaled
*up* on a failed read, which combined with the unfiltered demand count would
have pinned the pilot pool at maximum permanently.

## Idle time is derived, not counted

A member's idle clock is the last job completion GitHub recorded against its
runners, read in the same pass as demand. No stored counter, nothing to
reset, nothing that can fall out of step with reality.

A member the scan never saw finish anything is idle only as far back as the
scan's own window reaches. On a repo busy enough to make that window shorter
than the grace, absence from it is not evidence of idleness, and the tick
simply does not scale down.

## Scale-down deregisters before it cuts power

GitHub refuses (422) to delete a runner that is mid-job, and that refusal is
the only real lock in this system: once the registration is gone the runner
cannot be assigned anything at all, which is a guarantee no amount of
re-reading a listing can give. Checking `busy` and then stopping always
leaves a window in which a job is assigned and then killed by the stop. This
closes it rather than narrowing it, and it reuses the mechanism the replace
path already proved.

### What that costs

A stop is no longer free to undo. The registration is gone, so waking a
member means minting a fresh registration token, overwriting the pool's
secret, and letting the platform deliver it: the machine re-registers at boot
precisely *because* the token file changed.

Two consequences follow, and both are load-bearing.

**The secret row is never deleted.** The platform propagates a rotation to
machines already holding a copy - stopped ones included, where it lands in
the machine's secret row and is delivered at next boot - but only when the
write *updates* an existing row. A write that inserts is a first write and
reaches nobody. The reconcile used to delete the spent token at the end of
every run, which would have made every subsequent write an insert, and no
stopped member would ever have received a usable token again. The wake path
would have been broken from the first day, silently.

**A rotation reaches running members too**, as a new token file. Those
members re-register on their next unit restart, which is fine while the token
is fresh and fails once it has expired. The reconcile mints in the same tick
as any repair, so a restart it drives always holds a new token; an unplanned
reboot long after the last rotation can need one repair cycle to come back.

## Member lifecycle, in detail

The README's table is the summary. The reasoning behind the less obvious rows:

- **Still booting is skipped, but a young machine is not the same as a fresh
  one.** The boot grace is measured from machine *creation*. A member created
  last week and started twenty seconds ago sails past it, and reading its
  silence as "unreachable" deletes the disk - and with it the registration
  credentials that make a stop cheap in the first place. Autoscaling produces
  that state on every scale-up, so "started recently" is a separate check
  from "created recently".

- **`failed` does not wait out the grace.** The grace exists because a young
  machine's *silence* proves nothing. A machine reporting that it is dead is
  not silent, and waiting thirty minutes on it is thirty minutes of a member
  that is never coming back.

- **A stopped machine is never probed.** The decide phase reads an unanswered
  probe as "unreachable, replace", so probing a machine that is switched off
  would delete and rebuild every member autoscaling had just parked, at a
  template build each. Power state is read off the machine row, never
  inferred from a guest that cannot speak.

- **A status this version does not understand is skipped, not replaced.**
  Every downstream branch reads silence as delete-and-rebuild, and an
  uninterpretable status is no evidence at all that a machine is dead.

- **A member busy at every scan defers its own replacement.** Past 30 days
  the run says so and asks for a hand drain, rather than killing a job to
  make a point. A real drain - disable the registrations, wait for idle, then
  replace - is the proper fix and is not built yet.

- **Starts finish before any stop begins.** A tick that dies halfway then
  leaves the pool larger than intended, never smaller: too much capacity is a
  bill, too little is a stuck queue. As the planner stands the two are
  mutually exclusive in one tick, so this is a guard against future
  restructuring rather than a live path.

## Failure isolation

One member's failure is that member's failure: it is logged as an Actions
error, its budget stays spent, and the run continues. The pool converges
across runs.

This is not free, and it was not always true. An exception escaping a
`gather` cancelled every sibling mid-create; a probe that could not even
connect cancelled every sibling probe. `SystemExit` is a `BaseException`, so
`return_exceptions=True` does not hold it - the stop path catches it
explicitly for that reason.

The per-run replacement cap is spent at *admission*, not on success, so a bad
template rev stalls loudly after N boots instead of thrashing the pool
forever. The scan order rotates by run number, because with a fixed order one
permanently-broken low-numbered member owns the whole budget run after run
and nothing above it ever converges.

## Where this is going

This is v1, and deliberately the simple thing: long-lived machines whose
power state follows a level. The end state is ephemeral runners - a machine
per job, booted from a warm snapshot on a webhook and destroyed when the job
ends - which removes the idle question entirely rather than managing it. That
needs an ix-hosted control plane; until it exists, a fixed pool with a power
dial is the version that is honest about what it can guarantee.

That also removes the customer-side file entirely. `ix-pool.toml` and the
reconcile workflow exist because the pool is managed from the consumer's own
CI; once the control plane runs it, adopting ix CI is choosing a `runs-on`
label and nothing else, the way Blacksmith works today. The spec file is v1
surface polish, and v1 surface is the part v2 deletes.


## Why a vended token rather than a PAT, and rather than an App key

The reconcile needs a credential that can mint runner registration tokens,
and GitHub puts that behind the `administration` permission, which workflow
`GITHUB_TOKEN` does not have and cannot be granted. So a second credential is
structural, not a shortcut. The question is only what shape it takes, and two
obvious answers are both wrong.

**A fine-grained PAT** is bound to a person: it carries whatever else that
person granted it, it survives them leaving the org until somebody remembers
it, and rotating it is a human task on a calendar. It works, and it is the
fallback, but it is not somewhere to stay.

**A GitHub App private key in the customer's repo** is worse, and it is the
answer that looks right. An App's key is APP-GLOBAL: it mints installation
tokens for EVERY installation of that App. Put ix's key in a customer repo
and that repo holds a credential for every other pool ix runs. Have each
customer create their own App and the "five-minute setup" becomes a
manifest, a key download, a secret, and a rotation policy - per repo, forever.

So the key stays with ix and the token comes over the wire. The runner proves
which repository it is by presenting the OIDC identity GitHub signs for it;
ix verifies that claim, checks the App is installed on that repository, and
vends an installation token scoped to it alone. The caller cannot ask for a
token for a repository it is not running in, because it cannot forge the
claim. Nothing is stored on either side.

    POST https://ix.dev/api/ci/github-token
      Authorization: Bearer <IX_TOKEN>
      {"oidc_token": "<GitHub Actions OIDC JWT, audience=ix.dev>"}
      -> {"token": "ghs_...", "expires_at": ..., "installation_id": ..., "repository": ...}

The audience matters: a JWT minted for a different audience is replayable
there, so the action pins `audience=ix.dev` and a test asserts it does.

### The endpoints, checked before any of this was built

One unsupported endpoint would have ended the idea, and the registration-token
mint in particular has a persistent reputation for being PAT-only. It is not:

| endpoint | permission |
| --- | --- |
| `POST /actions/runners/registration-token` | Administration: write |
| `GET /actions/runners` | Administration: read |
| `DELETE /actions/runners/{id}` | Administration: write |
| `GET /actions/runs` | Actions: read |
| `GET /actions/runs/{id}/jobs` | Actions: read |

All five list installation access tokens as supported. The folklore appears
to come from the ENTERPRISE-level runner endpoints, which need
`manage_runners:enterprise` and were not App-callable in older docs, and from
organization-level runner management, which needs a different permission
again. Neither applies to a repository-scoped pool.

Nothing in the reconcile changed for any of this. An installation token is a
bearer token in the same header the PAT used, and the code that reads it
takes either - not as a choice, but because the vending endpoint is not
deployed yet and the PAT is what pools run on until it is.

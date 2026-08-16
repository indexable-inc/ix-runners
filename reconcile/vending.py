"""Acquire the GitHub credential that administers this pool's runners.

Two sources, chosen by ``IX_TOKEN_SOURCE`` (the action's ``token-source``
input):

- ``ix``: trade this workflow run's OIDC identity for a GitHub App
  installation token scoped to THIS repository, through the ix SDK
  (``client.ci().github_runner_token``). Nothing long-lived exists
  anywhere: GitHub mints the OIDC token for this run, ix verifies it and
  answers with a token GitHub expires in about an hour, scoped to the one
  repository the OIDC claims prove.
- ``pat``: the legacy admin PAT from ``GITHUB_ADMIN_TOKEN`` /
  ``RUNNER_PAT``. The fallback era, not the destination.

The OIDC fetch is the one raw HTTP call in this package: GitHub's own
runtime issues the JWT (``ACTIONS_ID_TOKEN_REQUEST_URL``), and no ix
surface can do that for us. Everything ix-facing goes through the SDK.
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request

from .github import OPENER

# The audience the ix verifier matches exactly. A workflow asking for any
# other audience gets a refusal that names this string.
AUDIENCE = "ix.dev"

# GitHub's OIDC issuer answers in well under a second; a hung request here
# should fail the run promptly, not hold it to the job timeout.
OIDC_TIMEOUT = 30

# The only host github.com's runtime issues an OIDC token from. The request
# URL is read from the environment, so it is subject to the same $GITHUB_ENV
# rewrite the rest of this package refuses to trust (github.api_base pins the
# API host for exactly this reason; the README pins actions/checkout by SHA
# because checkout runs first and can rewrite the environment we then read).
# The bearer this request carries mints OIDC JWTs at any audience, which ix
# trades for a GitHub App token with Administration read/write - so the host
# is pinned, not taken on faith.
OIDC_HOST_SUFFIX = ".actions.githubusercontent.com"


def token_source() -> str:
    """The caller's choice, defaulting to the PAT era."""
    return os.environ.get("IX_TOKEN_SOURCE") or "pat"


def preflight() -> None:
    """Fail before anything is created, naming the fix.

    Environment presence only - no network. The action used to do this in
    an inline bash step; it lives here now so driving the script directly
    gets the same diagnosis.
    """
    source = token_source()
    if source == "ix":
        if os.environ.get("GITHUB_ADMIN_TOKEN") or os.environ.get("RUNNER_PAT"):
            raise SystemExit(
                "token-source is 'ix' but an admin PAT is also set. Remove"
                " runner-pat (and delete the RUNNER_PAT secret): keeping both"
                " means the credential this mode exists to retire is still"
                " lying in the repository."
            )
        if not (
            os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL")
            and os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN")
        ):
            raise SystemExit(
                "token-source is 'ix' but this job cannot request an OIDC"
                " token. Add 'permissions: id-token: write' to the job that"
                " runs this action."
            )
        return
    if source == "pat":
        if not (os.environ.get("GITHUB_ADMIN_TOKEN") or os.environ.get("RUNNER_PAT")):
            raise SystemExit(
                "token-source is 'pat' but no admin credential is set: set"
                " runner-pat, or switch to token-source: ix (and install"
                " https://github.com/apps/ix-runners on this repository)."
            )
        return
    raise SystemExit(f"token-source must be 'ix' or 'pat', got {source!r}")


async def admin_credential(ix: object) -> str:
    """The bearer token the GitHub half of the reconcile runs on.

    Downstream does not care which era it came from: a vended installation
    token rides the same header as a PAT, and every endpoint this package
    calls accepts both.
    """
    if token_source() == "ix":
        return await _vended_token(ix)
    return _legacy_pat()


def _legacy_pat() -> str:
    for name in ("GITHUB_ADMIN_TOKEN", "RUNNER_PAT"):
        value = os.environ.get(name)
        if value:
            return value
    # preflight() already refused this; reachable only when driving the
    # module directly, so keep the message actionable anyway.
    raise SystemExit(
        "no admin credential: set GITHUB_ADMIN_TOKEN (the action does this"
        " from runner-pat) or RUNNER_PAT, or switch to token-source: ix"
    )


async def _vended_token(ix: object) -> str:
    jwt = _oidc_jwt()
    try:
        vended = await ix.ci().github_runner_token(jwt)
    except RuntimeError as error:
        # IxError subclasses RuntimeError. The server's refusal messages
        # already name the fix (audience mismatch, App not installed, rate
        # limit); repeating them verbatim is the diagnosis.
        raise SystemExit(f"ix refused to vend a GitHub token: {error}") from error
    # Mask before anything can print it: this redacts the credential from
    # any traceback the runner emits for the rest of the run.
    print(f"::add-mask::{vended.token}", flush=True)
    print(
        f"vended a GitHub token for {vended.repository},"
        f" expiring {vended.expires_at}",
        flush=True,
    )
    return vended.token


def _oidc_jwt() -> str:
    """This run's OIDC JWT, from GitHub's own issuance endpoint.

    Caller-blind proof of which repository is running: the JWT is signed by
    GitHub and carries the repository in its claims, which is what the ix
    verifier reads. This module never parses it.
    """
    url = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL")
    bearer = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN")
    if not (url and bearer):
        raise SystemExit(
            "this job cannot request an OIDC token: add"
            " 'permissions: id-token: write' to the job that runs this action"
        )
    _refuse_untrusted_oidc_url(url)
    separator = "&" if "?" in url else "?"
    request = urllib.request.Request(
        f"{url}{separator}audience={AUDIENCE}",
        headers={"Authorization": f"Bearer {bearer}"},
    )
    try:
        # OPENER refuses redirects: urllib re-sends the Authorization header
        # across a 30x, which would hand the ID-token bearer to whatever host
        # the pinned endpoint redirected to. None of this legitimately
        # redirects, so refusing it costs nothing and closes the last hop.
        with OPENER.open(request, timeout=OIDC_TIMEOUT) as response:
            payload = json.load(response)
    except (urllib.error.URLError, TimeoutError, ValueError) as error:
        raise SystemExit(
            f"GitHub's OIDC token endpoint did not answer: {error}"
        ) from error
    jwt = payload.get("value")
    if not isinstance(jwt, str) or not jwt:
        raise SystemExit("GitHub's OIDC token endpoint answered without a token")
    print(f"::add-mask::{jwt}", flush=True)
    return jwt


def _refuse_untrusted_oidc_url(url: str) -> None:
    """Send the ID-token bearer only to https on GitHub's OIDC host.

    ``ACTIONS_ID_TOKEN_REQUEST_URL`` is an environment variable, so an earlier
    step can rewrite it through ``$GITHUB_ENV``. Honoured unconditionally it
    aims the bearer at a host of that step's choosing (a passive collector can
    then mint ``ix.dev``-audience JWTs and trade them for an Administration
    read/write App token), or over plaintext http. On github.com the value is
    always ``token.actions.githubusercontent.com`` over https, so pinning it
    costs nothing. GHES is the deployment that already had to set
    ``IX_RUNNERS_ALLOW_NON_HOSTED`` (its issuer host differs); there the https
    requirement still holds but the host is honoured.
    """
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or ""
    if parsed.scheme != "https":
        raise SystemExit(
            f"ACTIONS_ID_TOKEN_REQUEST_URL is {url!r}, which is not https."
            " Refusing to send the OIDC request bearer to it."
        )
    hosted = host == "token.actions.githubusercontent.com" or host.endswith(
        OIDC_HOST_SUFFIX
    )
    if not hosted and os.environ.get("IX_RUNNERS_ALLOW_NON_HOSTED") != "1":
        raise SystemExit(
            f"ACTIONS_ID_TOKEN_REQUEST_URL points at {host!r}, not GitHub's"
            " Actions OIDC host. Refusing to send the OIDC request bearer to"
            " it (set IX_RUNNERS_ALLOW_NON_HOSTED=1 on GHES/ARC to accept a"
            " different issuer host explicitly)."
        )

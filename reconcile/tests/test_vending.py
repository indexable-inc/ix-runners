"""The vending module: source dispatch, preflight refusals, OIDC fetch.

Every network edge is faked; the SDK never loads (the fakes stand in for
it, as they do across the suite - the wheel is x86_64-only).
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import unittest
from unittest import mock

from reconcile import vending

IX_MODE = {"IX_TOKEN_SOURCE": "ix"}
OIDC_ENV = {
    "ACTIONS_ID_TOKEN_REQUEST_URL": "https://actions.example/token?x=1",
    "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "runner-bearer",
}


def scrubbed(extra: dict[str, str]) -> mock._patch_dict:
    """An environment holding exactly `extra` of the vending variables."""
    cleared = {
        name: ""
        for name in (
            "IX_TOKEN_SOURCE",
            "GITHUB_ADMIN_TOKEN",
            "RUNNER_PAT",
            "ACTIONS_ID_TOKEN_REQUEST_URL",
            "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
        )
    }
    return mock.patch.dict(os.environ, cleared | extra)


class FakeVended:
    token = "ghs_fake_installation_token"
    expires_at = "2026-08-16T02:00:00Z"
    repository = "owner/repo"


class FakeCi:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.jwts: list[str] = []

    async def github_runner_token(self, oidc_token: str) -> FakeVended:
        self.jwts.append(oidc_token)
        if self.error is not None:
            raise self.error
        return FakeVended()


class FakeClient:
    def __init__(self, ci: FakeCi) -> None:
        self._ci = ci

    def ci(self) -> FakeCi:
        return self._ci


def fake_oidc_endpoint(body: bytes = b'{"value": "jwt-abc"}'):
    """Patch urlopen with a stub that records the request it served."""
    served: list[object] = []

    @contextlib.contextmanager
    def urlopen(request, timeout=None):
        served.append(request)
        yield io.BytesIO(body)

    return mock.patch.object(vending.urllib.request, "urlopen", urlopen), served


class PreflightRefusals(unittest.TestCase):
    def test_ix_with_a_pat_still_wired_is_refused(self) -> None:
        with scrubbed(IX_MODE | OIDC_ENV | {"RUNNER_PAT": "ghp_x"}):
            with self.assertRaisesRegex(SystemExit, "admin PAT is also set"):
                vending.preflight()

    def test_ix_without_oidc_permission_names_the_fix(self) -> None:
        with scrubbed(IX_MODE):
            with self.assertRaisesRegex(SystemExit, "id-token: write"):
                vending.preflight()

    def test_pat_without_a_pat_is_refused(self) -> None:
        with scrubbed({"IX_TOKEN_SOURCE": "pat"}):
            with self.assertRaisesRegex(SystemExit, "no admin credential"):
                vending.preflight()

    def test_unknown_source_is_refused(self) -> None:
        with scrubbed({"IX_TOKEN_SOURCE": "app"}):
            with self.assertRaisesRegex(SystemExit, "'ix' or 'pat'"):
                vending.preflight()

    def test_default_is_the_pat_era(self) -> None:
        with scrubbed({"GITHUB_ADMIN_TOKEN": "ghp_y"}):
            vending.preflight()  # does not raise

    def test_ix_with_oidc_available_passes(self) -> None:
        with scrubbed(IX_MODE | OIDC_ENV):
            vending.preflight()  # does not raise


class AdminCredential(unittest.TestCase):
    def test_pat_mode_returns_the_env_credential(self) -> None:
        with scrubbed({"IX_TOKEN_SOURCE": "pat", "GITHUB_ADMIN_TOKEN": "ghp_z"}):
            token = asyncio.run(vending.admin_credential(FakeClient(FakeCi())))
        self.assertEqual(token, "ghp_z")

    def test_ix_mode_vends_through_the_sdk_and_masks(self) -> None:
        ci = FakeCi()
        patch, served = fake_oidc_endpoint()
        out = io.StringIO()
        with scrubbed(IX_MODE | OIDC_ENV), patch, contextlib.redirect_stdout(out):
            token = asyncio.run(vending.admin_credential(FakeClient(ci)))
        self.assertEqual(token, FakeVended.token)
        # The SDK got the JWT the OIDC endpoint issued, untouched.
        self.assertEqual(ci.jwts, ["jwt-abc"])
        # The OIDC request asked for the ix audience with the runner bearer.
        request = served[0]
        self.assertIn("audience=ix.dev", request.full_url)
        self.assertEqual(
            request.get_header("Authorization"), "Bearer runner-bearer"
        )
        # Both credentials were masked BEFORE the summary line printed them
        # anywhere near a log.
        lines = out.getvalue().splitlines()
        self.assertIn("::add-mask::jwt-abc", lines)
        self.assertIn(f"::add-mask::{FakeVended.token}", lines)
        self.assertLess(
            lines.index(f"::add-mask::{FakeVended.token}"),
            len(lines) - 1,
            "the mask must land before the human-readable summary",
        )

    def test_sdk_refusal_becomes_an_actionable_exit(self) -> None:
        refusal = RuntimeError(
            "the ix-runners GitHub App is not installed on owner/repo -"
            " install it at https://github.com/apps/ix-runners"
        )
        patch, _ = fake_oidc_endpoint()
        with scrubbed(IX_MODE | OIDC_ENV), patch, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(SystemExit, "apps/ix-runners"):
                asyncio.run(vending.admin_credential(FakeClient(FakeCi(error=refusal))))


class OidcFetch(unittest.TestCase):
    def test_appends_audience_with_the_right_separator(self) -> None:
        patch, served = fake_oidc_endpoint()
        with scrubbed(OIDC_ENV), patch, contextlib.redirect_stdout(io.StringIO()):
            vending._oidc_jwt()
        # The issuance URL already carries a query string, so the audience
        # must join with '&' or the runner bearer serves the wrong request.
        self.assertTrue(served[0].full_url.endswith("&audience=ix.dev"))

    def test_a_tokenless_answer_is_a_refusal_not_a_crash(self) -> None:
        patch, _ = fake_oidc_endpoint(body=json.dumps({"value": ""}).encode())
        with scrubbed(OIDC_ENV), patch:
            with self.assertRaisesRegex(SystemExit, "without a token"):
                vending._oidc_jwt()


if __name__ == "__main__":
    unittest.main()

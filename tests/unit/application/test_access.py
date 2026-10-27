"""The lesson's step 3: the merchant is authenticated with a secret and a token bound to its own key."""

from datetime import timedelta

import pytest

from open_banking.domain.errors import ClientAuthenticationError
from open_banking.infrastructure import keys
from tests.unit.conftest import BANK_URL
from tests.unit.helpers import dpop

TOKEN_URL = f"{BANK_URL}/oauth/token"


def issue(world, merchant=None, secret=None, proof=None):
    merchant = merchant or world.merchant
    proof = proof or dpop(merchant, world, "POST", "/oauth/token")
    return world.bank.issue_token.execute(merchant.client_id, secret or merchant.secret, proof, TOKEN_URL)


def call(world, token, merchant=None, method="POST", path="/v1/payments", proof=None):
    merchant = merchant or world.merchant
    proof = proof or dpop(merchant, world, method, path, token)
    return world.bank.authenticate.execute(f"DPoP {token}", proof, method=method, url=f"{BANK_URL}{path}")


def test_a_registered_merchant_gets_a_token_and_is_recognized_with_a_fresh_proof(world):
    issued = issue(world)
    assert issued.expires_in_seconds == 600
    assert call(world, issued.access_token) == "merchant-1"
    assert call(world, issued.access_token) == "merchant-1"  # a new proof each time, so the same token works again


def test_only_a_digest_of_the_token_is_stored(world):
    issued = issue(world)
    assert issued.access_token not in world.db.tokens and len(world.db.tokens) == 1


def test_a_wrong_secret_and_an_unknown_merchant_are_refused_alike(world):
    with pytest.raises(ClientAuthenticationError, match="invalid merchant credential"):
        issue(world, secret="not-the-secret")
    ghost = world.merchant.__class__("ghost", "x" * 20, world.merchant.private_pem, world.merchant.public_jwk)
    with pytest.raises(ClientAuthenticationError, match="invalid merchant credential"):
        world.bank.issue_token.execute("ghost", "x" * 20, dpop(ghost, world, "POST", "/oauth/token"), TOKEN_URL)


def test_a_proof_signed_by_another_key_cannot_get_a_token(world):
    with pytest.raises(ClientAuthenticationError):
        issue(world, proof=dpop(world.other_merchant, world, "POST", "/oauth/token"))


def test_a_proof_cannot_be_used_twice(world):
    proof = dpop(world.merchant, world, "POST", "/oauth/token")
    issue(world, proof=proof)
    with pytest.raises(ClientAuthenticationError, match="already used"):
        issue(world, proof=proof)
    token = issue(world).access_token
    request_proof = dpop(world.merchant, world, "POST", "/v1/payments", token)
    call(world, token, proof=request_proof)
    with pytest.raises(ClientAuthenticationError, match="already used"):
        call(world, token, proof=request_proof)


def test_a_stolen_token_is_useless_without_the_merchants_key(world):
    token = issue(world).access_token
    with pytest.raises(ClientAuthenticationError, match="different key"):
        call(world, token, proof=dpop(world.other_merchant, world, "POST", "/v1/payments", token))


def test_a_proof_for_another_request_or_token_is_refused(world):
    token = issue(world).access_token
    other_token = issue(world).access_token
    with pytest.raises(ClientAuthenticationError, match="different request"):
        call(world, token, proof=dpop(world.merchant, world, "GET", "/v1/payments", token))
    with pytest.raises(ClientAuthenticationError, match="different request"):
        call(world, token, proof=dpop(world.merchant, world, "POST", "/v1/elsewhere", token))
    with pytest.raises(ClientAuthenticationError, match="access token"):
        call(world, token, proof=dpop(world.merchant, world, "POST", "/v1/payments", other_token))
    with pytest.raises(ClientAuthenticationError, match="access token"):
        call(world, token, proof=dpop(world.merchant, world, "POST", "/v1/payments"))  # no token hash in the proof
    with pytest.raises(ClientAuthenticationError, match="does not carry"):
        issue(
            world, proof=dpop(world.merchant, world, "POST", "/oauth/token", token)
        )  # a token hash where none belongs


def test_a_stale_proof_and_an_expired_token_are_refused(world):
    token = issue(world).access_token
    old = dpop(world.merchant, world, "POST", "/v1/payments", token)
    world.clock.advance(timedelta(seconds=61))
    with pytest.raises(ClientAuthenticationError, match="too old"):
        call(world, token, proof=old)
    world.clock.advance(timedelta(minutes=10))
    with pytest.raises(ClientAuthenticationError, match="invalid merchant credential"):
        call(world, token)


@pytest.mark.parametrize("authorization", [None, "", "Bearer abc", "DPoP", "DPoP unknown-token"])
def test_a_missing_or_malformed_credential_is_refused(world, authorization):
    proof = dpop(world.merchant, world, "POST", "/v1/payments")
    with pytest.raises(ClientAuthenticationError):
        world.bank.authenticate.execute(authorization, proof, method="POST", url=f"{BANK_URL}/v1/payments")


def test_a_request_without_a_proof_is_refused(world):
    token = issue(world).access_token
    with pytest.raises(ClientAuthenticationError):
        world.bank.authenticate.execute(f"DPoP {token}", None, method="POST", url=f"{BANK_URL}/v1/payments")


def test_a_token_whose_key_was_replaced_is_refused(world):
    token = issue(world).access_token
    _, new_jwk = keys.generate_keypair()
    world.db.clients["merchant-1"] = world.db.clients["merchant-1"].__class__(
        "merchant-1", "merchant-1", world.db.clients["merchant-1"].secret_hash, new_jwk
    )
    with pytest.raises(ClientAuthenticationError):
        call(world, token)

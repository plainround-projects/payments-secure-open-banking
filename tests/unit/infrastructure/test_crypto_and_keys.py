import json
import os
import stat
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from open_banking.domain.errors import ClientAuthenticationError
from open_banking.infrastructure import keys
from open_banking.infrastructure.crypto import StandardCrypto

NOW = datetime(2026, 10, 27, 9, 0, tzinfo=UTC)
URL = "http://bank.test/v1/payments"
PEM, JWK = keys.generate_keypair()
OTHER_PEM, OTHER_JWK = keys.generate_keypair()


@pytest.fixture
def crypto():
    return StandardCrypto(pepper="test-pepper-0123456789", scrypt_n=16)


def proof(private=PEM, public=JWK, method="POST", url=URL, now=NOW, token=None):
    return keys.build_dpop_proof(private, public, method=method, url=url, now=now, access_token=token)


def verify(crypto, text, method="POST", url=URL, now=NOW, token=None, public=JWK):
    return crypto.verify_dpop_proof(text, public, method=method, url=url, now=now, access_token=token)


def test_a_secret_is_stored_as_a_salted_slow_hash_and_checked_in_constant_time(crypto):
    first, second = crypto.hash_secret("246810"), crypto.hash_secret("246810")
    assert first != second and first.startswith("scrypt$16$8$1$") and "246810" not in first
    assert crypto.verify_secret("246810", first) and not crypto.verify_secret("246811", first)


@pytest.mark.parametrize("stored", ["", "plain", "scrypt$x$8$1$aa$bb", "bcrypt$16$8$1$aa$bb", "scrypt$16$8$1$aa"])
def test_a_malformed_stored_secret_is_simply_not_a_match(crypto, stored):
    assert crypto.verify_secret("anything", stored) is False


def test_the_cost_comes_from_the_stored_value_so_it_can_change_over_time():
    cheap, dearer = StandardCrypto(pepper="p" * 16, scrypt_n=16), StandardCrypto(pepper="p" * 16, scrypt_n=32)
    assert dearer.verify_secret("s", cheap.hash_secret("s"))


def test_digests_are_keyed_by_the_pepper_and_tied_to_what_they_protect(crypto):
    other = StandardCrypto(pepper="another-pepper-0123456789", scrypt_n=16)
    digest = crypto.code_digest("challenge-1", "123456")
    assert digest == crypto.code_digest("challenge-1", "123456")
    assert digest != crypto.code_digest("challenge-2", "123456") != other.code_digest("challenge-2", "123456")
    assert digest != other.code_digest("challenge-1", "123456")
    assert crypto.token_digest("t") != other.token_digest("t") and crypto.token_digest("t") != crypto.code_digest(
        "t", ""
    )


def test_codes_are_six_digits_and_tokens_are_unguessable(crypto):
    codes = {crypto.new_code() for _ in range(50)}
    assert all(len(code) == 6 and code.isdigit() for code in codes) and len(codes) > 25
    assert len({crypto.new_token() for _ in range(50)}) == 50 and len(crypto.new_token()) >= 40


def test_a_valid_proof_verifies_and_reports_its_id_and_time(crypto):
    claims = verify(crypto, proof())
    assert claims.issued_at == NOW and len(claims.jti) == 32
    assert verify(crypto, proof()).jti != claims.jti


def test_a_proof_for_a_token_carries_the_hash_of_that_token(crypto):
    assert verify(crypto, proof(token="abc"), token="abc")
    with pytest.raises(ClientAuthenticationError, match="access token"):
        verify(crypto, proof(token="abc"), token="abd")


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"method": "GET"}, "different request"),
        ({"url": "http://bank.test/v1/other"}, "different request"),
        ({"url": "http://evil.test/v1/payments"}, "different request"),
        ({"now": NOW - timedelta(seconds=61)}, "too old"),
        ({"now": NOW + timedelta(seconds=61)}, "too old"),
    ],
)
def test_a_proof_for_another_request_or_time_is_refused(crypto, change, message):
    made = proof(**{k: v for k, v in change.items() if k in {"method", "url", "now"}})
    checked = {"method": "POST", "url": URL, "now": NOW}
    if "now" in change:
        made = proof()
        checked["now"] = change["now"]
    with pytest.raises(ClientAuthenticationError, match=message):
        verify(crypto, made, **checked)


def test_the_query_string_is_not_part_of_what_a_proof_is_bound_to(crypto):
    assert verify(crypto, proof(url=URL + "?a=1"), url=URL + "?b=2")


def test_a_proof_signed_by_another_key_is_refused(crypto):
    with pytest.raises(ClientAuthenticationError, match="different key"):
        verify(crypto, proof(OTHER_PEM, OTHER_JWK))
    forged = proof(OTHER_PEM, JWK)  # claims our key in its header but is signed with another
    with pytest.raises(ClientAuthenticationError):
        verify(crypto, forged)


def test_a_tampered_or_malformed_proof_is_refused(crypto):
    header, payload, signature = proof().split(".")
    claims = json.loads(keys.b64url_decode(payload))
    claims["htm"] = "GET"
    tampered = ".".join([header, keys.b64url(json.dumps(claims).encode()), signature])
    for bad in (tampered, "not.a.jwt", "", proof()[:-4]):
        with pytest.raises(ClientAuthenticationError):
            verify(crypto, bad)


def test_the_wrong_type_algorithm_or_missing_id_is_refused(crypto):
    plain = jwt.encode({"htm": "POST", "htu": URL, "iat": int(NOW.timestamp()), "jti": "x"}, PEM, algorithm="ES256")
    no_id = jwt.encode(
        {"htm": "POST", "htu": URL, "iat": int(NOW.timestamp())}, PEM, algorithm="ES256", headers={"typ": "dpop+jwt"}
    )
    text_time = jwt.encode(
        {"htm": "POST", "htu": URL, "iat": "yesterday", "jti": "x"}, PEM, algorithm="ES256", headers={"typ": "dpop+jwt"}
    )
    for bad in (plain, no_id, text_time):
        with pytest.raises(ClientAuthenticationError):
            verify(crypto, bad)


def test_request_signatures_verify_only_for_the_signed_bytes_and_key(crypto):
    signature = keys.sign(PEM, b"amount=3000")
    assert crypto.verify_signature(JWK, b"amount=3000", signature)
    assert not crypto.verify_signature(JWK, b"amount=3500", signature)
    assert not crypto.verify_signature(OTHER_JWK, b"amount=3000", signature)
    assert not crypto.verify_signature(JWK, b"amount=3000", "not-a-signature")
    assert not crypto.verify_signature({"kty": "EC"}, b"amount=3000", signature)


def test_thumbprints_identify_a_key_independent_of_member_order(crypto):
    assert crypto.thumbprint(JWK) == crypto.thumbprint(dict(reversed(list(JWK.items()))))
    assert crypto.thumbprint(JWK) != crypto.thumbprint(OTHER_JWK) and len(crypto.thumbprint(JWK)) == 43


def test_a_private_key_file_is_readable_by_its_owner_only(tmp_path):
    path = tmp_path / "nested" / "key.pem"
    keys.write_private_key(path, PEM)
    assert keys.read_private_key(path) == PEM and keys.public_jwk_of(PEM) == JWK
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert set(JWK) == {"kty", "crv", "x", "y"} and "d" not in JWK


def test_base64url_round_trips_without_padding():
    for size in range(0, 9):
        data = bytes(range(size))
        assert "=" not in keys.b64url(data) and keys.b64url_decode(keys.b64url(data)) == data

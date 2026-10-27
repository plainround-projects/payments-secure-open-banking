import json
import stat
from pathlib import Path

import httpx
import pytest

from open_banking.infrastructure.bank_settings import BankConfig
from open_banking.infrastructure.faults import Fault, FaultInjectingTransport, FaultScript
from open_banking.infrastructure.otp import FileOtpChannel, MemoryOtpChannel
from open_banking.infrastructure.settings import ConfigurationError, MerchantConfig

BANK = {"POSTGRES_PASSWORD": "not-a-real-secret", "BANK_PEPPER": "pepper-0123456789abc"}
MERCHANT = {"MERCHANT_CLIENT_SECRET": "not-a-real-secret"}


def test_the_bank_defaults_give_a_working_local_configuration_and_hide_secrets():
    config = BankConfig.from_env(BANK)
    assert config.server.port == 8463 and config.public_url == "http://127.0.0.1:8463"
    assert config.postgres.port == 54613 and config.timings.consent_time_to_live.total_seconds() == 600
    assert config.timings.recurring_consent_time_to_live.days == 90 and config.scrypt_n == 2**14
    assert "not-a-real-secret" not in repr(config) and "pepper-0123456789abc" not in repr(config)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("BANK_PEPPER", "short"),
        ("BANK_PORT", "0"),
        ("BANK_PUBLIC_URL", "bank.test"),
        ("SCRYPT_N", "1000"),
        ("LOG_LEVEL", "LOUD"),
        ("CONSENT_MINUTES", "-1"),
    ],
)
def test_a_bad_bank_value_names_the_variable(name, value):
    with pytest.raises(ConfigurationError, match=name):
        BankConfig.from_env({**BANK, name: value})


def test_the_bank_secrets_have_no_defaults():
    for name in ("POSTGRES_PASSWORD", "BANK_PEPPER"):
        with pytest.raises(ConfigurationError, match=name):
            BankConfig.from_env({key: value for key, value in BANK.items() if key != name})


def test_the_merchant_needs_a_secret_and_has_no_database_settings():
    config = MerchantConfig.from_env(MERCHANT)
    assert config.client_id == "merchant-1" and config.attempts == 3 and config.server.port == 8464
    assert "not-a-real-secret" not in repr(config) and not hasattr(config, "postgres")
    with pytest.raises(ConfigurationError, match="MERCHANT_CLIENT_SECRET"):
        MerchantConfig.from_env({})
    with pytest.raises(ConfigurationError, match="BANK_URL"):
        MerchantConfig.from_env({**MERCHANT, "BANK_URL": "bank"})


def test_the_phone_file_keeps_each_code_and_is_private(tmp_path):
    phone = FileOtpChannel(tmp_path / "var" / "outbox.log")
    assert phone.latest_code("c1") is None and phone.latest_any() is None
    phone.send("c1", "111111", "approve")
    phone.send("c2", "222222", "approve")
    phone.send("c1", "333333", "approve")
    assert (
        phone.latest_code("c1") == "333333" and phone.latest_code("c2") == "222222" and phone.latest_any() == "333333"
    )
    assert stat.S_IMODE(Path(tmp_path / "var" / "outbox.log").stat().st_mode) == 0o600
    assert json.loads((tmp_path / "var" / "outbox.log").read_text().splitlines()[0])["customer_id"] == "c1"


def test_the_memory_phone_remembers_messages():
    phone = MemoryOtpChannel()
    assert phone.latest_code("c1") is None
    phone.send("c1", "123456", "approve")
    assert phone.latest_code("c1") == "123456" and phone.messages == [("c1", "123456", "approve")]


def ok(request):
    return httpx.Response(200, json={"ok": True})


def test_a_dropped_response_is_processed_by_the_server_and_lost_to_the_caller():
    seen = []

    def server(request):
        seen.append(request.url.path)
        return httpx.Response(201, json={"done": True})

    script = FaultScript()
    client = httpx.Client(transport=FaultInjectingTransport(httpx.MockTransport(server), script))
    script.push(Fault.DROP_RESPONSE)
    with pytest.raises(httpx.ReadTimeout):
        client.post("http://bank.test/v1/payments")
    assert seen == ["/v1/payments"]  # the bank got it
    assert client.post("http://bank.test/v1/payments").status_code == 201


def test_a_dropped_request_never_reaches_the_server():
    seen = []
    script = FaultScript()

    def server(request):
        seen.append(request)
        return httpx.Response(200)

    client = httpx.Client(transport=FaultInjectingTransport(httpx.MockTransport(server), script))
    script.push(Fault.DROP_REQUEST, 2)
    for _ in range(2):
        with pytest.raises(httpx.ConnectTimeout):
            client.get("http://bank.test/")
    assert seen == [] and client.get("http://bank.test/").status_code == 200 and script.next() is None
    client.close()

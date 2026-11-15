"""Gateway bootstrap token registry tests."""

from sjtuclaw.entrypoints.gateway_auth import BootstrapTokenRegistry


def test_token_is_one_time_and_expires() -> None:
    now = [10.0]
    registry = BootstrapTokenRegistry(now=lambda: now[0])
    token = registry.issue("pet", ttl_seconds=30, token="p" * 43)
    assert registry.consume(token) == "pet"
    assert registry.consume(token) is None

    expired = registry.issue("browser", ttl_seconds=30, token="b" * 43)
    now[0] = 41.0
    assert registry.consume(expired) is None


def test_token_expires_at_its_ttl_deadline() -> None:
    now = [10.0]
    registry = BootstrapTokenRegistry(now=lambda: now[0])
    token = registry.issue("pet", ttl_seconds=30, token="p" * 43)

    now[0] = 40.0

    assert registry.consume(token) is None


def test_purposes_are_not_interchangeable() -> None:
    registry = BootstrapTokenRegistry()
    assert registry.consume(registry.issue("pet")) == "pet"
    assert registry.consume(registry.issue("browser")) == "browser"

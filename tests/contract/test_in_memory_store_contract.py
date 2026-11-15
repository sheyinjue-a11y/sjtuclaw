"""Run the backend-neutral SessionStore contract against the in-memory adapter."""

import pytest

from sjtuclaw.infrastructure.persistence import InMemorySessionStore
from sjtuclaw.ports.stores import SessionStore
from tests.contract.session_store_contract import SessionStoreContract


class TestInMemorySessionStoreContract(SessionStoreContract):
    @pytest.fixture
    def store(self) -> SessionStore:
        return InMemorySessionStore()

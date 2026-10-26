from datetime import datetime, timedelta, timezone

import pytest

from common.billing import customer_wallet_document_id
from services.agent_persistence_worker_v3.app.core.config import get_settings
from services.agent_persistence_worker_v3.app.services import billing_reconciliation
from services.agent_persistence_worker_v3.app.services.billing_reconciliation import (
    BillingReconciliationService,
)
from services.agent_persistence_worker_v3.app.services.billing_settlement import (
    BillingSettlementService,
)
from tests.unit.test_wallet_reservations import FakeClient


@pytest.fixture(autouse=True)
def _reset_worker_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _run_transaction(client, operation):
    return operation(client.transaction())


class _Snapshot:
    def __init__(self, data):
        self._data = dict(data)

    def to_dict(self):
        return dict(self._data)


def test_expired_reservation_without_ledger_releases_credit_for_manual_review(monkeypatch):
    settings = get_settings()
    client = FakeClient()
    user_id = "user-1"
    wallet_id = customer_wallet_document_id(user_id)
    client.documents[(settings.wallets_collection, wallet_id)] = {
        "billing_subject_id": user_id,
        "owner_uid": user_id,
        "currency": "USD",
        "status": "active",
        "available_credit_nanos": 500_000_000,
        "reserved_credit_nanos": 500_000_000,
        "settled_usage_nanos": 0,
        "pending_usage_reconciliation_count": 0,
    }
    client.documents[(settings.billing_reservations_collection, "turn-1")] = {
        "reservation_id": "turn-1",
        "turn_id": "turn-1",
        "request_id": "req-1",
        "billing_subject_id": user_id,
        "owner_uid": user_id,
        "agent_id": "maxima",
        "currency": "USD",
        "reserved_amount_nanos": 500_000_000,
        "status": "reserved",
    }
    monkeypatch.setattr(
        billing_reconciliation,
        "_run_firestore_transaction",
        _run_transaction,
    )
    service = BillingReconciliationService(
        firestore_client_factory=lambda: client,
    )

    reviewed = service._hold_expired_reservation_for_review_sync(
        client,
        client.documents[(settings.billing_reservations_collection, "turn-1")],
        datetime(2026, 8, 11, 11, 0, tzinfo=timezone.utc),
    )

    assert reviewed is True
    wallet = client.documents[(settings.wallets_collection, wallet_id)]
    assert wallet["available_credit_nanos"] == 1_000_000_000
    assert wallet["reserved_credit_nanos"] == 0
    assert wallet["pending_usage_reconciliation_count"] == 1
    reservation = client.documents[(settings.billing_reservations_collection, "turn-1")]
    assert reservation["status"] == "usage_recovery_manual_review"
    assert reservation["usage_recovery_hold_released"] is True
    assert reservation["usage_recovery_hold_registered"] is True
    assert reservation["usage_reconciliation_manual_review_reason"] == (
        "completed_turn_ledger_missing_after_reservation_expiry"
    )
    assert (settings.wallet_transactions_collection, "reservation_expired_turn-1") not in client.documents

    # A repeated reconciliation must not release the same reservation twice.
    reviewed_again = service._hold_expired_reservation_for_review_sync(
        client,
        reservation,
        datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc),
    )
    assert reviewed_again is False
    assert wallet["available_credit_nanos"] == 1_000_000_000
    assert wallet["reserved_credit_nanos"] == 0
    assert wallet["pending_usage_reconciliation_count"] == 1


def test_legacy_pending_missing_ledger_hold_is_released_without_double_counting(
    monkeypatch,
):
    settings = get_settings()
    client = FakeClient()
    user_id = "user-1"
    wallet_id = customer_wallet_document_id(user_id)
    client.documents[(settings.wallets_collection, wallet_id)] = {
        "billing_subject_id": user_id,
        "owner_uid": user_id,
        "currency": "USD",
        "status": "active",
        "available_credit_nanos": 500_000_000,
        "reserved_credit_nanos": 500_000_000,
        "pending_usage_reconciliation_count": 1,
    }
    reservation = {
        "reservation_id": "turn-1",
        "turn_id": "turn-1",
        "billing_subject_id": user_id,
        "owner_uid": user_id,
        "reserved_amount_nanos": 500_000_000,
        "status": "usage_reconciliation_pending",
        "usage_recovery_hold_registered": True,
    }
    client.documents[(settings.billing_reservations_collection, "turn-1")] = reservation
    monkeypatch.setattr(
        billing_reconciliation,
        "_run_firestore_transaction",
        _run_transaction,
    )
    service = BillingReconciliationService(firestore_client_factory=lambda: client)

    assert service._hold_expired_reservation_for_review_sync(
        client, reservation, datetime.now(timezone.utc)
    )
    wallet = client.documents[(settings.wallets_collection, wallet_id)]
    assert wallet["available_credit_nanos"] == 1_000_000_000
    assert wallet["reserved_credit_nanos"] == 0
    assert wallet["pending_usage_reconciliation_count"] == 1
    assert client.documents[(settings.billing_reservations_collection, "turn-1")][
        "usage_recovery_hold_released"
    ] is True


def test_reconciliation_migrates_older_pending_hold_before_next_lookup(monkeypatch):
    monkeypatch.setenv("MONTHLY_SERVICE_FEE_NANOS", "5000000000")
    get_settings.cache_clear()
    settings = get_settings()
    client = FakeClient()
    now = datetime.now(timezone.utc)
    user_id = "user-1"
    wallet_id = customer_wallet_document_id(user_id)
    client.documents[(settings.wallets_collection, wallet_id)] = {
        "schema_version": 1,
        "billing_subject_id": user_id,
        "owner_uid": user_id,
        "currency": "USD",
        "status": "active",
        "available_credit_nanos": 500_000_000,
        "reserved_credit_nanos": 500_000_000,
        "settled_usage_nanos": 0,
        "pending_usage_reconciliation_count": 1,
    }
    reservation = {
        "reservation_id": "turn-1",
        "turn_id": "turn-1",
        "request_id": "req-1",
        "billing_subject_id": user_id,
        "owner_uid": user_id,
        "agent_id": "maxima",
        "currency": "USD",
        "reserved_amount_nanos": 500_000_000,
        "status": "usage_reconciliation_pending",
        "usage_recovery_hold_registered": True,
        "usage_reconciliation_next_attempt_at": now + timedelta(minutes=30),
        "usage_reconciliation_deadline_at": now + timedelta(days=7),
        "created_at": now - timedelta(minutes=5),
    }
    client.documents[(settings.billing_reservations_collection, "turn-1")] = reservation
    client.documents[(settings.billing_ledger_collection, "turn-1")] = {
        "turn_id": "turn-1",
        "event_id": "evt-1",
        "uid": user_id,
        "billing_subject_id": user_id,
        "billing_reservation_id": "turn-1",
        "thread_id": "thread-1",
        "session_id": "session-1",
        "agent_id": "maxima",
        "created_at": now - timedelta(minutes=4),
        "estimated_cost_usd_nanos": None,
        "provider_interaction_id": "interaction-123",
        "pricing_model": "gemini-3.8-flash",
    }
    settlement = BillingSettlementService(
        firestore_client_factory=lambda: client,
        transaction_runner=_run_transaction,
    )
    service = BillingReconciliationService(
        firestore_client_factory=lambda: client,
        settlement_service=settlement,
    )
    monkeypatch.setattr(service, "_expired_reservations", lambda *_: [])
    monkeypatch.setattr(service, "_manual_review_reservations", lambda *_: [])
    monkeypatch.setattr(
        service,
        "_pending_usage_reservations",
        lambda *_: [_Snapshot(reservation)],
    )

    service.reconcile_expired_sync()

    wallet = client.documents[(settings.wallets_collection, wallet_id)]
    assert wallet["available_credit_nanos"] == 1_000_000_000
    assert wallet["reserved_credit_nanos"] == 0
    assert wallet["pending_usage_reconciliation_count"] == 1
    migrated = client.documents[(settings.billing_reservations_collection, "turn-1")]
    assert migrated["usage_recovery_hold_released"] is True
    assert migrated["usage_reconciliation_attempt_count"] == 0


def test_expired_reservation_query_filters_status_before_applying_batch_limit(
    monkeypatch,
):
    now = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setenv("BILLING_RECONCILIATION_BATCH_SIZE", "2")
    get_settings.cache_clear()

    class Snapshot:
        def __init__(self, data):
            self.data = data

        def to_dict(self):
            return dict(self.data)

    class Collection:
        def __init__(self, documents):
            self.documents = documents

        def stream(self):
            return list(self.documents)

    class Client:
        def __init__(self, documents):
            self.collection_ref = Collection(documents)

        def collection(self, name):
            return self.collection_ref

    expired = now.replace(hour=10)
    documents = [
        Snapshot({"status": "settled", "expires_at": expired})
        for _ in range(5)
    ]
    active = Snapshot({"status": "reserved", "expires_at": expired})
    documents.append(active)
    client = Client(documents)
    service = BillingReconciliationService(firestore_client_factory=lambda: client)

    results = list(service._expired_reservations(client, now))

    assert results == [active]


def test_usage_reconciliation_backoff_starts_at_five_minutes_and_grows(monkeypatch):
    monkeypatch.setenv("BILLING_USAGE_RECONCILIATION_INITIAL_BACKOFF_SECONDS", "300")
    monkeypatch.setenv("BILLING_USAGE_RECONCILIATION_MAX_BACKOFF_SECONDS", "21600")
    get_settings.cache_clear()
    settings = get_settings()
    client = FakeClient()
    now = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
    reservation = {"turn_id": "turn-1", "usage_reconciliation_attempt_count": 0}
    client.documents[(settings.billing_reservations_collection, "turn-1")] = dict(
        reservation
    )
    service = BillingReconciliationService(firestore_client_factory=lambda: client)

    service._schedule_usage_recovery_retry(client, reservation, now)
    saved = client.documents[(settings.billing_reservations_collection, "turn-1")]
    assert saved["usage_reconciliation_attempt_count"] == 1
    assert saved["usage_reconciliation_next_attempt_at"] == now.replace(minute=5)

    service._schedule_usage_recovery_retry(client, saved, now)
    saved = client.documents[(settings.billing_reservations_collection, "turn-1")]
    assert saved["usage_reconciliation_attempt_count"] == 2
    assert saved["usage_reconciliation_next_attempt_at"] == now.replace(minute=10)


from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from omnigent.errors import OmnigentError
from omnigent.native.source_owner import NativeOwner, NativeStopOutcome
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore


@pytest.fixture
def stores(tmp_path: Path) -> tuple[SqlAlchemyConversationStore, SqlAlchemyConversationStore]:
    metadata = f"sqlite:///{tmp_path / 'metadata.db'}"
    conversations = f"sqlite:///{tmp_path / 'conversations.db'}"
    return (
        SqlAlchemyConversationStore(metadata, conversations),
        SqlAlchemyConversationStore(metadata, conversations),
    )


def test_different_agent_cannot_insert_destination_without_explicit_native_stop(stores):
    first, second = stores
    agents = SqlAlchemyAgentStore(first.storage_location)
    target = agents.create(
        agent_id="44b4151dd6cdfed6ee19430832398e05", name="target", bundle_location="target-bundle"
    )
    source = first.create_conversation(labels={"omnigent.wrapper": "prime-native-ui"})
    with pytest.raises(OmnigentError, match="explicit Stop"):
        second.fork_conversation(source.id, agent_id=target.id)
    same_agent = second.fork_conversation(source.id)
    assert len(first.list_conversations().data) == 2
    assert same_agent.id != source.id


def test_stop_revokes_shared_epoch_and_resume_invalidates_verified_stop(stores):
    first, second = stores
    source = first.create_conversation(labels={"omnigent.wrapper": "prime-native-ui"})
    owner = NativeOwner(provider="prime-native", environment="host-boot-user", runtime="private")
    with ThreadPoolExecutor(max_workers=2) as pool:
        tickets = list(pool.map(lambda store: store.admit_native(source.id, owner), stores))
    assert tickets[0].epoch == tickets[1].epoch
    stop = second.seal_native_stop(source.id)
    with pytest.raises(OmnigentError, match="revoked"):
        first.validate_native_admission(tickets[0])
    assert first.finish_native_stop(stop, NativeStopOutcome.VERIFIED).outcome == "verified"
    fork = second.fork_conversation(source.id, selected_agent_id="target")
    assert fork.id != source.id
    resumed = first.admit_native(source.id, owner)
    assert resumed.epoch != stop.admission.epoch
    with pytest.raises(OmnigentError, match="explicit Stop"):
        second.fork_conversation(source.id, selected_agent_id="target")
    same_agent = second.fork_conversation(source.id)
    assert same_agent.id != source.id


def test_unknown_pending_stop_does_not_expire_or_rebind(stores):
    first, second = stores
    source = first.create_conversation(labels={"omnigent.wrapper": "prime-native-ui"})
    owner = NativeOwner(provider="prime-native", environment="host", runtime="private")
    first.admit_native(source.id, owner)
    stop = first.seal_native_stop(source.id)
    second.finish_native_stop(stop, NativeStopOutcome.UNKNOWN, pending=True)
    restarted = SqlAlchemyConversationStore(str(first._engine.url), str(first._conv_engine.url))
    with pytest.raises(OmnigentError, match="pending"):
        restarted.admit_native(source.id, owner)
    with pytest.raises(OmnigentError, match="explicit Stop"):
        second.fork_conversation(source.id, selected_agent_id="target")


def test_unsupported_close_releases_resume_but_never_qualifies_fork(stores):
    first, second = stores
    source = first.create_conversation(labels={"omnigent.wrapper": "codex-native-ui"})
    owner = NativeOwner(provider="codex", environment="host", runtime="private")
    ticket = first.admit_native(source.id, owner)
    stop = second.seal_native_stop(source.id)
    result = first.finish_native_stop(stop, NativeStopOutcome.UNKNOWN, pending=False)
    assert result.outcome == "unknown"
    resumed = second.admit_native(source.id, owner)
    assert resumed.epoch != ticket.epoch
    with pytest.raises(OmnigentError, match="explicit Stop"):
        first.fork_conversation(source.id, selected_agent_id="target")
    assert first.get_conversation(source.id).id == source.id


def test_delayed_stop_result_cannot_qualify_successor(stores):
    first, second = stores
    source = first.create_conversation(labels={"omnigent.wrapper": "prime-native-ui"})
    owner = NativeOwner(provider="prime-native", environment="host", runtime="private")
    first.admit_native(source.id, owner)
    old_stop = first.seal_native_stop(source.id)
    second.finish_native_stop(old_stop, NativeStopOutcome.VERIFIED)
    resumed = first.admit_native(source.id, owner)
    new_stop = second.seal_native_stop(source.id)
    with pytest.raises(OmnigentError, match="superseded"):
        first.finish_native_stop(old_stop, NativeStopOutcome.VERIFIED)
    assert second.get_native_source(source.id).admission.epoch == resumed.epoch
    assert second.finish_native_stop(new_stop, NativeStopOutcome.VERIFIED).outcome == "verified"


def test_original_launch_ticket_cannot_reopen_stopped_source(stores):
    first, second = stores
    source = first.create_conversation(labels={"omnigent.wrapper": "prime-native-ui"})
    owner = NativeOwner(provider="prime-native", environment="host", runtime="private")
    ticket = first.admit_native(source.id, owner)
    stop = second.seal_native_stop(source.id)
    second.finish_native_stop(stop, NativeStopOutcome.VERIFIED)
    with pytest.raises(OmnigentError, match="revoked"):
        first.admit_native(source.id, owner, expected_epoch=ticket.epoch)
    assert first.fork_conversation(source.id, selected_agent_id="target").id != source.id


def test_replacement_preserves_unresolved_ownership_and_ordinary_resume(stores):
    first, second = stores
    source = first.create_conversation(labels={"omnigent.wrapper": "prime-native-ui"})
    old = NativeOwner(
        provider="prime-native", environment="host", runtime="private", coordinator="old"
    )
    ticket = first.admit_native(source.id, old)
    replacement = old.model_copy(update={"coordinator": "new"})
    admitted = second.admit_native(source.id, replacement, expected_epoch=ticket.epoch)
    assert admitted.epoch == ticket.epoch
    first.validate_native_admission(ticket)
    stop = first.seal_native_stop(source.id)
    assert stop.unresolved_owners == (old,)
    result = second.finish_native_stop(stop, NativeStopOutcome.VERIFIED)
    assert result.outcome == "unknown"
    assert second.admit_native(source.id, replacement).epoch != ticket.epoch
    with pytest.raises(OmnigentError, match="explicit Stop"):
        first.fork_conversation(source.id, selected_agent_id="target")


def test_verified_close_replacement_uses_original_new_epoch(stores):
    first, second = stores
    source = first.create_conversation(labels={"omnigent.wrapper": "prime-native-ui"})
    old = NativeOwner(
        provider="prime-native", environment="host", runtime="private", coordinator="old"
    )
    first.admit_native(source.id, old)
    stop = first.seal_native_stop(source.id)
    second.finish_native_stop(stop, NativeStopOutcome.VERIFIED)
    launch_ticket = first.invalidate_native_proof(source.id)
    replacement = old.model_copy(update={"coordinator": "new"})
    admitted = second.admit_native(source.id, replacement, expected_epoch=launch_ticket.epoch)
    assert admitted.epoch == launch_ticket.epoch
    current_stop = second.seal_native_stop(source.id)
    assert current_stop.unresolved_owners == ()
    assert first.finish_native_stop(current_stop, NativeStopOutcome.VERIFIED).outcome == "verified"

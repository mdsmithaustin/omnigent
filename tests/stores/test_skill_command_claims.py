import pytest

from omnigent.db.db_models import workspace_scope
from omnigent.entities import MessageData, NewConversationItem, SlashCommandData
from omnigent.entities.conversation import SkillCommandContext, SkillCommandDelivery
from omnigent.errors import OmnigentError
from omnigent.runtime.prompt import history_to_input_items
from omnigent.server.routes._sessions.helpers import (
    _skill_command_identity,
    _validate_skill_command_claim,
)
from omnigent.server.schemas import SessionEventInput


def command_items():
    return [
        NewConversationItem(
            type="slash_command",
            stable_id="ab" * 16,
            response_id="turn_one",
            created_by="alice",
            data=SlashCommandData(
                agent="agent",
                name="review",
                arguments="hello",
                delivery=SkillCommandDelivery(
                    invocation_id="cd" * 16, fingerprint="ef" * 32, context_item_id="12" * 16
                ),
            ),
        ),
        NewConversationItem(
            type="message",
            stable_id="12" * 16,
            response_id="turn_one",
            created_by="alice",
            data=MessageData(
                role="user",
                is_meta=True,
                content=[{"type": "input_text", "text": "Skill context"}],
                skill_context=SkillCommandContext(command_item_id="ab" * 16),
            ),
        ),
    ]


def test_claim_and_context_settle_monotonically(conversation_store):
    store = conversation_store
    conv = store.create_conversation()
    first = store.claim_skill_command(conv.id, command_items())
    assert first.data.delivery.status == "unknown"
    assert history_to_input_items(store.list_items(conv.id).data) == []
    settled = store.settle_skill_command(conv.id, first.id, "ef" * 32, "accepted")
    assert settled.data.delivery.status == "accepted"
    contradictory = store.settle_skill_command(conv.id, first.id, "ef" * 32, "rejected")
    assert contradictory.data.delivery.status == "accepted"
    assert history_to_input_items(store.list_items(conv.id).data) == [
        {"role": "user", "content": [{"type": "input_text", "text": "Skill context"}]}
    ]
    losing = command_items()
    losing[1].stable_id = "34" * 16
    assert store.claim_skill_command(conv.id, losing).deduplicated is True
    assert len(store.list_items(conv.id).data) == 2


@pytest.mark.parametrize("status", ["unknown", "accepted", "rejected"])
def test_forked_claim_and_context_are_display_only(conversation_store, status):
    store = conversation_store
    conv = store.create_conversation()
    item = store.claim_skill_command(conv.id, command_items())
    if status != "unknown":
        store.settle_skill_command(conv.id, item.id, "ef" * 32, status)
    fork = store.fork_conversation(conv.id)
    copied = store.list_items(fork.id).data
    assert copied[0].data.delivery.status == status
    assert copied[0].data.delivery.historical is True
    assert copied[0].data.delivery.context_item_id == copied[1].id
    assert copied[1].data.skill_context.command_item_id == copied[0].id
    assert copied[1].data.skill_context.historical is True
    assert history_to_input_items(copied) == []
    with pytest.raises(ValueError, match="does not match"):
        store.settle_skill_command(fork.id, copied[0].id, "ef" * 32, "accepted")
    assert store.get_item(conv.id, item.id).data.delivery.historical is False


def test_claim_creator_and_stable_record_collision_conflict(conversation_store):
    conv = conversation_store.create_conversation()
    claim = conversation_store.claim_skill_command(conv.id, command_items())
    _validate_skill_command_claim(claim, "cd" * 16, "ef" * 32, "alice")
    with pytest.raises(OmnigentError):
        _validate_skill_command_claim(claim, "cd" * 16, "ef" * 32, "bob")
    ordinary = conversation_store.append(
        conv.id,
        [
            NewConversationItem(
                type="message",
                stable_id="ff" * 16,
                response_id="turn_other",
                data=MessageData(role="user", content=[]),
            )
        ],
    )[0]
    with pytest.raises(OmnigentError):
        _validate_skill_command_claim(ordinary, "cd" * 16, "ef" * 32, "alice")


def test_claim_scope_is_conversation_and_workspace(conversation_store):
    store = conversation_store
    first = store.create_conversation()
    second = store.create_conversation()
    body = SessionEventInput(
        type="slash_command", data={"name": "review", "arguments": "hello", "stable_id": "cd" * 16}
    )
    assert (
        _skill_command_identity(first.id, body)[1] != _skill_command_identity(second.id, body)[1]
    )
    item = store.claim_skill_command(first.id, command_items())
    assert store.get_item(second.id, item.id) is None
    with workspace_scope(100):
        assert store.get_item(first.id, item.id) is None


def test_unbound_meta_retains_its_history_meaning(conversation_store):
    conv = conversation_store.create_conversation()
    items = conversation_store.append(
        conv.id,
        [
            NewConversationItem(
                type="message",
                response_id="turn",
                data=MessageData(
                    role="user",
                    is_meta=True,
                    content=[{"type": "input_text", "text": "Ordinary context"}],
                ),
            )
        ],
    )
    assert history_to_input_items(items) == [
        {"role": "user", "content": [{"type": "input_text", "text": "Ordinary context"}]}
    ]

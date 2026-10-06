from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlparse

import httpx
from playwright.sync_api import Page, Request, Route, expect

from omnigent.entities import NewConversationItem
from omnigent.entities.conversation import parse_item_data
from tests.e2e_ui.conftest import seed_committed_items


def test_saved_native_skill_admission_survives_recovery_and_reload(
    page: Page,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    base_url, session_id = seeded_session
    initial_model_requests = httpx.get(f"{mock_llm_server_url}/stats", timeout=5.0).json()
    fixture = Path(__file__).resolve().parents[3] / (
        "web/src/pages/__fixtures__/nativeSkillAdmission.json"
    )
    items = json.loads(fixture.read_text())
    skill = items[0]
    invocation_id = "f4d13bb24fdd467db5810319cfec2f0d"
    event = {
        "type": "slash_command",
        "data": {
            "kind": "skill",
            "name": "recovery-nonce",
            "arguments": "EFFECT_edf9f79f312ae1a8",
            "stable_id": invocation_id,
        },
    }
    seed_committed_items(
        session_id,
        [
            NewConversationItem(
                type=item["type"],
                response_id=item["response_id"],
                stable_id=item["id"],
                data=parse_item_data(item["type"], {**item, "agent": item.get("model")}),
            )
            for item in items
        ],
    )
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        response = client.get(f"/v1/sessions/{session_id}/items")
        response.raise_for_status()
        saved_skill = next(
            item for item in response.json()["data"] if item["type"] == "slash_command"
        )
        assert saved_skill["delivery"] == skill["delivery"]
        assert saved_skill["native_invocation"] == "/recovery-nonce EFFECT_edf9f79f312ae1a8"
        assert "role" not in saved_skill

    storage_key = (
        "omnigent:skill-submission:v1:"
        + json.dumps([base_url, "local", session_id], separators=(",", ":"))
        + ":"
        + invocation_id
    )
    page.add_init_script(
        """(() => {
            const [key, submission] = """
        + json.dumps([storage_key, {"event": event, "delivery": None}])
        + """;
            if (localStorage.getItem(key) === null)
                localStorage.setItem(key, JSON.stringify(submission));
        })();"""
    )
    command_posts: list[str] = []
    history_reads: list[str] = []

    def observe(request: Request) -> None:
        path = urlparse(request.url).path
        if request.method == "GET" and path == f"/v1/sessions/{session_id}/items":
            history_reads.append(request.url)
        if request.method == "POST" and path in {
            f"/v1/sessions/{session_id}/events",
            "/v1/responses",
        }:
            command_posts.append(request.post_data or "")

    def refuse_command(route: Route) -> None:
        if route.request.method == "POST":
            route.abort()
        else:
            route.continue_()

    page.on("request", observe)
    page.route(f"**/v1/sessions/{session_id}/events", refuse_command)
    page.route("**/v1/responses", refuse_command)
    page.set_viewport_size({"width": 1440, "height": 1000})
    page.goto(f"{base_url}/c/{session_id}")
    transcript = page.get_by_role("log")
    answer = transcript.get_by_text("RECOVERY_NATIVE_DONE EFFECT_edf9f79f312ae1a8", exact=True)
    worked = transcript.get_by_role("button", name=re.compile(r"^Worked"))
    expect(answer).to_be_visible(timeout=20_000)
    expect(worked).to_have_attribute("aria-expanded", "false")
    panel = page.locator("details").filter(has=page.locator("summary", has_text="Skill recovery"))
    expect(panel).to_be_visible()
    with page.expect_response(
        lambda response: (
            urlparse(response.url).path == f"/v1/sessions/{session_id}/items"
            and response.request.method == "GET"
        )
    ) as recovery_read:
        panel.get_by_role("button", name="Check admission", exact=True).click()
    assert recovery_read.value.status == 200
    expect(panel).to_have_count(0)
    submission = page.evaluate("key => JSON.parse(localStorage.getItem(key))", storage_key)
    assert submission == {"event": event, "delivery": skill["delivery"]}
    assert command_posts == []
    assert len(history_reads) >= 2
    assert httpx.get(f"{mock_llm_server_url}/stats", timeout=5.0).json() == initial_model_requests

    for reload in (False, True):
        if reload:
            page.reload()
        expect(answer).to_be_visible(timeout=20_000)
        expect(worked).to_have_attribute("aria-expanded", "false")
        expect(panel).to_have_count(0)
        expect(transcript.get_by_test_id("slash-command-card")).to_be_visible()
        expect(transcript.get_by_test_id("slash-command-card")).to_have_count(1)
        expect(
            transcript.get_by_text("Admitted; completion not confirmed", exact=True)
        ).to_be_visible()
        transcript.get_by_text("Invocation details", exact=True).click()
        expect(transcript.get_by_text(invocation_id, exact=True)).to_be_visible()
        expect(transcript.get_by_role("button", name="Ran 1 shell command")).to_have_count(0)
        expect(
            transcript.get_by_text("Write independent recovery nonce", exact=True)
        ).to_have_count(0)
        expect(
            transcript.get_by_text("(Bash completed with no output)", exact=True)
        ).to_have_count(0)
        expect(transcript.get_by_text(re.compile("Base directory for this skill:"))).to_have_count(
            0
        )
        with page.expect_response(
            lambda response: (
                urlparse(response.url).path == f"/v1/sessions/{session_id}/items"
                and response.request.method == "GET"
            )
        ) as card_read:
            transcript.get_by_role("button", name="Check admission", exact=True).click()
        assert card_read.value.status == 200
        expect(transcript.get_by_role("status")).to_have_text(
            "Saved admission checked. Nothing was resent."
        )
        assert (
            page.evaluate("key => JSON.parse(localStorage.getItem(key))", storage_key)
            == submission
        )
        assert command_posts == []
        expect(worked).to_have_attribute("aria-expanded", "false")

    worked.click()
    expect(worked).to_have_attribute("aria-expanded", "true")
    expect(transcript.get_by_role("button", name="Ran 1 shell command")).to_be_visible()
    worked.click()
    expect(worked).to_have_attribute("aria-expanded", "false")
    expect(
        transcript.get_by_text("Admitted; completion not confirmed", exact=True)
    ).to_be_visible()
    assert command_posts == []
    assert httpx.get(f"{mock_llm_server_url}/stats", timeout=5.0).json() == initial_model_requests

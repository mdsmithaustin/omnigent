import type * as IdentityModule from "@/lib/identity";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { SkillCommandRecovery } from "./SkillCommandRecovery";
import { handleSessionEvent, pumpStreamEvents, useChatStore } from "@/store/chatStore";
import { readSkillSubmissions } from "@/lib/skillCommandRecovery";

vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => "local",
}));

afterEach(() => {
  cleanup();
  localStorage.clear();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

it("recovers a lost submission from durable storage after stream reconciliation and remount", async () => {
  const posts: { type: string; data: { stable_id: string; arguments: string } }[] = [];
  const requests: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input, init) => {
      requests.push(String(input));
      if (init?.method === "POST") {
        posts.push(JSON.parse(init.body));
        throw new Error("Reply lost");
      }
      return new Response(JSON.stringify({ data: [], has_more: false }), {
        headers: { "Content-Type": "application/json" },
      });
    }),
  );
  useChatStore.setState({
    conversationId: "conv_reload",
    abortController: new AbortController(),
    pendingUserMessages: [],
    status: "idle",
  });
  const mounted = render(<SkillCommandRecovery conversationId="conv_reload" />);
  await useChatStore.getState().sendSlashCommand("review", "original arguments", "agent");
  const stableId = posts[0]!.data.stable_id;
  handleSessionEvent(
    {
      type: "slash_command",
      kind: "skill",
      name: "review",
      arguments: "original arguments",
      output: null,
      agentName: "agent",
      itemId: "claim",
      responseId: "turn",
      delivery: { invocation_id: stableId, fingerprint: "cd".repeat(32), status: "unknown" },
    },
    "conv_reload",
  );
  mounted.unmount();
  useChatStore.setState({ pendingUserMessages: [], blocks: [] });
  render(<SkillCommandRecovery conversationId="conv_reload" />);
  expect(screen.getByText("/review original arguments").textContent).toBe(
    "/review original arguments",
  );
  fireEvent.click(screen.getByRole("button", { name: "Check admission" }));
  await waitFor(() =>
    expect(screen.getByRole("status").textContent).toContain(
      "No saved admission found. Outcome remains unknown.",
    ),
  );
  expect(readSkillSubmissions("conv_reload")[0]?.event.data).toEqual({
    kind: "skill",
    name: "review",
    arguments: "original arguments",
    stable_id: stableId,
  });
  expect(posts).toHaveLength(1);
  expect(requests.at(-1)).toContain("/v1/sessions/conv_reload/items?");
});

it("does not submit when the original request cannot be retained", async () => {
  const fetcher = vi.fn(async () => new Response("{}"));
  vi.stubGlobal("fetch", fetcher);
  const storage = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
    throw new Error("Storage is full");
  });
  useChatStore.setState({
    conversationId: "conv_full",
    abortController: new AbortController(),
    pendingUserMessages: [],
    blocks: [],
    status: "idle",
  });
  try {
    await useChatStore.getState().sendSlashCommand("review", "hello", "agent");
    const state = useChatStore.getState();
    expect(JSON.stringify(state.blocks)).toContain("Storage is full. Skill was not sent.");
    expect(state.pendingUserMessages).toEqual([]);
    expect(fetcher).not.toHaveBeenCalled();
  } finally {
    storage.mockRestore();
  }
});

it("shows a later storage failure while retaining the server admission and original request", async () => {
  const conversationId = "conv_later_storage_failure";
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => {
      throw new Error("Reply lost");
    }),
  );
  useChatStore.setState({
    conversationId,
    abortController: new AbortController(),
    pendingUserMessages: [],
    blocks: [],
    status: "idle",
  });
  render(<SkillCommandRecovery conversationId={conversationId} />);
  await act(async () => {
    await useChatStore.getState().sendSlashCommand("review", "original arguments", "agent");
  });
  const original = readSkillSubmissions(conversationId)[0]!;
  const delivery = {
    invocation_id: original.event.data.stable_id,
    fingerprint: "cd".repeat(32),
    status: "accepted" as const,
  };
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
    throw new DOMException("Quota exhausted", "QuotaExceededError");
  });
  const item = {
    id: "claim",
    response_id: "turn",
    type: "slash_command",
    status: "completed",
    kind: "skill",
    name: "review",
    arguments: "original arguments",
    delivery,
  };
  const frame = new TextEncoder().encode(
    `event: response.output_item.done\ndata: ${JSON.stringify({ item })}\n\ndata: [DONE]\n\n`,
  );
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(frame);
      controller.close();
    },
  });
  let end: string | undefined;
  await act(async () => {
    end = await pumpStreamEvents(
      conversationId,
      body,
      new AbortController(),
      useChatStore.setState,
      useChatStore.getState,
    );
  });
  expect(end).toBe("server_closed");
  expect(useChatStore.getState().blocks).toContainEqual(
    expect.objectContaining({ type: "slash_command", delivery }),
  );
  expect(useChatStore.getState().pendingUserMessages).toEqual([]);
  expect(readSkillSubmissions(conversationId)).toEqual([original]);
  expect(screen.getByRole("alert").textContent).toContain("Could not update saved skill admission");

  vi.stubGlobal(
    "fetch",
    vi.fn(
      async () =>
        new Response(JSON.stringify({ data: [item], has_more: false }), {
          headers: { "Content-Type": "application/json" },
        }),
    ),
  );
  fireEvent.click(screen.getByRole("button", { name: "Check admission" }));
  await waitFor(() => expect(screen.getByText("Admitted; completion not confirmed")).toBeTruthy());
  expect(readSkillSubmissions(conversationId)).toEqual([original]);
});

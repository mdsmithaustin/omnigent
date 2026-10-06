import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import type * as IdentityModule from "@/lib/identity";
import type * as HostsModule from "@/hooks/useHosts";
import type * as HealthModule from "@/hooks/RunnerHealthProvider";
import type * as FilesModule from "@/hooks/useWorkspaceChangedFiles";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, act, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { readSkillSubmissions, type SkillCommandSubmission } from "@/lib/skillCommandRecovery";
import { clearSessionDrafts } from "@/lib/sessionDrafts";
import { isSlashCommandItem, type ConversationItem } from "@/lib/conversationItems";
import type { Session } from "@/lib/types";
import { TerminalFirstContextProvider } from "@/shell/TerminalFirstContext";
import { bindConversationForTest, initChatStore, useChatStore } from "@/store/chatStore";
import { ChatPage } from "./ChatPage";

vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => "local",
}));
vi.mock("@/hooks/useAgents", () => ({
  useAgents: () => ({ data: [{ id: "agent", name: "native" }], error: null, refetch: vi.fn() }),
  useSessionAgent: () => ({ data: { id: "agent", name: "native" } }),
}));
vi.mock("@/hooks/useSidebarData", () => ({ useLoadedConversations: () => ({}) }));
vi.mock("@/hooks/useUnseenConversations", () => ({ useMarkConversationSeen: () => {} }));
vi.mock("@/hooks/useSessionOnlineRefresh", () => ({
  useRefreshSessionStateOnRunnerOnline: () => {},
}));
vi.mock("@/hooks/RunnerHealthProvider", async (importOriginal) => ({
  ...(await importOriginal<typeof HealthModule>()),
  useSessionRunnerOnline: () => true,
  useSessionHostOnline: () => true,
}));
vi.mock("@/hooks/useHosts", async (importOriginal) => ({
  ...(await importOriginal<typeof HostsModule>()),
  useHosts: () => ({ data: [] }),
  useHostModelOptions: () => ({ data: [] }),
}));
vi.mock("@/hooks/useWorkspaceChangedFiles", async (importOriginal) => ({
  ...(await importOriginal<typeof FilesModule>()),
  useWorkspaceAllFiles: () => ({ data: [] }),
  useWorkspaceDirectory: () => ({ data: [] }),
}));
vi.mock("@/hooks/useGithub", () => ({ useGithubInfo: () => ({ data: undefined }) }));
vi.mock("@/hooks/useComposerGitStatus", () => ({
  useComposerGitStatus: () => ({ branchState: "unknown", githubState: "unknown" }),
}));
vi.mock("@/hooks/useChildSessions", () => ({ useChildSessions: () => ({ children: [] }) }));
vi.mock("@/shell/MainTerminalView", () => ({ MainTerminalView: () => null }));

const conversationId = "conv_native_skill";
const originalStore = useChatStore.getState();
let queryClient: QueryClient;
let posts: { event: SkillCommandSubmission["event"]; saved: SkillCommandSubmission[] }[];
let messages: unknown[];
let historyRequests: URL[];
let streams: ReadableStreamDefaultController<Uint8Array>[];
const nativeItems: ConversationItem[] = JSON.parse(
  readFileSync(
    path.join(
      path.dirname(fileURLToPath(import.meta.url)),
      "__fixtures__/nativeSkillAdmission.json",
    ),
    "utf8",
  ),
);

function resetConversation() {
  const harness = "claude-native";
  initChatStore(queryClient);
  bindConversationForTest(conversationId, {
    boundAgentId: "agent",
    boundAgentName: "native",
    sessionHarness: harness,
    sessionHostId: "host",
    loadingConversation: false,
    abortController: new AbortController(),
    status: "idle",
    sessionStatus: "idle",
    blocks: [],
    pendingUserMessages: [],
  });
  const session: Session = {
    id: conversationId,
    agentId: "agent",
    agentName: "native",
    harness,
    hostId: "host",
    runnerId: "runner",
    workspace: "/workspace",
    status: "idle",
    permissionLevel: 4,
    createdAt: 1,
    title: null,
    items: [],
    parentSessionId: null,
    subAgentName: null,
    kind: "default",
    labels: {
      "omnigent.wrapper": "claude-code-native-ui",
      "omnigent.ui": "terminal",
    },
  };
  queryClient.setQueryData(["session", conversationId], session);
}

function mountChat() {
  return render(
    <QueryClientProvider client={queryClient}>
      <TooltipProvider>
        <TerminalFirstContextProvider
          value={{
            isClaudeNative: true,
            isNativeWrapper: true,
            isTerminalFirst: true,
            isShellView: false,
            view: "chat",
            terminalViewKey: null,
            setView: vi.fn(),
            terminalsAvailable: true,
            terminalStartingUp: false,
          }}
        >
          <MemoryRouter initialEntries={[`/c/${conversationId}`]}>
            <Routes>
              <Route path="/c/:conversationId" element={<ChatPage />} />
            </Routes>
          </MemoryRouter>
        </TerminalFirstContextProvider>
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  localStorage.clear();
  clearSessionDrafts();
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  posts = [];
  messages = [];
  historyRequests = [];
  streams = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), window.location.origin);
      if (url.pathname === `/v1/sessions/${conversationId}/events` && init?.method === "POST") {
        const event = JSON.parse(String(init.body));
        if (event.type === "slash_command") {
          posts.push({ event, saved: readSkillSubmissions(conversationId) });
          throw new Error("Reply lost");
        }
        messages.push(event);
        return Response.json({});
      }
      if (url.pathname === "/v1/skills") {
        return Response.json({ skills: [{ name: "recovery-nonce", description: "Write nonce" }] });
      }
      if (url.pathname === `/v1/sessions/${conversationId}/stream`) {
        const body = new ReadableStream<Uint8Array>({
          start(controller) {
            streams.push(controller);
            init?.signal?.addEventListener(
              "abort",
              () => controller.error(new DOMException("Aborted", "AbortError")),
              { once: true },
            );
          },
        });
        return new Response(body, { headers: { "Content-Type": "text/event-stream" } });
      }
      if (url.pathname === `/v1/sessions/${conversationId}/items`) {
        historyRequests.push(url);
        return Response.json({
          data: nativeItems
            .map((item) =>
              isSlashCommandItem(item)
                ? {
                    ...item,
                    delivery: { ...item.delivery, invocation_id: posts[0]!.event.data.stable_id },
                  }
                : item,
            )
            .toReversed(),
          has_more: false,
        });
      }
      if (url.pathname === `/v1/sessions/${conversationId}`) {
        return Response.json({
          id: conversationId,
          agent_id: "agent",
          agent_name: "native",
          harness: "claude-native",
          host_id: "host",
          runner_id: "runner",
          workspace: "/workspace",
          status: "idle",
          permission_level: 4,
          created_at: 1,
          title: null,
          items: [],
          labels: { "omnigent.wrapper": "claude-code-native-ui", "omnigent.ui": "terminal" },
        });
      }
      return Response.json({ data: [], has_more: false });
    }),
  );
});

afterEach(() => {
  cleanup();
  initChatStore(queryClient);
  queryClient.clear();
  useChatStore.setState(originalStore, true);
  localStorage.clear();
  clearSessionDrafts();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function coldReload() {
  initChatStore(queryClient);
  useChatStore.setState(originalStore, true);
  queryClient.clear();
  return mountChat();
}

function expectVisibleAdmission(invocationId: string) {
  expect(screen.getByRole("button", { name: /Worked/ })).toHaveAttribute("aria-expanded", "false");
  expect(screen.getByText("RECOVERY_NATIVE_DONE EFFECT_edf9f79f312ae1a8")).toBeVisible();
  expect(screen.queryByText("Admitted; completion not confirmed")).toBeVisible();
  expect(screen.getAllByTestId("slash-command-card")).toHaveLength(1);
  fireEvent.click(screen.getByText("Invocation details"));
  expect(screen.getByText(invocationId)).toBeVisible();
}

async function recoverOriginal() {
  resetConversation();
  let mounted = mountChat();
  const textarea = await screen.findByLabelText("Message the agent");
  await waitFor(() =>
    expect(
      queryClient.getQueriesData({ queryKey: ["skills"] }).map(([, data]) => data),
    ).toContainEqual([{ name: "recovery-nonce", description: "Write nonce" }]),
  );
  fireEvent.change(textarea, { target: { value: "/recovery-nonce EFFECT_edf9f79f312ae1a8" } });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
  await waitFor(() => expect(posts).toHaveLength(1));
  const { event, saved } = posts[0]!;
  expect(event.data).toEqual({
    kind: "skill",
    name: "recovery-nonce",
    arguments: "EFFECT_edf9f79f312ae1a8",
    stable_id: expect.stringMatching(/^[0-9a-f]{32}$/),
  });
  expect(saved).toEqual([{ event, delivery: null }]);
  expect(messages).toEqual([]);
  expect(await screen.findByText("Skill recovery")).toBeVisible();
  mounted.unmount();
  mounted = coldReload();
  await screen.findByText("RECOVERY_NATIVE_DONE EFFECT_edf9f79f312ae1a8");
  const panel = screen.getByText("Skill recovery").closest("details")!;
  fireEvent.click(within(panel).getByRole("button", { name: "Check admission" }));
  await waitFor(() => expect(screen.queryByText("Skill recovery")).toBeNull());
  expect(readSkillSubmissions(conversationId)).toEqual([
    {
      event,
      delivery: {
        ...nativeItems.find(isSlashCommandItem)!.delivery,
        invocation_id: event.data.stable_id,
      },
    },
  ]);
  expect(posts).toHaveLength(1);
  expect(messages).toEqual([]);
  return { mounted, event };
}

describe("native admission in the real transcript", () => {
  it("retains the accepted original journal and one POST after checking history", async () => {
    const { event } = await recoverOriginal();
    expect(readSkillSubmissions(conversationId)[0]?.delivery?.status).toBe("accepted");
    expect(readSkillSubmissions(conversationId)[0]?.delivery?.invocation_id).toBe(
      event.data.stable_id,
    );
    expect(historyRequests).toHaveLength(2);
    expect(posts).toHaveLength(1);
    expect(messages).toEqual([]);
  });

  it("keeps the accepted original card visible with Worked closed after recovery, reload and reconnect", async () => {
    const { mounted, event } = await recoverOriginal();
    expectVisibleAdmission(event.data.stable_id);
    fireEvent.click(screen.getByRole("button", { name: "Check admission" }));
    expect(await screen.findByText("Saved admission checked. Nothing was resent.")).toBeVisible();
    expect(posts).toHaveLength(1);
    mounted.unmount();
    coldReload();
    await screen.findByText("RECOVERY_NATIVE_DONE EFFECT_edf9f79f312ae1a8");
    expect(screen.queryByText("Skill recovery")).toBeNull();
    expectVisibleAdmission(event.data.stable_id);
    const requestsBeforeReconnect = historyRequests.length;
    const stream = streams.at(-1)!;
    await act(async () => stream.close());
    await waitFor(() => expect(historyRequests.length).toBeGreaterThan(requestsBeforeReconnect));
    expect(screen.getByRole("button", { name: /Worked/ })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    expect(screen.getByText("Admitted; completion not confirmed")).toBeVisible();
    expect(screen.getAllByTestId("slash-command-card")).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "Check admission" }));
    expect(await screen.findByText("Saved admission checked. Nothing was resent.")).toBeVisible();
    expect(readSkillSubmissions(conversationId)[0]?.event).toEqual(event);
    expect(readSkillSubmissions(conversationId)[0]?.delivery?.invocation_id).toBe(
      event.data.stable_id,
    );
    expect(posts).toHaveLength(1);
    expect(messages).toEqual([]);
    expect(historyRequests.every((url) => !url.searchParams.has("invocation_id"))).toBe(true);
  });
});

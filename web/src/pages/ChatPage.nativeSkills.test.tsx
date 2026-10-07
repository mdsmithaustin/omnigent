import type * as IdentityModule from "@/lib/identity";
import type * as HostsModule from "@/hooks/useHosts";
import type * as HealthModule from "@/hooks/RunnerHealthProvider";
import type * as FilesModule from "@/hooks/useWorkspaceChangedFiles";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { readSkillSubmissions, type SkillCommandSubmission } from "@/lib/skillCommandRecovery";
import { clearSessionDrafts } from "@/lib/sessionDrafts";
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
vi.mock("@/components/chat/Transcript", () => ({ Transcript: () => null }));
vi.mock("@/shell/MainTerminalView", () => ({ MainTerminalView: () => null }));

const conversationId = "conv_native_skill";
const originalStore = useChatStore.getState();
let queryClient: QueryClient;
let posts: { event: SkillCommandSubmission["event"]; saved: SkillCommandSubmission[] }[];
let messages: unknown[];
let historyRequests: URL[];
let historyHasClaim: boolean;

function resetConversation(harness: "claude-native" | "codex-native") {
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
      "omnigent.wrapper": harness === "claude-native" ? "claude-code-native-ui" : "codex-native-ui",
      "omnigent.ui": "terminal",
    },
  };
  queryClient.setQueryData(["session", conversationId], session);
}

function mountChat(harness: "claude-native" | "codex-native") {
  resetConversation(harness);
  return render(
    <QueryClientProvider client={queryClient}>
      <TooltipProvider>
        <TerminalFirstContextProvider
          value={{
            isClaudeNative: harness === "claude-native",
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
  historyHasClaim = true;
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
        return Response.json({ skills: [{ name: "Team:Review", description: "Review source" }] });
      }
      if (url.pathname === `/v1/sessions/${conversationId}/items`) {
        historyRequests.push(url);
        if (!url.searchParams.has("after")) {
          return Response.json({
            data: [{ id: "cursor", type: "message", role: "user", content: [] }],
            has_more: true,
          });
        }
        return Response.json({
          data: historyHasClaim
            ? [
                {
                  id: "claim",
                  type: "slash_command",
                  status: "completed",
                  kind: "skill",
                  name: "Team:Review",
                  arguments: posts[0]?.event.data.arguments,
                  delivery: {
                    invocation_id: posts[0]?.event.data.stable_id,
                    fingerprint: "cd".repeat(32),
                    status: "unknown",
                  },
                },
              ]
            : [],
          has_more: false,
        });
      }
      return Response.json({ data: [], has_more: false });
    }),
  );
});

afterEach(() => {
  cleanup();
  queryClient.clear();
  useChatStore.setState(originalStore, true);
  localStorage.clear();
  clearSessionDrafts();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("native ChatPage skill submission", () => {
  it.each([
    ["claude-native", "/Team:Review src/a.ts  keep\tspacing", "src/a.ts  keep\tspacing"],
    ["codex-native", "$Team:Review src/a.ts  keep\tspacing", "src/a.ts  keep\tspacing"],
    ["codex-native", "$Team:Review", ""],
  ] as const)(
    "sends %s %s once and recovers the original journal by history",
    async (harness, text, args) => {
      const mounted = mountChat(harness);
      const textarea = await screen.findByLabelText("Message the agent");
      await waitFor(() =>
        expect(
          queryClient.getQueriesData({ queryKey: ["skills"] }).map(([, data]) => data),
        ).toContainEqual([{ name: "Team:Review", description: "Review source" }]),
      );
      expect(textarea).not.toBeDisabled();
      fireEvent.change(textarea, { target: { value: text } });
      fireEvent.click(screen.getByRole("button", { name: "Send" }));
      await waitFor(() => expect(posts.length + messages.length).toBe(1));
      expect(
        posts,
        `Expected one slash_command POST; observed plaintext ${JSON.stringify(messages)}`,
      ).toHaveLength(1);
      expect(messages).toEqual([]);
      const { event, saved } = posts[0]!;
      expect(event.data).toEqual({
        kind: "skill",
        name: "Team:Review",
        arguments: args,
        stable_id: expect.stringMatching(/^[0-9a-f]{32}$/),
      });
      expect(saved).toEqual([{ event, delivery: null }]);
      expect(screen.getByText("Skill recovery")).toBeVisible();
      mounted.unmount();
      useChatStore.setState({ blocks: [], pendingUserMessages: [] });
      mountChat(harness);
      fireEvent.click(await screen.findByRole("button", { name: "Check delivery" }));
      await waitFor(() =>
        expect(readSkillSubmissions(conversationId)[0]?.delivery?.invocation_id).toBe(
          event.data.stable_id,
        ),
      );
      expect(historyRequests).toHaveLength(2);
      expect(historyRequests.map((url) => url.searchParams.get("after"))).toEqual([null, "cursor"]);
      expect(historyRequests.every((url) => !url.searchParams.has("invocation_id"))).toBe(true);
      expect(readSkillSubmissions(conversationId)[0]?.event).toEqual(event);
      expect(readSkillSubmissions(conversationId)[0]?.delivery?.status).toBe("unknown");
      expect(posts).toHaveLength(1);
      expect(messages).toEqual([]);
    },
  );

  it("keeps a lost submission unknown when paginated history has no claim", async () => {
    historyHasClaim = false;
    const mounted = mountChat("codex-native");
    const textarea = await screen.findByLabelText("Message the agent");
    await waitFor(() =>
      expect(
        queryClient.getQueriesData({ queryKey: ["skills"] }).map(([, data]) => data),
      ).toContainEqual([{ name: "Team:Review", description: "Review source" }]),
    );
    fireEvent.change(textarea, { target: { value: "$Team:Review arg" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(posts).toHaveLength(1));
    const original = posts[0]!.event;
    mounted.unmount();
    mountChat("codex-native");
    fireEvent.click(await screen.findByRole("button", { name: "Check delivery" }));
    expect(
      await screen.findByText(
        "No saved delivery found. Outcome remains unknown. Nothing was resent.",
      ),
    ).toBeVisible();
    expect(historyRequests).toHaveLength(2);
    expect(historyRequests.map((url) => url.searchParams.get("after"))).toEqual([null, "cursor"]);
    expect(historyRequests.every((url) => !url.searchParams.has("invocation_id"))).toBe(true);
    expect(readSkillSubmissions(conversationId)).toEqual([{ event: original, delivery: null }]);
    expect(screen.getByText("Admission unknown")).toBeVisible();
    expect(posts).toHaveLength(1);
    expect(messages).toEqual([]);
  });

  it("does not POST when the initial browser journal write fails", async () => {
    mountChat("claude-native");
    const textarea = await screen.findByLabelText("Message the agent");
    await waitFor(() =>
      expect(
        queryClient.getQueriesData({ queryKey: ["skills"] }).map(([, data]) => data),
      ).toContainEqual([{ name: "Team:Review", description: "Review source" }]),
    );
    const setItem = Storage.prototype.setItem;
    const failedWrite = vi.fn();
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(function (this: Storage, key, value) {
      if (key.startsWith("omnigent:skill-submission:v1:")) {
        failedWrite(key, value);
        throw new Error("Journal storage blocked");
      }
      setItem.call(this, key, value);
    });
    fireEvent.change(textarea, { target: { value: "/Team:Review arg" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() =>
      expect(useChatStore.getState().blocks).toContainEqual(
        expect.objectContaining({
          type: "error",
          message: "Journal storage blocked. Skill was not sent.",
        }),
      ),
    );
    expect(failedWrite).toHaveBeenCalledTimes(1);
    expect(readSkillSubmissions(conversationId)).toEqual([]);
    expect(posts).toEqual([]);
    expect(messages).toEqual([]);
  });
});

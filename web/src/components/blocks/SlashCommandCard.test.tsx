// DOM smoke for the slash-command indicator. Pure jsdom — no
// canvas, no clipboard, no animation timing.

import { ConversationScopeContext } from "@/components/chat/conversationScope";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SlashCommandCard } from "./SlashCommandCard";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("SlashCommandCard", () => {
  it("renders the 'Skill' framing + name with no payload", () => {
    render(
      <SlashCommandCard kind="skill" name="dev-productivity:simplify" arguments="" output={null} />,
    );
    expect(screen.getByText("Skill")).toBeDefined();
    expect(screen.getByText("dev-productivity:simplify")).toBeDefined();
  });

  it("kind='command' switches the prefix to 'Command'", () => {
    // Bucket-C CLI built-ins (``/effort``, ``/clear``, ``/compact``,
    // ``/model``, ``/ultrareview``) render with the Command label —
    // distinct from user-authored Skills.
    render(<SlashCommandCard kind="command" name="effort" arguments="high" output={null} />);
    expect(screen.getByText("Command")).toBeDefined();
    expect(screen.getByText("effort")).toBeDefined();
    expect(screen.getByText("high")).toBeDefined();
    // The data attribute lets the snapshot/styling tests later
    // differentiate cards by kind without inspecting class lists.
    const card = screen.getByTestId("slash-command-card");
    expect(card.getAttribute("data-slash-kind")).toBe("command");
  });

  it("shows args inline in the trigger row when present", () => {
    render(<SlashCommandCard kind="skill" name="oncall" arguments="file-bug" output={null} />);
    expect(screen.getByText("oncall")).toBeDefined();
    expect(screen.getByText("file-bug")).toBeDefined();
  });

  it("collapsed by default; click reveals labelled Arguments and Output panels", () => {
    const { container } = render(
      <SlashCommandCard
        kind="skill"
        name="oncall"
        arguments="file-bug"
        output="oncall: file-bug subcommand started"
      />,
    );
    const trigger = container.querySelector<HTMLElement>('[data-slot="collapsible-trigger"]');
    expect(trigger).not.toBeNull();
    expect(trigger!.getAttribute("data-state")).toBe("closed");

    fireEvent.click(trigger!);

    expect(trigger!.getAttribute("data-state")).toBe("open");
    // CodeBlock may split text into multiple syntax-highlight spans;
    // ``getAllByText`` tolerates that as long as at least one match
    // exists.
    expect(screen.getAllByText("Arguments").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Output").length).toBeGreaterThan(0);
    expect(screen.getAllByText("file-bug").length).toBeGreaterThan(0);
    expect(
      screen.getAllByText((_, node) =>
        Boolean(node?.textContent?.includes("oncall: file-bug subcommand started")),
      ).length,
    ).toBeGreaterThan(0);
  });

  it("no payload renders without a Collapsible wrapper", () => {
    const { container } = render(
      <SlashCommandCard kind="skill" name="dev-productivity:simplify" arguments="" output={null} />,
    );
    expect(container.querySelector('[data-slot="collapsible-trigger"]')).toBeNull();
  });
});

it.each([
  ["unknown", false, "Admission unknown"],
  ["rejected", false, "Rejected before admission"],
  ["accepted", false, "Admitted; completion not confirmed"],
  ["accepted", true, "Historical copy"],
] as const)("shows saved %s admission with historical=%s", (status, historical, label) => {
  const props = {
    kind: "skill" as const,
    name: "review",
    arguments: "",
    output: null,
    delivery: { invocation_id: "ab".repeat(16), fingerprint: "cd".repeat(32), status, historical },
  };
  const { container } = render(
    <ConversationScopeContext.Provider value="conv_check">
      <SlashCommandCard {...props} />
    </ConversationScopeContext.Provider>,
  );
  expect(container.textContent).toContain(label);
  if (historical || status !== "unknown") {
    expect(screen.queryByRole("button", { name: /Check / })).toBeNull();
  } else {
    expect(screen.getByRole("button", { name: "Check delivery" })).toBeEnabled();
  }
});

it.each([
  ["accepted", "Admitted; completion not confirmed"],
  ["rejected", "Rejected before admission"],
] as const)(
  "checks %s delivery from a transcript card using only history reads",
  async (status, label) => {
    const fetcher = vi.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) =>
        new Response(
          JSON.stringify({
            data: [
              {
                id: "claim",
                type: "slash_command",
                delivery: {
                  invocation_id: "ab".repeat(16),
                  fingerprint: "cd".repeat(32),
                  status,
                },
              },
            ],
            has_more: false,
          }),
          { headers: { "Content-Type": "application/json" } },
        ),
    );
    vi.stubGlobal("fetch", fetcher);
    render(
      <ConversationScopeContext.Provider value="conv_check">
        <SlashCommandCard
          kind="skill"
          name="review"
          arguments="hello"
          output={null}
          delivery={{
            invocation_id: "ab".repeat(16),
            fingerprint: "cd".repeat(32),
            status: "unknown",
          }}
        />
      </ConversationScopeContext.Provider>,
    );
    fireEvent.click(screen.getByRole("button", { name: "Check delivery" }));
    await waitFor(() => expect(screen.getByText(label)).toBeVisible());
    expect(screen.queryByRole("button", { name: /Check / })).toBeNull();
    expect(fetcher.mock.calls).toHaveLength(1);
    expect(String(fetcher.mock.calls[0]?.[0])).toContain("/v1/sessions/conv_check/items?");
    expect(fetcher.mock.calls[0]?.[1]?.method ?? "GET").toBe("GET");
    expect(screen.getByRole("status").textContent).toBe(
      "Saved delivery checked. Nothing was resent.",
    );
  },
);

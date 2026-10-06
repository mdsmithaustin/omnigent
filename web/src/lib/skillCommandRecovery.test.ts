import { expect, it, vi } from "vitest";
import { checkSkillAdmission } from "./skillCommandRecovery";
import { BlockStream } from "./blockStream";
import { itemsToBlocks } from "./itemsToBlocks";
import { parseEvent } from "./sse";
import { buildBubbles } from "./renderItems";

it.each(["accepted", "rejected", "unknown"] as const)(
  "preserves %s admission through history and live rendering",
  (status) => {
    const delivery = {
      invocation_id: "ab".repeat(16),
      fingerprint: "cd".repeat(32),
      status,
      historical: false,
    };
    const item = {
      id: "ef".repeat(16),
      response_id: "turn",
      type: "slash_command" as const,
      status: "completed" as const,
      kind: "skill" as const,
      name: "review",
      arguments: "hello",
      delivery,
    };
    const history = itemsToBlocks([item]);
    expect(history.find((block) => block.type === "slash_command")).toMatchObject({ delivery });
    const event = parseEvent("response.output_item.done", { item });
    expect(event).toMatchObject({ delivery });
    const live = new BlockStream().reduceSync([event!]);
    expect(live.find((block) => block.type === "slash_command")).toMatchObject({ delivery });
    expect(JSON.stringify(buildBubbles(history, null))).toContain(JSON.stringify(delivery));
  },
);

it("checks older history pages by invocation identity and rejects historical recovery", async () => {
  const delivery = {
    invocation_id: "ab".repeat(16),
    fingerprint: "cd".repeat(32),
    status: "unknown",
    historical: true,
  };
  const requests: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input) => {
      requests.push(String(input));
      return new Response(
        JSON.stringify(
          requests.length === 1
            ? {
                data: [
                  {
                    id: "newer",
                    type: "slash_command",
                    name: "review",
                    arguments: "same text",
                    delivery: { ...delivery, invocation_id: "12".repeat(16), historical: false },
                  },
                ],
                has_more: true,
              }
            : {
                data: [
                  {
                    id: "older",
                    type: "slash_command",
                    name: "review",
                    arguments: "same text",
                    delivery,
                  },
                ],
                has_more: false,
              },
        ),
        { headers: { "Content-Type": "application/json" } },
      );
    }),
  );
  try {
    await expect(checkSkillAdmission("conv_fork", delivery.invocation_id)).rejects.toThrow(
      "Historical copies cannot be recovered.",
    );
    expect(requests).toHaveLength(2);
    expect(requests[1]).toContain("after=newer");
  } finally {
    vi.unstubAllGlobals();
  }
});

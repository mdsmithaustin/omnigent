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

it.each([
  ["accepted", false],
  ["rejected", false],
  ["unknown", false],
  ["unknown", true],
] as const)(
  "checks %s history by invocation identity with historical=%s",
  async (status, historical) => {
    const delivery = {
      invocation_id: "ab".repeat(16),
      fingerprint: "cd".repeat(32),
      status,
      historical,
    };
    const requests: { url: string; method: string }[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input, init) => {
        requests.push({ url: String(input), method: init?.method ?? "GET" });
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
      if (historical) {
        await expect(checkSkillAdmission("conv_fork", delivery.invocation_id)).rejects.toThrow(
          "Historical copies cannot be recovered.",
        );
      } else {
        await expect(checkSkillAdmission("conv_fork", delivery.invocation_id)).resolves.toEqual(
          delivery,
        );
      }
      expect(requests).toHaveLength(2);
      expect(requests.map(({ method }) => method)).toEqual(["GET", "GET"]);
      expect(
        requests.map(({ url }) => new URL(url, "http://localhost").searchParams.get("after")),
      ).toEqual([null, "newer"]);
      expect(
        requests.every(({ url }) =>
          new URL(url, "http://localhost").pathname.endsWith("/v1/sessions/conv_fork/items"),
        ),
      ).toBe(true);
      expect(
        requests.every(
          ({ url }) => !new URL(url, "http://localhost").searchParams.has("invocation_id"),
        ),
      ).toBe(true);
    } finally {
      vi.unstubAllGlobals();
    }
  },
);

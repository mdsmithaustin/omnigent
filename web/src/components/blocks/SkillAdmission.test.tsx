import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { checkSkillAdmission } from "@/lib/skillCommandRecovery";
import type { SkillCommandDelivery } from "@/lib/skillCommandDelivery";
import { SkillAdmission } from "./SkillAdmission";

vi.mock("@/lib/skillCommandRecovery", () => ({ checkSkillAdmission: vi.fn() }));

const invocationId = "ab".repeat(16);
const unknown: SkillCommandDelivery = {
  invocation_id: invocationId,
  fingerprint: "cd".repeat(32),
  status: "unknown",
};

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

it.each([
  [undefined, "Admission unknown", true],
  [unknown, "Admission unknown", true],
  [{ ...unknown, status: "accepted" }, "Admitted; completion not confirmed", false],
  [{ ...unknown, status: "rejected" }, "Rejected before admission", false],
  [{ ...unknown, historical: true }, "Historical copy", false],
  [{ ...unknown, status: "accepted", historical: true }, "Historical copy", false],
  [{ ...unknown, status: "rejected", historical: true }, "Historical copy", false],
] satisfies [SkillCommandDelivery | undefined, string, boolean][])(
  "renders %j as %s with actionable=%s",
  (delivery, label, actionable) => {
    render(
      <SkillAdmission
        invocationId={invocationId}
        delivery={delivery}
        conversationId="conv_check"
      />,
    );
    expect(screen.getByText(label)).toBeVisible();
    fireEvent.click(screen.getByText("Invocation details"));
    expect(screen.getByText(invocationId)).toBeVisible();
    if (actionable) {
      expect(screen.getByRole("button", { name: "Check delivery" })).toBeEnabled();
    } else {
      expect(screen.queryByRole("button", { name: /Check / })).toBeNull();
    }
  },
);

it.each([undefined, unknown])("keeps %j display-only without a conversation", (delivery) => {
  render(<SkillAdmission invocationId={invocationId} delivery={delivery} />);
  expect(screen.getByText("Admission unknown")).toBeVisible();
  expect(screen.queryByRole("button", { name: /Check / })).toBeNull();
});

it.each([
  [undefined, "accepted", "Admitted; completion not confirmed"],
  [undefined, "rejected", "Rejected before admission"],
  [unknown, "accepted", "Admitted; completion not confirmed"],
  [unknown, "rejected", "Rejected before admission"],
] as const)("hides the action after checking %j to %s", async (delivery, status, label) => {
  let resolve!: (result: SkillCommandDelivery) => void;
  vi.mocked(checkSkillAdmission).mockReturnValue(
    new Promise((done) => {
      resolve = done;
    }),
  );
  render(
    <SkillAdmission invocationId={invocationId} delivery={delivery} conversationId="conv_check" />,
  );
  fireEvent.click(screen.getByRole("button", { name: "Check delivery" }));
  expect(screen.getByRole("button", { name: "Checking delivery…" })).toBeDisabled();
  await act(async () => resolve({ ...unknown, status }));
  expect(screen.getByText(label)).toBeVisible();
  expect(screen.getByRole("status")).toHaveTextContent(
    "Saved delivery checked. Nothing was resent.",
  );
  expect(screen.queryByRole("button", { name: /Check.*delivery/ })).toBeNull();
  expect(checkSkillAdmission).toHaveBeenCalledWith("conv_check", invocationId);
});

it.each([
  [null, "No saved delivery found. Outcome remains unknown. Nothing was resent."],
  [unknown, "Saved delivery checked. Nothing was resent."],
  [new Error("History unavailable"), "History unavailable"],
] as const)("keeps an unresolved check actionable for %j", async (result, message) => {
  if (result instanceof Error) vi.mocked(checkSkillAdmission).mockRejectedValue(result);
  else vi.mocked(checkSkillAdmission).mockResolvedValue(result);
  render(
    <SkillAdmission invocationId={invocationId} delivery={unknown} conversationId="conv_check" />,
  );
  fireEvent.click(screen.getByRole("button", { name: "Check delivery" }));
  expect(await screen.findByRole("status")).toHaveTextContent(message);
  expect(screen.getByText("Admission unknown")).toBeVisible();
  expect(screen.getByRole("button", { name: "Check delivery" })).toBeEnabled();
});

it.each([
  [{ ...unknown, status: "accepted" }, "Admitted; completion not confirmed"],
  [{ ...unknown, status: "rejected" }, "Rejected before admission"],
  [{ ...unknown, historical: true }, "Historical copy"],
] satisfies [SkillCommandDelivery, string][])(
  "prefers updated props %j after an unknown check",
  async (delivery, label) => {
    vi.mocked(checkSkillAdmission).mockResolvedValue(unknown);
    const mounted = render(
      <SkillAdmission invocationId={invocationId} delivery={unknown} conversationId="conv_check" />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Check delivery" }));
    expect(await screen.findByRole("status")).toHaveTextContent("Saved delivery checked.");
    expect(screen.getByRole("button", { name: "Check delivery" })).toBeEnabled();
    mounted.rerender(
      <SkillAdmission
        invocationId={invocationId}
        delivery={delivery}
        conversationId="conv_check"
      />,
    );
    expect(screen.getByText(label)).toBeVisible();
    expect(screen.queryByRole("button", { name: /Check / })).toBeNull();
  },
);

it.each([
  ["accepted", "Admitted; completion not confirmed"],
  ["rejected", "Rejected before admission"],
] as const)(
  "retains a checked %s outcome when props become unknown or missing",
  async (status, label) => {
    vi.mocked(checkSkillAdmission).mockResolvedValue({ ...unknown, status });
    const mounted = render(
      <SkillAdmission invocationId={invocationId} conversationId="conv_check" />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Check delivery" }));
    expect(await screen.findByText(label)).toBeVisible();
    for (const delivery of [unknown, undefined]) {
      mounted.rerender(
        <SkillAdmission
          invocationId={invocationId}
          delivery={delivery}
          conversationId="conv_check"
        />,
      );
      expect(screen.getByText(label)).toBeVisible();
      expect(screen.queryByRole("button", { name: /Check / })).toBeNull();
    }
  },
);

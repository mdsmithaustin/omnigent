import { z } from "zod";
import { getOmnigentServerIdentity } from "./host";
import { getCurrentUserId } from "./identity";
import { fetchSessionItemsPage } from "./sessionsApi";
import { skillCommandDeliverySchema, type SkillCommandDelivery } from "./skillCommandDelivery";

const submissionSchema = z.object({
  event: z.object({
    type: z.literal("slash_command"),
    data: z.object({
      kind: z.literal("skill"),
      name: z.string(),
      arguments: z.string(),
      stable_id: z.string().regex(/^[0-9a-f]{32}$/),
    }),
  }),
  delivery: skillCommandDeliverySchema.nullable(),
});
export type SkillCommandSubmission = z.infer<typeof submissionSchema>;
const changeEvent = "omnigent:skill-submissions-changed";
const admissionPersistenceErrors = new Map<string, string>();

function submissionPrefix(conversationId: string): string | null {
  const server = getOmnigentServerIdentity();
  const user = getCurrentUserId();
  if (server === null || user === null) return null;
  return `omnigent:skill-submission:v1:${JSON.stringify([server, user, conversationId])}:`;
}

export function readSkillSubmissions(conversationId: string): SkillCommandSubmission[] {
  const prefix = submissionPrefix(conversationId);
  if (prefix === null) return [];
  const submissions: SkillCommandSubmission[] = [];
  for (let i = 0; i < localStorage.length; i++) {
    const key = localStorage.key(i)!;
    if (key.startsWith(prefix))
      submissions.push(submissionSchema.parse(JSON.parse(localStorage.getItem(key)!)));
  }
  return submissions;
}

export function subscribeSkillSubmissions(listener: () => void): () => void {
  window.addEventListener(changeEvent, listener);
  window.addEventListener("storage", listener);
  return () => {
    window.removeEventListener(changeEvent, listener);
    window.removeEventListener("storage", listener);
  };
}

export function saveSkillSubmission(
  conversationId: string,
  event: SkillCommandSubmission["event"],
): string {
  const prefix = submissionPrefix(conversationId);
  if (prefix === null)
    throw new Error("Skill recovery requires a resolved server and user identity.");
  const key = prefix + event.data.stable_id;
  if (localStorage.getItem(key) !== null)
    throw new Error("This invocation already exists. Use Check delivery.");
  localStorage.setItem(
    key,
    JSON.stringify({ event, delivery: null } satisfies SkillCommandSubmission),
  );
  window.dispatchEvent(new Event(changeEvent));
  return key;
}

export function removeSkillSubmission(key: string): void {
  localStorage.removeItem(key);
  admissionPersistenceErrors.delete(key);
  window.dispatchEvent(new Event(changeEvent));
}

export function readSkillAdmissionPersistenceErrors(conversationId: string) {
  const prefix = submissionPrefix(conversationId);
  if (prefix === null) return [];
  return [...admissionPersistenceErrors.entries()]
    .filter(([key]) => key.startsWith(prefix))
    .map(([key, reason]) => ({ invocationId: key.slice(prefix.length), reason }));
}

export function recordSkillAdmission(conversationId: string, delivery: SkillCommandDelivery): void {
  if (delivery.historical) return;
  const prefix = submissionPrefix(conversationId);
  if (prefix === null) return;
  const key = prefix + delivery.invocation_id;
  try {
    const raw = localStorage.getItem(key);
    if (raw === null) return;
    const submission = submissionSchema.parse(JSON.parse(raw));
    if (!submission.delivery || submission.delivery.status === "unknown")
      localStorage.setItem(key, JSON.stringify({ ...submission, delivery }));
    admissionPersistenceErrors.delete(key);
  } catch (error) {
    admissionPersistenceErrors.set(key, error instanceof Error ? error.message : String(error));
  }
  window.dispatchEvent(new Event(changeEvent));
}

export async function checkSkillAdmission(
  conversationId: string,
  invocationId: string,
): Promise<SkillCommandDelivery | null> {
  let olderThan: string | undefined;
  for (;;) {
    // oxlint-disable-next-line no-await-in-loop
    const page = await fetchSessionItemsPage(conversationId, { olderThan });
    for (const item of page.items) {
      if (item.type !== "slash_command") continue;
      const parsed = skillCommandDeliverySchema.safeParse(item.delivery);
      if (!parsed.success || parsed.data.invocation_id !== invocationId) continue;
      if (parsed.data.historical) throw new Error("Historical copies cannot be recovered.");
      recordSkillAdmission(conversationId, parsed.data);
      return parsed.data;
    }
    if (!page.hasMore) return null;
    const next = page.items[0]?.id;
    if (!next || next === olderThan)
      throw new Error("Skill delivery history could not be read completely.");
    olderThan = next;
  }
}

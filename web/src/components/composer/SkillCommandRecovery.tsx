import { useEffect, useState } from "react";
import { SkillAdmission } from "@/components/blocks/SkillAdmission";
import {
  readSkillAdmissionPersistenceErrors,
  readSkillSubmissions,
  subscribeSkillSubmissions,
  type SkillCommandSubmission,
} from "@/lib/skillCommandRecovery";

export function SkillCommandRecovery({ conversationId }: { conversationId: string | null }) {
  const [submissions, setSubmissions] = useState<SkillCommandSubmission[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [persistenceErrors, setPersistenceErrors] = useState<
    ReturnType<typeof readSkillAdmissionPersistenceErrors>
  >([]);
  useEffect(() => {
    const refresh = () => {
      try {
        setSubmissions(conversationId ? readSkillSubmissions(conversationId) : []);
        setError(null);
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      }
      setPersistenceErrors(
        conversationId ? readSkillAdmissionPersistenceErrors(conversationId) : [],
      );
    };
    refresh();
    return subscribeSkillSubmissions(refresh);
  }, [conversationId]);
  const unknown = submissions.filter(
    (submission) => !submission.delivery || submission.delivery.status === "unknown",
  );
  if (!conversationId || (!unknown.length && !error && !persistenceErrors.length)) return null;
  return (
    <details className="mx-auto w-full max-w-3xl px-4 py-2 text-sm" open>
      <summary className="cursor-pointer">Skill recovery</summary>
      {error && <p role="alert">Could not read saved skill submissions. {error}</p>}
      {persistenceErrors.map(({ invocationId, reason }) => (
        <p key={invocationId} role="alert">
          Could not update saved skill delivery for <code>{invocationId}</code> in this browser.{" "}
          {reason} Check delivery reads the server without resending the command.
        </p>
      ))}
      {unknown.map(({ event, delivery }) => (
        <div key={event.data.stable_id} className="mt-2 rounded-md border p-2">
          <div className="break-words">
            /{event.data.name}
            {event.data.arguments ? ` ${event.data.arguments}` : ""}
          </div>
          <SkillAdmission
            conversationId={conversationId}
            invocationId={event.data.stable_id}
            delivery={delivery ?? undefined}
          />
        </div>
      ))}
    </details>
  );
}

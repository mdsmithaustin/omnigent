import { useState } from "react";
import { useScopedConversationId } from "@/components/chat/conversationScope";
import { Button } from "@/components/ui/button";
import { checkSkillAdmission } from "@/lib/skillCommandRecovery";
import { skillAdmissionLabel, type SkillCommandDelivery } from "@/lib/skillCommandDelivery";

export function SkillAdmission({
  invocationId,
  delivery,
  conversationId,
}: {
  invocationId: string;
  delivery?: SkillCommandDelivery;
  conversationId?: string;
}) {
  const scopedConversationId = useScopedConversationId();
  const target = conversationId ?? scopedConversationId;
  const [checked, setChecked] = useState<SkillCommandDelivery | null>(null);
  const [checking, setChecking] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const current = delivery?.historical ? delivery : (checked ?? delivery);

  async function check() {
    if (!target) return;
    setChecking(true);
    setMessage(null);
    try {
      const result = await checkSkillAdmission(target, invocationId);
      setChecked(result);
      setMessage(
        result
          ? "Saved admission checked. Nothing was resent."
          : "No saved admission found. Outcome remains unknown. Nothing was resent.",
      );
    } catch (error) {
      setMessage(error instanceof Error ? error.message : String(error));
    } finally {
      setChecking(false);
    }
  }

  return (
    <div className="space-y-1 py-1 text-xs text-muted-foreground">
      <div className="flex flex-wrap items-center gap-2">
        <span>{current ? skillAdmissionLabel(current) : "Admission unknown"}</span>
        {!current?.historical && target && (
          <Button variant="outline" size="sm" disabled={checking} onClick={() => void check()}>
            {checking ? "Checking admission…" : "Check admission"}
          </Button>
        )}
      </div>
      <details>
        <summary className="cursor-pointer">Invocation details</summary>
        <code className="break-all">{invocationId}</code>
      </details>
      {message && <p role="status">{message}</p>}
    </div>
  );
}

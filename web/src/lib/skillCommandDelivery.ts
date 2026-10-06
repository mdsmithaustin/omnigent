import { z } from "zod";

export const skillCommandDeliverySchema = z.object({
  invocation_id: z.string(),
  fingerprint: z.string(),
  status: z.enum(["unknown", "accepted", "rejected"]),
  context_item_id: z.string().nullable().optional(),
  historical: z.boolean().optional(),
});

export type SkillCommandDelivery = z.infer<typeof skillCommandDeliverySchema>;

export function skillAdmissionLabel(delivery: SkillCommandDelivery): string {
  if (delivery.historical) return "Historical copy";
  switch (delivery.status) {
    case "accepted":
      return "Admitted; completion not confirmed";
    case "rejected":
      return "Rejected before admission";
    case "unknown":
      return "Admission unknown";
  }
}

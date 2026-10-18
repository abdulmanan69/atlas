/**
 * Zod schemas for the inspector forms. They enforce what the API refuses on save (lengths,
 * identifiers, reserved names); whether text is *filled in* is the flow validator's job,
 * so a draft with an empty prompt can still be saved.
 */
import { z } from "zod";

import { LIMITS } from "@/features/voice/flow/types";

const captureField = z.object({
  name: z.string().regex(/^[a-z][a-z0-9_]{0,47}$/, "Lowercase letters, numbers and underscores"),
  type: z.enum(["string", "number", "boolean"]),
  description: z.string().trim().min(1, "Say what to capture").max(300),
});

export const captureFieldsSchema = z
  .array(captureField)
  .max(LIMITS.captures)
  .superRefine((fields, ctx) => {
    const names = fields.map((f) => f.name);
    names.forEach((n, i) => {
      if (n === "opted_out") ctx.addIssue({ code: "custom", path: [i, "name"], message: "Reserved: always captured" });
      else if (names.indexOf(n) !== i) ctx.addIssue({ code: "custom", path: [i, "name"], message: "Duplicate name" });
    });
  });

export const nodeFormSchema = z.object({
  name: z.string().trim().min(1, "Give it a name").max(LIMITS.name, `Keep it under ${LIMITS.name} characters`),
  prompt: z.string().max(LIMITS.prompt),
  greeting: z.string().max(LIMITS.greeting, `Keep the greeting under ${LIMITS.greeting} characters`),
  allow_interrupt: z.boolean(),
  use_global_rules: z.boolean(),
  capture_fields: captureFieldsSchema,
  action_ids: z.array(z.string()).max(20),
  knowledge_ids: z.array(z.string()).max(20),
});

export const pathFormSchema = z.object({
  name: z.string().trim().min(1, "Give the path a short name").max(LIMITS.pathName, `Keep it under ${LIMITS.pathName} characters`),
  when: z.string().max(LIMITS.when, `Keep it under ${LIMITS.when} characters`),
  say_while_moving: z.string().max(LIMITS.sayWhileMoving, `Keep it under ${LIMITS.sayWhileMoving} characters`),
});

export const globalRulesSchema = z.string().max(LIMITS.globalRules, `Keep global rules under ${LIMITS.globalRules} characters`);

export type NodeForm = z.infer<typeof nodeFormSchema>;
export type PathForm = z.infer<typeof pathFormSchema>;

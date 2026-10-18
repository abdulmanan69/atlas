import { z } from "zod";

const optional = (max: number) =>
  z
    .string()
    .trim()
    .max(max)
    .transform((v) => v || null);

export const leadSchema = z.object({
  first_name: z.string().trim().min(1, "Enter a first name").max(120),
  last_name: optional(120),
  phone: optional(64),
  email: z
    .string()
    .trim()
    .transform((v) => v || null)
    .pipe(z.email("Enter a valid email address").nullable()),
  company: optional(200),
  job_title: optional(200),
  source: optional(80),
  tags: z
    .string()
    .transform((v) =>
      v
        .split(",")
        .map((t) => t.trim().toLowerCase())
        .filter(Boolean),
    )
    .pipe(z.array(z.string().regex(/^[a-z0-9][a-z0-9_\-:]{0,63}$/, "Tags use letters, numbers, - _ :")).max(30)),
  owner_user_id: z.string().transform((v) => v || null),
  stage_id: z.string().transform((v) => v || null),
  do_not_call: z.boolean(),
});
export type LeadFormInput = z.input<typeof leadSchema>;
export type LeadFormOutput = z.output<typeof leadSchema>;

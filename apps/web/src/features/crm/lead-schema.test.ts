import { leadSchema } from "@/features/crm/lead-schema";

const base = {
  first_name: "Ayesha",
  last_name: "",
  phone: "",
  email: "",
  company: "",
  job_title: "",
  source: "",
  tags: "",
  owner_user_id: "",
  stage_id: "",
  do_not_call: false,
};

describe("leadSchema", () => {
  it("turns empty optional fields into null and parses tags", () => {
    const out = leadSchema.parse({ ...base, tags: " VIP, q4 ,, " });
    expect(out).toMatchObject({ last_name: null, email: null, phone: null, owner_user_id: null, stage_id: null });
    expect(out.tags).toEqual(["vip", "q4"]);
  });

  it("requires a first name", () => {
    const r = leadSchema.safeParse({ ...base, first_name: "   " });
    expect(r.success).toBe(false);
  });

  it("validates email and tag format", () => {
    expect(leadSchema.safeParse({ ...base, email: "nope" }).success).toBe(false);
    expect(leadSchema.safeParse({ ...base, tags: "bad tag!" }).success).toBe(false);
    expect(leadSchema.safeParse({ ...base, email: "a@example.com", tags: "ok-tag" }).success).toBe(true);
  });
});

import { captureFieldsSchema, nodeFormSchema, pathFormSchema } from "@/features/voice/flow/inspector-schema";

const node = { name: "Qualify", prompt: "", greeting: "", allow_interrupt: true, use_global_rules: true, capture_fields: [], action_ids: [], knowledge_ids: [] };

describe("inspector schemas", () => {
  it("accepts a draft node with an empty prompt (the validator reports it, save allows it)", () => {
    expect(nodeFormSchema.safeParse(node).success).toBe(true);
  });

  it("requires a name and enforces lengths", () => {
    expect(nodeFormSchema.safeParse({ ...node, name: "  " }).error?.issues[0]?.message).toBe("Give it a name");
    expect(nodeFormSchema.safeParse({ ...node, name: "x".repeat(61) }).success).toBe(false);
    expect(nodeFormSchema.safeParse({ ...node, greeting: "x".repeat(601) }).success).toBe(false);
    expect(pathFormSchema.safeParse({ name: "", when: "", say_while_moving: "" }).success).toBe(false);
    expect(pathFormSchema.safeParse({ name: "Go", when: "x".repeat(301), say_while_moving: "" }).success).toBe(false);
    expect(pathFormSchema.safeParse({ name: "Go", when: "", say_while_moving: "" }).success).toBe(true);
  });

  it("checks capture fields: identifiers, duplicates and the reserved name", () => {
    const ok = captureFieldsSchema.safeParse([{ name: "budget", type: "string", description: "Budget" }]);
    expect(ok.success).toBe(true);
    const bad = captureFieldsSchema.safeParse([
      { name: "Budget", type: "string", description: "x" },
      { name: "opted_out", type: "boolean", description: "x" },
      { name: "a", type: "number", description: "x" },
      { name: "a", type: "number", description: "" },
    ]);
    const messages = bad.error?.issues.map((i) => `${i.path.join(".")}: ${i.message}`) ?? [];
    expect(messages).toContain("0.name: Lowercase letters, numbers and underscores");
    expect(messages).toContain("1.name: Reserved: always captured");
    expect(messages).toContain("3.name: Duplicate name");
    expect(messages).toContain("3.description: Say what to capture");
  });
});

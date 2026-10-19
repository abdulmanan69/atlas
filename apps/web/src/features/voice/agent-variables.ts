/**
 * The variables prompts can use, shared by every variable picker in the agent editor
 * (script fields, flow inspector, side card): the lead fields plus the agent's own.
 */
import { createContext, useContext } from "react";

import { VARIABLES, variableNameProblem } from "@/features/voice/agent-form";

export interface AvailableVariable {
  name: string;
  source: "lead" | "agent";
  /** Agent variables only: the value used unless the lead has a field of the same name. */
  value?: string;
}

const LEAD: AvailableVariable[] = VARIABLES.map((name) => ({ name, source: "lead" }));

/** Lead fields first, then the agent's valid, distinct variables in the order defined. */
export function availableVariables(custom: { name: string; value: string }[] | undefined): AvailableVariable[] {
  const seen = new Set(VARIABLES);
  const own: AvailableVariable[] = [];
  for (const v of custom ?? []) {
    if (!v.name || seen.has(v.name) || variableNameProblem(v.name)) continue;
    seen.add(v.name);
    own.push({ name: v.name, source: "agent", value: v.value });
  }
  return [...LEAD, ...own];
}

const AgentVariablesContext = createContext<AvailableVariable[]>(LEAD);

export const AgentVariablesProvider = AgentVariablesContext.Provider;

export function useAgentVariables(): AvailableVariable[] {
  return useContext(AgentVariablesContext);
}

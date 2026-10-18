/**
 * Session state. The access token lives only in memory; the refresh token is an
 * HttpOnly cookie the browser manages. Only the selected organization id is persisted.
 */
import { create } from "zustand";

import type { components } from "@/types/api";

type TokenResponse = components["schemas"]["TokenResponse"];
type Membership = components["schemas"]["MembershipOut"];
type User = components["schemas"]["UserOut"];
export type Role = Membership["role"];

const ORG_KEY = "atlas-org";

function readOrg(): string | null {
  try {
    return localStorage.getItem(ORG_KEY);
  } catch {
    return null;
  }
}

function writeOrg(id: string | null): void {
  try {
    if (id) localStorage.setItem(ORG_KEY, id);
    else localStorage.removeItem(ORG_KEY);
  } catch {
    /* storage unavailable (private mode) — selection simply isn't remembered */
  }
}

interface SessionState {
  status: "unknown" | "authenticated" | "anonymous";
  accessToken: string | null;
  user: User | null;
  memberships: Membership[];
  orgId: string | null;
  signIn: (tokens: TokenResponse) => void;
  setMemberships: (memberships: Membership[]) => void;
  selectOrg: (orgId: string) => void;
  signOut: () => void;
}

function pickOrg(memberships: Membership[], preferred: string | null): string | null {
  if (preferred && memberships.some((m) => m.organization.id === preferred)) return preferred;
  return memberships[0]?.organization.id ?? null;
}

export const session = create<SessionState>()((set, get) => ({
  status: "unknown",
  accessToken: null,
  user: null,
  memberships: [],
  orgId: readOrg(),
  signIn: (tokens) => {
    const orgId = pickOrg(tokens.memberships, get().orgId);
    writeOrg(orgId);
    set({
      status: "authenticated",
      accessToken: tokens.access_token,
      user: tokens.user,
      memberships: tokens.memberships,
      orgId,
    });
  },
  setMemberships: (memberships) => {
    const orgId = pickOrg(memberships, get().orgId);
    writeOrg(orgId);
    set({ memberships, orgId });
  },
  selectOrg: (orgId) => {
    writeOrg(orgId);
    set({ orgId });
  },
  signOut: () => set({ status: "anonymous", accessToken: null, user: null, memberships: [] }),
}));

export const useSession = session;

/** Membership for the currently selected organization. */
export function useCurrentMembership(): Membership | null {
  return useSession((s) => s.memberships.find((m) => m.organization.id === s.orgId) ?? null);
}

const RANK: Record<Role, number> = { viewer: 0, agent: 1, manager: 2, admin: 3, owner: 4 };

/** UI hint only; the API enforces every permission. */
export function useHasRole(min: Role): boolean {
  const role = useCurrentMembership()?.role;
  return role !== undefined && RANK[role] >= RANK[min];
}

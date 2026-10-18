import { createBrowserRouter } from "react-router";

import { AppShell } from "@/components/app-shell";
import { AnonymousOnly, RequireAuth } from "@/components/route-guards";

const page = (loader: () => Promise<{ default: React.ComponentType }>) => async () => ({
  Component: (await loader()).default,
});

export const router = createBrowserRouter([
  {
    element: <AnonymousOnly />,
    children: [
      { path: "/sign-in", lazy: page(() => import("@/features/auth/sign-in-page")) },
      { path: "/sign-up", lazy: page(() => import("@/features/auth/sign-up-page")) },
    ],
  },
  { path: "/invite", lazy: page(() => import("@/features/auth/accept-invite-page")) },
  {
    element: <RequireAuth />,
    children: [
      {
        element: <AppShell />,
        children: [
          { index: true, lazy: page(() => import("@/features/dashboard/dashboard-page")) },
          { path: "leads", lazy: page(() => import("@/features/crm/leads-page")) },
          { path: "leads/:leadId", lazy: page(() => import("@/features/crm/lead-detail-page")) },
          { path: "tasks", lazy: page(() => import("@/features/tasks/tasks-page")) },
          { path: "team", lazy: page(() => import("@/features/settings/team-page")) },
          { path: "settings", lazy: page(() => import("@/features/settings/settings-page")) },
          { path: "audit", lazy: page(() => import("@/features/audit/audit-page")) },
          { path: "agents", lazy: page(() => import("@/features/voice/agents-page")) },
          { path: "agents/:agentId", lazy: page(() => import("@/features/voice/agent-editor-page")) },
          { path: "ai-providers", lazy: page(() => import("@/features/voice/ai-providers-page")) },
          { path: "calls", lazy: page(() => import("@/features/calls/calls-page")) },
          { path: "calls/:callId", lazy: page(() => import("@/features/calls/call-detail-page")) },
          { path: "campaigns", lazy: page(() => import("@/features/campaigns/campaigns-page")) },
          { path: "campaigns/:campaignId", lazy: page(() => import("@/features/campaigns/campaign-detail-page")) },
          { path: "reports", lazy: page(() => import("@/features/reports/reports-page")) },
          { path: "phone-numbers", lazy: page(() => import("@/features/telephony/phone-numbers-page")) },
          { path: "actions", lazy: page(() => import("@/features/actions/actions-page")) },
          { path: "knowledge", lazy: page(() => import("@/features/knowledge/knowledge-page")) },
          { path: "developer", lazy: page(() => import("@/features/developer/developer-page")) },
          { path: "*", lazy: page(() => import("@/features/not-found-page")) },
        ],
      },
    ],
  },
]);

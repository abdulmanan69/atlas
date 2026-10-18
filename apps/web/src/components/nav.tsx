import {
  BarChart3,
  BookOpen,
  Bot,
  CheckSquare,
  Cpu,
  LayoutDashboard,
  Megaphone,
  Phone,
  PhoneCall,
  ScrollText,
  Settings,
  Users,
  UsersRound,
  Webhook,
  Zap,
  type LucideIcon,
} from "lucide-react";

import type { Role } from "@/lib/session";

export interface NavItem {
  to: string;
  label: string;
  icon: LucideIcon;
  group: "Pipeline" | "Voice AI" | "Organization";
  minRole?: Role;
}

export const NAV: NavItem[] = [
  { to: "/", label: "Overview", icon: LayoutDashboard, group: "Pipeline" },
  { to: "/leads", label: "Leads", icon: Users, group: "Pipeline" },
  { to: "/tasks", label: "Tasks", icon: CheckSquare, group: "Pipeline" },
  { to: "/campaigns", label: "Campaigns", icon: Megaphone, group: "Pipeline" },
  { to: "/calls", label: "Calls", icon: PhoneCall, group: "Pipeline" },
  { to: "/reports", label: "Reports", icon: BarChart3, group: "Pipeline" },
  { to: "/agents", label: "Agents", icon: Bot, group: "Voice AI" },
  { to: "/phone-numbers", label: "Phone numbers", icon: Phone, group: "Voice AI", minRole: "manager" },
  { to: "/actions", label: "Actions", icon: Zap, group: "Voice AI", minRole: "manager" },
  { to: "/knowledge", label: "Knowledge", icon: BookOpen, group: "Voice AI", minRole: "manager" },
  { to: "/ai-providers", label: "AI providers", icon: Cpu, group: "Voice AI", minRole: "manager" },
  { to: "/team", label: "Team", icon: UsersRound, group: "Organization", minRole: "agent" },
  { to: "/settings", label: "Settings", icon: Settings, group: "Organization" },
  { to: "/developer", label: "API & webhooks", icon: Webhook, group: "Organization", minRole: "admin" },
  { to: "/audit", label: "Audit log", icon: ScrollText, group: "Organization", minRole: "admin" },
];

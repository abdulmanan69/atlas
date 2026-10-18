import { Tabs as TabsPrimitive } from "radix-ui";
import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

export const Tabs = TabsPrimitive.Root;
export const TabsContent = ({ value, children, className }: { value: string; children: ReactNode; className?: string }) => (
  <TabsPrimitive.Content value={value} className={cn("focus-visible:outline-none", className)}>
    {children}
  </TabsPrimitive.Content>
);

export function TabsList({ tabs, className }: { tabs: { value: string; label: ReactNode; badge?: ReactNode }[]; className?: string }) {
  return (
    <TabsPrimitive.List className={cn("flex gap-1 border-b border-line", className)}>
      {tabs.map((t) => (
        <TabsPrimitive.Trigger
          key={t.value}
          value={t.value}
          className={cn(
            "relative -mb-px inline-flex h-9 items-center gap-1.5 border-b-2 border-transparent px-3 text-[13px] font-medium text-muted transition-colors hover:text-ink",
            "data-[state=active]:border-accent data-[state=active]:text-ink",
          )}
        >
          {t.label}
          {t.badge}
        </TabsPrimitive.Trigger>
      ))}
    </TabsPrimitive.List>
  );
}

import { useSearchParams } from "react-router";

import { PageHeader } from "@/components/common";
import { Tabs, TabsContent, TabsList } from "@/components/ui/tabs";
import { ApiKeysTab } from "@/features/developer/api-keys-tab";
import { DocsTab } from "@/features/developer/docs-tab";
import { WebhooksTab } from "@/features/developer/webhooks-tab";

const TABS = [
  { value: "keys", label: "API keys" },
  { value: "webhooks", label: "Webhooks" },
  { value: "docs", label: "Docs" },
] as const;
type Tab = (typeof TABS)[number]["value"];

export default function DeveloperPage() {
  const [params, setParams] = useSearchParams();
  const raw = params.get("tab");
  const tab: Tab = TABS.some((t) => t.value === raw) ? (raw as Tab) : "keys";

  return (
    <>
      <PageHeader title="API & webhooks" description="Keys for your systems to call ATLAS, and signed events ATLAS sends to them." />
      <Tabs
        value={tab}
        onValueChange={(v) => setParams((p) => {
          const next = new URLSearchParams(p);
          if (v === "keys") next.delete("tab");
          else next.set("tab", v);
          return next;
        }, { replace: true })}
      >
        <TabsList tabs={[...TABS]} className="mb-5" />
        <TabsContent value="keys">
          <ApiKeysTab />
        </TabsContent>
        <TabsContent value="webhooks">
          <WebhooksTab />
        </TabsContent>
        <TabsContent value="docs">
          <DocsTab />
        </TabsContent>
      </Tabs>
    </>
  );
}

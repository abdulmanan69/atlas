import { MutationCache, QueryCache, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useEffect } from "react";
import { RouterProvider } from "react-router";
import { Toaster } from "sonner";

import { router } from "@/app/router";
import { ApiError, refreshSession } from "@/lib/api";
import { session } from "@/lib/session";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      refetchOnWindowFocus: false,
      // Don't retry what will fail again: auth, permission, validation, not-found.
      retry: (count, err) => !(err instanceof ApiError && err.status >= 400 && err.status < 500) && count < 2,
    },
  },
  queryCache: new QueryCache(),
  mutationCache: new MutationCache(),
});

// Signing out (or switching tenant) must never show another tenant's cached data.
session.subscribe((state, prev) => {
  if (state.orgId !== prev.orgId || (prev.status === "authenticated" && state.status !== "authenticated")) {
    queryClient.clear();
  }
});

export function Providers() {
  useEffect(() => {
    if (session.getState().status === "unknown") {
      void refreshSession().then((ok) => {
        if (!ok) session.getState().signOut();
      });
    }
  }, []);

  return (
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
      <Toaster
        position="bottom-right"
        toastOptions={{
          classNames: {
            toast: "!bg-surface !text-ink !border-line !shadow-pop !font-sans !text-[13px]",
            description: "!text-muted",
          },
        }}
      />
    </QueryClientProvider>
  );
}

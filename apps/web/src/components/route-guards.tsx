import { Navigate, Outlet, useLocation } from "react-router";

import { FullPageSpinner } from "@/components/common";
import { useSession } from "@/lib/session";

export function RequireAuth() {
  const status = useSession((s) => s.status);
  const location = useLocation();
  if (status === "unknown") return <FullPageSpinner label="Restoring your session" />;
  if (status === "anonymous") {
    const next = encodeURIComponent(location.pathname + location.search);
    return <Navigate to={`/sign-in?next=${next}`} replace />;
  }
  return <Outlet />;
}

export function AnonymousOnly() {
  const status = useSession((s) => s.status);
  if (status === "unknown") return <FullPageSpinner label="Loading" />;
  if (status === "authenticated") return <Navigate to="/" replace />;
  return <Outlet />;
}

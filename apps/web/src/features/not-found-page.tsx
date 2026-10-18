import { Link } from "react-router";

import { EmptyState } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/primitives";

export default function NotFoundPage() {
  return (
    <Card>
      <EmptyState
        title="This page doesn't exist"
        description="The link may be out of date, or the record belongs to another organization."
        action={
          <Button asChild variant="primary">
            <Link to="/">Go to overview</Link>
          </Button>
        }
      />
    </Card>
  );
}

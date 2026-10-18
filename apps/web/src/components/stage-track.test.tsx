import { render, screen } from "@testing-library/react";

import { StageTrack } from "@/components/stage-track";

const stages = [
  { id: "s-new", key: "new", name: "New", position: 0, category: "open" as const },
  { id: "s-contacted", key: "contacted", name: "Contacted", position: 1, category: "open" as const },
  { id: "s-qualified", key: "qualified", name: "Qualified", position: 2, category: "open" as const },
  { id: "s-won", key: "converted", name: "Converted", position: 3, category: "won" as const },
  { id: "s-lost", key: "unreachable", name: "Unreachable", position: 5, category: "lost" as const },
];

describe("StageTrack", () => {
  it("describes progress through the open stages", () => {
    render(<StageTrack stages={stages} currentStageId="s-contacted" />);
    expect(screen.getByRole("img")).toHaveAccessibleName("Contacted — stage 2 of 3");
    expect(screen.getByText("Contacted")).toBeInTheDocument();
  });

  it("marks a won stage as complete", () => {
    render(<StageTrack stages={stages} currentStageId="s-won" />);
    expect(screen.getByRole("img")).toHaveAccessibleName("Converted — pipeline complete");
  });

  it("reports where a lost lead left the pipeline", () => {
    render(<StageTrack stages={stages} currentStageId="s-lost" showLabel={false} />);
    expect(screen.getByRole("img")).toHaveAccessibleName("Unreachable — left the pipeline after 3 of 3 stages");
    expect(screen.queryByText("Unreachable")).not.toBeInTheDocument();
  });

  it("renders nothing for an unknown stage", () => {
    const { container } = render(<StageTrack stages={stages} currentStageId="missing" />);
    expect(container).toBeEmptyDOMElement();
  });
});

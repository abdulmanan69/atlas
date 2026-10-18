import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";

import { CallingHoursEditor } from "@/features/campaigns/calling-hours-editor";
import { DEFAULT_HOURS, type HoursDraft } from "@/features/campaigns/campaigns-model";

function Harness({ initial = DEFAULT_HOURS, onChange }: { initial?: HoursDraft; onChange: (v: HoursDraft) => void }) {
  const [value, setValue] = useState(initial);
  return (
    <CallingHoursEditor
      value={value}
      onChange={(v) => {
        setValue(v);
        onChange(v);
      }}
    />
  );
}

describe("CallingHoursEditor", () => {
  it("shows one row per weekday with weekends off by default", () => {
    render(<Harness onChange={() => undefined} />);
    expect(screen.getByRole("switch", { name: "Calls on Monday" })).toBeChecked();
    expect(screen.getByRole("switch", { name: "Calls on Saturday" })).not.toBeChecked();
    expect(screen.getByRole("combobox", { name: "Monday from" })).toHaveTextContent("09:00");
    expect(screen.getByRole("combobox", { name: "Monday to" })).toHaveTextContent("18:00");
    expect(screen.getByRole("combobox", { name: "Saturday from" })).toBeDisabled();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("toggles a day and enables its window", async () => {
    const onChange = vi.fn();
    render(<Harness onChange={onChange} />);
    await userEvent.click(screen.getByRole("switch", { name: "Calls on Saturday" }));
    expect(onChange).toHaveBeenLastCalledWith(expect.objectContaining({ sat: { enabled: true, from: "09:00", to: "18:00" } }));
    expect(screen.getByRole("combobox", { name: "Saturday from" })).toBeEnabled();
  });

  it("announces when no day is enabled or a window is inverted", async () => {
    const allOff = Object.fromEntries(Object.entries(DEFAULT_HOURS).map(([d, w]) => [d, { ...w, enabled: false }])) as HoursDraft;
    const { unmount } = render(<Harness initial={allOff} onChange={() => undefined} />);
    expect(screen.getByRole("alert")).toHaveTextContent("Pick at least one day");
    unmount();

    render(<Harness initial={{ ...DEFAULT_HOURS, tue: { enabled: true, from: "17:00", to: "09:00" } }} onChange={() => undefined} />);
    expect(screen.getByRole("alert")).toHaveTextContent("Tuesday: the window must start before it ends.");
  });
});

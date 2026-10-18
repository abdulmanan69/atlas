import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { DateTimePicker, presetDate, withTime } from "@/components/ui/date-time-picker";

describe("presetDate", () => {
  const now = new Date(2026, 0, 10, 15, 42);
  it("lands on 09:00 local of the right day", () => {
    expect(presetDate("today", now)).toEqual(new Date(2026, 0, 10, 9, 0));
    expect(presetDate("tomorrow", now)).toEqual(new Date(2026, 0, 11, 9, 0));
    expect(presetDate("next-week", now)).toEqual(new Date(2026, 0, 17, 9, 0));
  });
  it("keeps the day when the time changes", () => {
    expect(withTime(new Date(2026, 5, 3, 1, 2), 23, 59)).toEqual(new Date(2026, 5, 3, 23, 59));
  });
});

describe("DateTimePicker", () => {
  it("applies a preset and closes", async () => {
    const onChange = vi.fn();
    render(<DateTimePicker aria-label="Follow-up" value={null} onChange={onChange} />);
    const trigger = screen.getByRole("button", { name: "Follow-up" });
    expect(trigger).toHaveTextContent("Pick a date and time");
    await userEvent.click(trigger);
    await userEvent.click(await screen.findByRole("button", { name: "Tomorrow 9:00" }));
    expect(onChange).toHaveBeenCalledWith(presetDate("tomorrow").toISOString());
    await waitFor(() => expect(screen.queryByRole("button", { name: "Tomorrow 9:00" })).not.toBeInTheDocument());
  });

  it("shows the value in the user's locale and clears", async () => {
    const onChange = vi.fn();
    const value = new Date(2026, 2, 5, 14, 30).toISOString();
    render(<DateTimePicker aria-label="Follow-up" value={value} onChange={onChange} clearable />);
    expect(screen.getByRole("button", { name: "Follow-up" })).toHaveTextContent(/2026/);
    await userEvent.click(screen.getByRole("button", { name: "Clear date" }));
    expect(onChange).toHaveBeenCalledWith(null);
  });

  it("changes the day from the calendar without losing the time", async () => {
    const onChange = vi.fn();
    const value = new Date(2026, 2, 5, 14, 30).toISOString();
    render(<DateTimePicker aria-label="Follow-up" value={value} onChange={onChange} presets={false} />);
    await userEvent.click(screen.getByRole("button", { name: "Follow-up" }));
    const grid = await screen.findByRole("grid");
    const day = Array.from(grid.querySelectorAll("button")).find((b) => b.textContent === "12" && !b.closest("[data-outside]"));
    expect(day).toBeDefined();
    await userEvent.click(day!);
    expect(onChange).toHaveBeenCalledWith(new Date(2026, 2, 12, 14, 30).toISOString());
  });
});

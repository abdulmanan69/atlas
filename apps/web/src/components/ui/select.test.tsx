import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { Select, SelectItem } from "@/components/ui/select";

function stages(onChange: (v: string) => void, value: string, allowEmpty = false) {
  return (
    <Select aria-label="Stage" value={value} onValueChange={onChange} placeholder="All stages" allowEmpty={allowEmpty} emptyLabel="All stages">
      <SelectItem value="new">New</SelectItem>
      <SelectItem value="contacted">Contacted</SelectItem>
    </Select>
  );
}

describe("Select", () => {
  it("opens with the keyboard and selects an item", async () => {
    const onChange = vi.fn();
    render(stages(onChange, ""));
    const trigger = screen.getByRole("combobox", { name: "Stage" });
    expect(trigger).toHaveTextContent("All stages");
    trigger.focus();
    await userEvent.keyboard("{Enter}");
    expect(await screen.findByRole("listbox")).toBeInTheDocument();
    await userEvent.keyboard("{ArrowDown}{Enter}");
    expect(onChange).toHaveBeenCalledWith("contacted");
  });

  it("maps the empty option to an empty string", async () => {
    const onChange = vi.fn();
    render(stages(onChange, "new", true));
    const trigger = screen.getByRole("combobox", { name: "Stage" });
    expect(trigger).toHaveTextContent("New");
    trigger.focus();
    await userEvent.keyboard("{Enter}");
    await screen.findByRole("listbox");
    await userEvent.keyboard("{ArrowUp}{Enter}");
    expect(onChange).toHaveBeenCalledWith("");
  });

  it("shows the empty label when nothing is chosen", () => {
    render(stages(vi.fn(), "", true));
    expect(screen.getByRole("combobox", { name: "Stage" })).toHaveTextContent("All stages");
  });
});

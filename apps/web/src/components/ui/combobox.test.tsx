import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { Combobox } from "@/components/ui/combobox";

const options = [
  { value: "u1", label: "Ayesha Khan", description: "ayesha@example.com" },
  { value: "u2", label: "Bilal Ahmed", description: "bilal@example.com" },
];

describe("Combobox", () => {
  it("filters by search and selects an option", async () => {
    const onChange = vi.fn();
    render(<Combobox aria-label="Owner" value={null} onValueChange={onChange} options={options} placeholder="Any owner" />);
    const trigger = screen.getByRole("combobox", { name: "Owner" });
    expect(trigger).toHaveTextContent("Any owner");
    await userEvent.click(trigger);
    const input = await screen.findByPlaceholderText("Search…");
    await userEvent.type(input, "bil");
    expect(await screen.findByText("Bilal Ahmed")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText("Ayesha Khan")).not.toBeInTheDocument());
    await userEvent.click(screen.getByText("Bilal Ahmed"));
    expect(onChange).toHaveBeenCalledWith("u2");
    await waitFor(() => expect(screen.queryByPlaceholderText("Search…")).not.toBeInTheDocument());
  });

  it("shows the selected label and clears", async () => {
    const onChange = vi.fn();
    render(<Combobox aria-label="Owner" value="u1" onValueChange={onChange} options={options} allowClear />);
    expect(screen.getByRole("combobox", { name: "Owner" })).toHaveTextContent("Ayesha Khan");
    await userEvent.click(screen.getByRole("button", { name: "Clear" }));
    expect(onChange).toHaveBeenCalledWith(null);
  });

  it("falls back to valueLabel for ids outside the list", () => {
    render(<Combobox aria-label="Voice" value="v-9" valueLabel="v-9" onValueChange={vi.fn()} options={[]} />);
    expect(screen.getByRole("combobox", { name: "Voice" })).toHaveTextContent("v-9");
  });
});

import React from "react";
import { render } from "@testing-library/react-native";
import { LoginStage } from "./LoginStage";

describe("LoginStage", () => {
  it("draws the lattice without exposing it to assistive technology", () => {
    const view = render(
      <LoginStage server email={false} password={false} working={false} />,
    );
    view.rerender(<LoginStage server email password working />);
    const stage = view.UNSAFE_root.findAll(
      (node) => node.props.importantForAccessibility === "no-hide-descendants",
    );
    expect(stage.length).toBeGreaterThan(0);
    expect(stage[0].props.accessibilityElementsHidden).toBe(true);
  });
});

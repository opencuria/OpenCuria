import { controlLayout, hugsContent } from "./controlLayout";

it("sizes unstretched controls without a width to their label", () => {
  expect(hugsContent(controlLayout({ alignSelf: "center" }))).toBe(true);
  expect(hugsContent(controlLayout([{ alignSelf: "flex-start" }]))).toBe(true);
});

it("keeps controls that get their width from layout full width", () => {
  expect(hugsContent(controlLayout(undefined))).toBe(false);
  expect(hugsContent(controlLayout({ alignSelf: "stretch" }))).toBe(false);
  expect(hugsContent(controlLayout({ alignSelf: "center", width: 200 }))).toBe(
    false,
  );
  expect(hugsContent(controlLayout({ alignSelf: "center", flex: 1 }))).toBe(
    false,
  );
  expect(
    hugsContent(controlLayout({ alignSelf: "flex-end", flexGrow: 1 })),
  ).toBe(false);
});

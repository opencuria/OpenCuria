import { afterEach, describe, expect, it } from "vitest";
import { armAnchor, claimAnchor, disarmAnchor } from "./promptFlightAnchor";

const anchor = (report: (y: number | null) => void) => report(640);

describe("prompt flight anchor", () => {
  afterEach(() => disarmAnchor());

  it("hands an armed anchor to the first row that claims it", () => {
    armAnchor(anchor, 1_000);
    expect(claimAnchor("local-user-1", 1_100)).toBe(anchor);
    // Another row gets nothing: one prompt is sent at a time.
    expect(claimAnchor("local-user-2", 1_100)).toBeNull();
  });

  it("serves a strict-mode replay of the mount, never a later remount", () => {
    armAnchor(anchor, 1_000);
    expect(claimAnchor("local-user-3", 1_000)).toBe(anchor);
    expect(claimAnchor("local-user-3", 1_500)).toBe(anchor);
    expect(claimAnchor("local-user-3", 2_500)).toBeNull();
  });

  it("lets an anchor nobody claimed expire", () => {
    armAnchor(anchor, 1_000);
    expect(claimAnchor("local-user-4", 4_500)).toBeNull();
    armAnchor(anchor, 5_000);
    disarmAnchor();
    expect(claimAnchor("local-user-5", 5_000)).toBeNull();
  });
});

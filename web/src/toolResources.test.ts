import { describe, expect, it } from "vitest";
import { normalizeToolResources, visibleToolResources } from "./toolResources";

describe("normalizeToolResources", () => {
  it("accepts validated host-relative download resources", () => {
    expect(normalizeToolResources([
      {
        kind: "download",
        name: "report.md",
        href: "/downloads/download_123",
        mimeType: "text/markdown",
        expiresAt: "2026-07-31T00:00:00+00:00",
      },
    ])).toEqual([
      {
        kind: "download",
        name: "report.md",
        href: "/downloads/download_123",
        mimeType: "text/markdown",
        expiresAt: "2026-07-31T00:00:00+00:00",
      },
    ]);
  });

  it.each([
    "https://example.test/downloads/download_123",
    "//example.test/downloads/download_123",
    "/api/v1/downloads/download_123",
    "/downloads/",
    "/downloads/download.123",
  ])("rejects unsafe or unsupported href %s", (href) => {
    expect(normalizeToolResources([
      { kind: "download", name: "report.md", href },
    ])).toEqual([]);
  });

  it("ignores unknown resource kinds", () => {
    expect(normalizeToolResources([
      {
        kind: "preview",
        name: "report.md",
        href: "/downloads/download_123",
      },
    ])).toEqual([]);
  });

  it("returns an empty array for non-array input", () => {
    expect(normalizeToolResources({ kind: "download" })).toEqual([]);
    expect(normalizeToolResources(null)).toEqual([]);
  });

  it("hides download resources for failed tools", () => {
    const resources = normalizeToolResources([
      {
        kind: "download",
        name: "report.md",
        href: "/downloads/download_123",
      },
    ]);

    expect(visibleToolResources(resources, "error")).toEqual([]);
    expect(visibleToolResources(resources, "success")).toEqual(resources);
    expect(visibleToolResources(resources, "running")).toEqual(resources);
  });
});

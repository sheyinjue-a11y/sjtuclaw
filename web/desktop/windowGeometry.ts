export type Point = { x: number; y: number };
export type Size = { width: number; height: number };
export type WorkArea = Point & Size;

export function clampWindowPosition(point: Point, size: Size, area: WorkArea): Point {
  return {
    x: Math.min(Math.max(point.x, area.x), area.x + area.width - size.width),
    y: Math.min(Math.max(point.y, area.y), area.y + area.height - size.height),
  };
}

export function toIntegerPoint(x: unknown, y: unknown): Point | null {
  if (typeof x !== "number" || !Number.isFinite(x) || typeof y !== "number" || !Number.isFinite(y)) {
    return null;
  }
  return { x: Math.round(x), y: Math.round(y) };
}

export function chatSideForBounds(
  bounds: Pick<WorkArea, "x" | "width">,
  area: WorkArea,
): "left" | "right" {
  return bounds.x <= area.x + 24 ? "right" : "left";
}

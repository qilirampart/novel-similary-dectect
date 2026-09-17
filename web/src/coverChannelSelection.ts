function uniqueIds(ids: number[]): number[] {
  return Array.from(new Set(ids.filter((id) => Number.isInteger(id) && id > 0)));
}

export function selectAllFiltered(ids: number[]): Set<number> {
  return new Set(uniqueIds(ids));
}

export function invertFilteredSelection(ids: number[], selected: Set<number>): Set<number> {
  return new Set(uniqueIds(ids).filter((id) => !selected.has(id)));
}

export function selectFirstFiltered(ids: number[], count: number): Set<number> {
  const safeCount = Math.max(0, Math.floor(Number(count) || 0));
  return new Set(uniqueIds(ids).slice(0, safeCount));
}

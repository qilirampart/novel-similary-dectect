import type { ReactNode } from "react";

type TextRange = [number, number];

type NormalizedSource = {
  normalized: string;
  sourceIndexMap: number[];
};

export type EvidenceHighlightRanges = {
  queryRanges: TextRange[];
  candidateRanges: TextRange[];
};

const MIN_MULTI_HIGHLIGHT_CHARS = 5;
const MAX_MULTI_HIGHLIGHT_BLOCKS = 24;

function normalizeHighlightText(value: string): string {
  return value
    .toLowerCase()
    .replace(/[\s\r\n\t]+/g, "")
    .replace(/[^0-9a-z\u4e00-\u9fff]+/g, "");
}

function buildNormalizedSource(text: string): NormalizedSource {
  const source = String(text ?? "");
  const normalizedChars: string[] = [];
  const sourceIndexMap: number[] = [];

  for (let index = 0; index < source.length; index += 1) {
    const normalizedChar = normalizeHighlightText(source[index]);
    if (!normalizedChar) continue;
    for (const char of normalizedChar) {
      normalizedChars.push(char);
      sourceIndexMap.push(index);
    }
  }

  return {
    normalized: normalizedChars.join(""),
    sourceIndexMap
  };
}

function findLooseMatchRange(text: string, matchedSubstring: string): TextRange | null {
  const source = String(text ?? "");
  const needle = String(matchedSubstring ?? "");
  if (!source.trim() || !needle.trim()) return null;

  const normalizedNeedle = normalizeHighlightText(needle);
  if (!normalizedNeedle) return null;

  const exactIndex = source.indexOf(needle);
  if (exactIndex >= 0) {
    return [exactIndex, exactIndex + needle.length];
  }

  const normalizedSource = buildNormalizedSource(source);
  const normalizedIndex = normalizedSource.normalized.indexOf(normalizedNeedle);
  if (normalizedIndex < 0) return null;

  const start = normalizedSource.sourceIndexMap[normalizedIndex];
  const endSourceIndex = normalizedSource.sourceIndexMap[normalizedIndex + normalizedNeedle.length - 1];
  if (start === undefined || endSourceIndex === undefined) return null;
  return [start, endSourceIndex + 1];
}

function mapNormalizedRangeToSource(source: NormalizedSource, start: number, end: number): TextRange | null {
  if (start < 0 || end <= start) return null;
  const sourceStart = source.sourceIndexMap[start];
  const sourceEndIndex = source.sourceIndexMap[end - 1];
  if (sourceStart === undefined || sourceEndIndex === undefined) return null;
  return [sourceStart, sourceEndIndex + 1];
}

function mergeRanges(ranges: TextRange[]): TextRange[] {
  if (ranges.length <= 1) return ranges;

  const sorted = [...ranges].sort((left, right) => left[0] - right[0] || left[1] - right[1]);
  const merged: TextRange[] = [sorted[0]];

  for (let index = 1; index < sorted.length; index += 1) {
    const current = sorted[index];
    const previous = merged[merged.length - 1];
    if (current[0] <= previous[1]) {
      previous[1] = Math.max(previous[1], current[1]);
      continue;
    }
    merged.push([current[0], current[1]]);
  }

  return merged;
}

function findLongestCommonBlock(
  left: string,
  right: string,
  leftStart: number,
  leftEnd: number,
  rightStart: number,
  rightEnd: number
): { leftStart: number; rightStart: number; length: number } | null {
  const leftLength = leftEnd - leftStart;
  const rightLength = rightEnd - rightStart;
  if (leftLength <= 0 || rightLength <= 0) return null;

  const previous = new Uint32Array(rightLength + 1);
  const current = new Uint32Array(rightLength + 1);
  let bestLength = 0;
  let bestLeftEnd = 0;
  let bestRightEnd = 0;

  for (let leftIndex = 0; leftIndex < leftLength; leftIndex += 1) {
    current.fill(0);
    const leftChar = left[leftStart + leftIndex];
    for (let rightIndex = 0; rightIndex < rightLength; rightIndex += 1) {
      if (leftChar !== right[rightStart + rightIndex]) continue;
      const nextLength = previous[rightIndex] + 1;
      current[rightIndex + 1] = nextLength;
      if (nextLength > bestLength) {
        bestLength = nextLength;
        bestLeftEnd = leftStart + leftIndex + 1;
        bestRightEnd = rightStart + rightIndex + 1;
      }
    }
    previous.set(current);
  }

  if (bestLength <= 0) return null;
  return {
    leftStart: bestLeftEnd - bestLength,
    rightStart: bestRightEnd - bestLength,
    length: bestLength
  };
}

function collectCommonBlocks(
  left: string,
  right: string,
  leftStart: number,
  leftEnd: number,
  rightStart: number,
  rightEnd: number,
  output: Array<{ leftStart: number; rightStart: number; length: number }>
): void {
  if (output.length >= MAX_MULTI_HIGHLIGHT_BLOCKS) return;

  const block = findLongestCommonBlock(left, right, leftStart, leftEnd, rightStart, rightEnd);
  if (!block || block.length < MIN_MULTI_HIGHLIGHT_CHARS) return;

  collectCommonBlocks(left, right, leftStart, block.leftStart, rightStart, block.rightStart, output);
  output.push(block);
  collectCommonBlocks(
    left,
    right,
    block.leftStart + block.length,
    leftEnd,
    block.rightStart + block.length,
    rightEnd,
    output
  );
}

export function buildEvidenceHighlightRanges(
  queryText: string,
  candidateText: string,
  matchedSubstring = ""
): EvidenceHighlightRanges {
  const query = String(queryText ?? "");
  const candidate = String(candidateText ?? "");
  if (!query.trim() || !candidate.trim()) {
    return { queryRanges: [], candidateRanges: [] };
  }

  const normalizedQuery = buildNormalizedSource(query);
  const normalizedCandidate = buildNormalizedSource(candidate);
  const blocks: Array<{ leftStart: number; rightStart: number; length: number }> = [];

  if (normalizedQuery.normalized && normalizedCandidate.normalized) {
    collectCommonBlocks(
      normalizedQuery.normalized,
      normalizedCandidate.normalized,
      0,
      normalizedQuery.normalized.length,
      0,
      normalizedCandidate.normalized.length,
      blocks
    );
  }

  const queryRanges = blocks
    .map((block) => mapNormalizedRangeToSource(normalizedQuery, block.leftStart, block.leftStart + block.length))
    .filter((range): range is TextRange => Array.isArray(range));
  const candidateRanges = blocks
    .map((block) => mapNormalizedRangeToSource(normalizedCandidate, block.rightStart, block.rightStart + block.length))
    .filter((range): range is TextRange => Array.isArray(range));

  const fallbackQueryRange = findLooseMatchRange(query, matchedSubstring);
  const fallbackCandidateRange = findLooseMatchRange(candidate, matchedSubstring);

  if (fallbackQueryRange) queryRanges.push(fallbackQueryRange);
  if (fallbackCandidateRange) candidateRanges.push(fallbackCandidateRange);

  return {
    queryRanges: mergeRanges(queryRanges),
    candidateRanges: mergeRanges(candidateRanges)
  };
}

export function renderHighlightedEvidence(text: string, ranges: TextRange[], keyPrefix: string): ReactNode {
  const source = String(text ?? "");
  if (!source) return "-";
  if (ranges.length === 0) return source;

  const nodes: ReactNode[] = [];
  let cursor = 0;

  ranges.forEach(([start, end], index) => {
    const safeStart = Math.max(start, cursor);
    const safeEnd = Math.max(end, safeStart);
    if (safeStart > cursor) {
      nodes.push(source.slice(cursor, safeStart));
    }
    if (safeEnd > safeStart) {
      nodes.push(
        <mark key={`${keyPrefix}-match-${index}`} className="evidence-highlight">
          {source.slice(safeStart, safeEnd)}
        </mark>
      );
    }
    cursor = safeEnd;
  });

  if (cursor < source.length) {
    nodes.push(source.slice(cursor));
  }

  return <>{nodes}</>;
}

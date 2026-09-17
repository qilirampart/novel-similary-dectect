import type {
  CoverChannelListResponse,
  CoverFilterOptionsResponse
} from "./api";

type ChannelPageQuery = {
  keyword: string;
  operatorPk?: number;
  offset: number;
};

type CacheEntry<T> = {
  value: T;
  storedAt: number;
};

const CHANNEL_PAGE_TTL_MS = 60_000;
const FILTER_OPTIONS_TTL_MS = 5 * 60_000;
const channelPages = new Map<string, CacheEntry<CoverChannelListResponse>>();
let filterOptions: CacheEntry<CoverFilterOptionsResponse> | undefined;

function queryKey(query: ChannelPageQuery): string {
  return JSON.stringify([
    query.keyword.trim(),
    query.operatorPk ?? null,
    Math.max(0, Math.floor(query.offset))
  ]);
}

export function getCachedChannelPage(
  query: ChannelPageQuery,
  now = Date.now()
): CoverChannelListResponse | undefined {
  const key = queryKey(query);
  const cached = channelPages.get(key);
  if (!cached) return undefined;
  if (now - cached.storedAt > CHANNEL_PAGE_TTL_MS) {
    channelPages.delete(key);
    return undefined;
  }
  return cached.value;
}

export function setCachedChannelPage(
  query: ChannelPageQuery,
  value: CoverChannelListResponse,
  now = Date.now()
): void {
  channelPages.set(queryKey(query), { value, storedAt: now });
}

export function getCachedFilterOptions(
  now = Date.now()
): CoverFilterOptionsResponse | undefined {
  if (!filterOptions) return undefined;
  if (now - filterOptions.storedAt > FILTER_OPTIONS_TTL_MS) {
    filterOptions = undefined;
    return undefined;
  }
  return filterOptions.value;
}

export function setCachedFilterOptions(
  value: CoverFilterOptionsResponse,
  now = Date.now()
): void {
  filterOptions = { value, storedAt: now };
}

export function clearCoverChannelCache(): void {
  channelPages.clear();
  filterOptions = undefined;
}

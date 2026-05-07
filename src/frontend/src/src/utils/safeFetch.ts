/**
 * Safe Fetch Utility for Flume Frontend
 *
 * Wraps the Fetch API with robust error handling that ensures API responses
 * are always parsed correctly — even when the server returns an HTML 500
 * error page instead of JSON. Without this wrapper, calling `.json()` on an
 * HTML response throws a cryptic SyntaxError and obscures the real problem.
 *
 * Usage:
 *   import { safeFetchJson } from '@/utils/safeFetch';
 *   const data = await safeFetchJson<MyType>('/api/endpoint');
 */

import { appLogger } from '@/utils/logger';

/**
 * Fetch a URL and safely parse the JSON body. If the response is not JSON
 * (e.g. an HTML 500 page), a descriptive error is thrown instead of a
 * confusing SyntaxError.
 */
export async function safeFetchJson<T = unknown>(
  url: string,
  init?: RequestInit,
): Promise<T> {
  const res = await fetch(url, init);
  const contentType = res.headers.get('content-type') ?? '';

  // If the response is not JSON at all, short-circuit with a clear message.
  if (!contentType.includes('application/json')) {
    const snippet = await res.text().catch(() => '');
    const hint = snippet.slice(0, 120).replace(/\s+/g, ' ').trim();
    appLogger.error(`Non-JSON response from ${url}`, {
      status: res.status,
      contentType,
      hint,
    });
    throw new Error(
      `Server returned ${res.status} (${contentType || 'unknown content-type'}) — expected JSON.${hint ? ` Body: ${hint}…` : ''}`,
    );
  }

  const data: unknown = await res.json().catch(() => ({}));

  if (!res.ok) {
    const msg =
      typeof data === 'object' &&
      data !== null &&
      'error' in data &&
      typeof (data as { error: unknown }).error === 'string'
        ? (data as { error: string }).error
        : `Request failed (${res.status})`;
    throw new Error(msg);
  }

  return data as T;
}

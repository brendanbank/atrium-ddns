import { QueryClient } from '@tanstack/react-query';

/** Single QueryClient shared across every component this bundle
 *  registers — they all wrap their tree in a QueryClientProvider that
 *  points at this client so they share cache for the same query keys.
 */
export const queryClient = new QueryClient({
  defaultOptions: {
    queries: { staleTime: 2_000, retry: 1 },
  },
});

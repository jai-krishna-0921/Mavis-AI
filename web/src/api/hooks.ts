import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, ApiError } from './client'
import type { ConnectorId, Preferences, VaultKind } from './types'

export const useMe = () =>
  useQuery({
    queryKey: ['me'],
    queryFn: api.me,
    retry: (n, e) => !(e instanceof ApiError && e.status === 401) && n < 1,
    staleTime: 60_000,
  })

export const useConnectors = () => useQuery({ queryKey: ['connectors'], queryFn: api.connectors })

export function useDisconnect() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (v: { id: ConnectorId; forget: boolean }) => api.disconnect(v.id, v.forget),
    onSuccess: () => Promise.all([
      qc.invalidateQueries({ queryKey: ['connectors'] }),
      qc.invalidateQueries({ queryKey: ['vault'] }),
    ]),
  })
}

export const useVaultSummary = () => useQuery({ queryKey: ['vault', 'summary'], queryFn: api.vaultSummary })
export const useVaultSources = () => useQuery({ queryKey: ['vault', 'sources'], queryFn: api.vaultSources })

export const useVaultItems = (kind: VaultKind, q: string) =>
  useInfiniteQuery({
    queryKey: ['vault', 'items', kind, q],
    queryFn: ({ pageParam }) => api.vaultItems({ kind, q, cursor: pageParam }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (p) => p.next_cursor ?? undefined,
  })

export function usePatchItem() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (v: { id: string; title?: string; detail?: string }) => api.vaultPatch(v.id, { title: v.title, detail: v.detail }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['vault'] }),
  })
}

export function useForgetItem() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (v: { id: string; alsoSuppress: boolean }) => api.vaultForget(v.id, v.alsoSuppress),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['vault'] }),
  })
}

export function useForgetSource() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (source: string) => api.vaultForgetSource(source),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['vault'] }),
  })
}

export const usePreferences = () => useQuery({ queryKey: ['preferences'], queryFn: api.preferences })

export function usePatchPreferences() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (p: Partial<Preferences>) => api.patchPreferences(p),
    onSuccess: (data) => { qc.setQueryData(['preferences'], data); void qc.invalidateQueries({ queryKey: ['me'] }) },
  })
}

export const useInvites = () => useQuery({ queryKey: ['invites'], queryFn: api.invites })

export function useCreateInvite() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (name?: string) => api.createInvite(name),
    onSuccess: () => Promise.all([qc.invalidateQueries({ queryKey: ['invites'] }), qc.invalidateQueries({ queryKey: ['me'] })]),
  })
}

export function useRevokeInvite() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (code: string) => api.revokeInvite(code),
    onSuccess: () => Promise.all([qc.invalidateQueries({ queryKey: ['invites'] }), qc.invalidateQueries({ queryKey: ['me'] })]),
  })
}

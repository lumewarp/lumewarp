import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type Resource } from "./api";
import { useToast } from "./components/Toast";

export function useMe() {
  return useQuery({ queryKey: ["me"], queryFn: () => api.me(), staleTime: 5 * 60_000 });
}

/** Viewers are read-only; the server enforces it too. */
export function useCanWrite() {
  const { data } = useMe();
  return !!data && data.role !== "viewer";
}

export function useList<T>(key: string, fn: () => Promise<T[]>) {
  return useQuery({ queryKey: [key], queryFn: fn });
}

/** list + create/update/remove mutations for a CRUD resource, invalidating the list and toasting errors. */
export function useCrud<T, C>(key: string, res: Omit<Resource<T, C>, "create"> & { create(input: C): Promise<unknown> }, noun: string) {
  const qc = useQueryClient();
  const toast = useToast();
  const list = useQuery({ queryKey: [key], queryFn: () => res.list() });
  const done = (msg: string) => () => {
    void qc.invalidateQueries({ queryKey: [key] });
    toast("success", msg);
  };
  const fail = (e: Error) => toast("error", e.message);
  const create = useMutation({ mutationFn: (i: C) => res.create(i), onSuccess: done(`${noun} created`) });
  const update = useMutation({ mutationFn: ({ id, input }: { id: string; input: Partial<C> }) => res.update(id, input), onSuccess: done(`${noun} saved`) });
  const remove = useMutation({ mutationFn: (id: string) => res.remove(id), onSuccess: done(`${noun} deleted`), onError: fail });
  return { list, create, update, remove };
}

/** id → name lookup for rendering foreign keys. */
export function useNames(rows: { id: string; name: string }[] | undefined) {
  const map = new Map((rows ?? []).map((r) => [r.id, r.name]));
  return (id: string | null | undefined) => (id ? (map.get(id) ?? id) : "—");
}

// Behaviour adapted from t3code (https://github.com/pingdotgg/t3code), MIT License,
// Copyright (c) 2026 T3 Tools Inc. See mobile/modules/opencuria-highlights/LICENSE.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { OpenCuriaClient } from "@opencuria/client";
import type { ProjectEntry } from "./fileTree";

export const WORKSPACE_ROOT = "/workspace";
const SEARCH_DEBOUNCE_MS = 200;

/** Workspace-relative path (no leading slash) of an absolute workspace path. */
export function relativeWorkspacePath(
  path: string,
  root = WORKSPACE_ROOT,
): string | null {
  if (path === root) return "";
  if (!path.startsWith(`${root}/`)) return null;
  return path.slice(root.length + 1).replace(/\/+$/, "");
}

export function absoluteWorkspacePath(
  relative: string,
  root = WORKSPACE_ROOT,
): string {
  return relative ? `${root}/${relative}` : root;
}

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback;
}

export interface FileTreeEntries {
  entries: ReadonlyArray<ProjectEntry>;
  expandedPaths: ReadonlySet<string>;
  setExpandedPaths: (
    update: (current: ReadonlySet<string>) => ReadonlySet<string>,
  ) => void;
  loadingDirectories: ReadonlySet<string>;
  loadDirectory: (path: string, retry?: boolean) => void;
  error: string | null;
  isPending: boolean;
  isRefreshing: boolean;
  refresh: () => void;
  searchResultPaths: ReadonlySet<string> | null;
  searchTruncated: boolean;
  selectedPath: string | null;
  selectPath: (path: string | null) => void;
}

/**
 * Lazily listed workspace tree: the root on first use, every other directory
 * when it is expanded. A non-empty query switches to the runner's file search
 * and shows matches with their folders.
 */
export function useFileTreeEntries({
  client,
  workspaceId,
  searchQuery,
  enabled,
}: {
  client: OpenCuriaClient;
  workspaceId: string;
  searchQuery: string;
  enabled: boolean;
}): FileTreeEntries {
  const [listings, setListings] = useState<ReadonlyMap<string, ProjectEntry[]>>(
    new Map(),
  );
  const [loading, setLoading] = useState<ReadonlySet<string>>(new Set());
  const [errors, setErrors] = useState<ReadonlyMap<string, string>>(new Map());
  const [expandedPaths, setExpanded] = useState<ReadonlySet<string>>(new Set());
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [isRefreshing, setRefreshing] = useState(false);
  const [search, setSearch] = useState<{
    query: string;
    paths: string[];
    truncated: boolean;
    error: string | null;
  } | null>(null);
  const generation = useRef(0);
  const requested = useRef(new Set<string>());

  const loadDirectory = useCallback(
    (path: string, retry = false) => {
      if (!retry && requested.current.has(path)) return;
      requested.current.add(path);
      const gen = generation.current;
      setLoading((current) => new Set(current).add(path));
      client.files
        .listFiles(workspaceId, absoluteWorkspacePath(path))
        .then((result) => {
          if (gen !== generation.current) return;
          if (result.error) throw new Error(result.error);
          const children: ProjectEntry[] = [];
          for (const entry of result.entries ?? []) {
            const relative = relativeWorkspacePath(entry.path);
            if (relative)
              children.push({
                path: relative,
                kind: entry.type === "directory" ? "directory" : "file",
              });
          }
          setListings((current) => new Map(current).set(path, children));
          setErrors((current) => {
            if (!current.has(path)) return current;
            const next = new Map(current);
            next.delete(path);
            return next;
          });
        })
        .catch((error: unknown) => {
          if (gen !== generation.current) return;
          requested.current.delete(path);
          setErrors((current) =>
            new Map(current).set(
              path,
              errorMessage(error, "Could not list files"),
            ),
          );
        })
        .finally(() => {
          if (gen !== generation.current) return;
          setLoading((current) => {
            const next = new Set(current);
            next.delete(path);
            return next;
          });
        });
    },
    [client, workspaceId],
  );

  // A new workspace starts over.
  useEffect(() => {
    generation.current += 1;
    requested.current = new Set();
    setListings(new Map());
    setErrors(new Map());
    setLoading(new Set());
    setExpanded(new Set());
    setSelectedPath(null);
    setSearch(null);
  }, [client, workspaceId]);

  useEffect(() => {
    if (enabled) loadDirectory("");
  }, [enabled, loadDirectory, listings.size]);

  const query = searchQuery.trim();
  useEffect(() => {
    if (!query || !enabled) {
      setSearch(null);
      return;
    }
    const gen = generation.current;
    let live = true;
    const timer = setTimeout(() => {
      client.files
        .findFiles(workspaceId, query)
        .then((result) => {
          if (!live || gen !== generation.current) return;
          if (result.error) throw new Error(result.error);
          setSearch({
            query,
            paths: result.paths
              .flatMap((path) => relativeWorkspacePath(path) ?? [])
              .filter(Boolean),
            truncated: result.truncated === true,
            error: null,
          });
        })
        .catch((error: unknown) => {
          if (live && gen === generation.current)
            setSearch({
              query,
              paths: [],
              truncated: false,
              error: errorMessage(error, "Search failed"),
            });
        });
    }, SEARCH_DEBOUNCE_MS);
    return () => {
      live = false;
      clearTimeout(timer);
    };
  }, [client, enabled, query, workspaceId]);

  const searching = Boolean(query);
  const entries = useMemo(() => {
    if (searching)
      return (search?.paths ?? []).map(
        (path): ProjectEntry => ({ path, kind: "file" }),
      );
    const all: ProjectEntry[] = [];
    for (const children of listings.values()) all.push(...children);
    return all;
  }, [listings, search, searching]);

  const refresh = useCallback(() => {
    generation.current += 1;
    requested.current = new Set();
    setErrors(new Map());
    setLoading(new Set());
    setRefreshing(true);
    const gen = generation.current;
    const directories = ["", ...expandedPaths];
    Promise.all(
      directories.map((path) =>
        client.files
          .listFiles(workspaceId, absoluteWorkspacePath(path))
          .then((result) => {
            if (result.error) throw new Error(result.error);
            requested.current.add(path);
            return [
              path,
              (result.entries ?? []).flatMap((entry): ProjectEntry[] => {
                const relative = relativeWorkspacePath(entry.path);
                return relative
                  ? [
                      {
                        path: relative,
                        kind: entry.type === "directory" ? "directory" : "file",
                      },
                    ]
                  : [];
              }),
            ] as const;
          })
          .catch(
            (error: unknown) =>
              [path, errorMessage(error, "Could not list files")] as const,
          ),
      ),
    )
      .then((results) => {
        if (gen !== generation.current) return;
        const next = new Map<string, ProjectEntry[]>();
        const failed = new Map<string, string>();
        for (const [path, value] of results) {
          if (typeof value === "string") failed.set(path, value);
          else next.set(path, value);
        }
        setListings(next);
        setErrors(failed);
      })
      .finally(() => {
        if (gen === generation.current) setRefreshing(false);
      });
  }, [client, expandedPaths, workspaceId]);

  const selectPath = useCallback((path: string | null) => {
    setSelectedPath(path);
    if (!path) return;
    // Reveal: expand every ancestor of the selection.
    setExpanded((current) => {
      const next = new Set(current);
      const parts = path.split("/");
      for (let index = 1; index < parts.length; index += 1)
        next.add(parts.slice(0, index).join("/"));
      return next.size === current.size ? current : next;
    });
  }, []);

  const searchResultPaths = useMemo(
    () => (searching ? new Set(search?.paths ?? []) : null),
    [search, searching],
  );

  return {
    entries,
    expandedPaths,
    setExpandedPaths: setExpanded,
    loadingDirectories: loading,
    loadDirectory,
    error: searching
      ? (search?.error ?? null)
      : (errors.get("") ?? errors.values().next().value ?? null),
    isPending: searching
      ? search === null || search.query !== query
      : !listings.has("") && !errors.has(""),
    isRefreshing,
    refresh,
    searchResultPaths,
    searchTruncated: searching && search?.truncated === true,
    selectedPath,
    selectPath,
  };
}

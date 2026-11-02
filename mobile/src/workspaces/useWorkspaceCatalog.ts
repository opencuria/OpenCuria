import { useCallback, useEffect, useRef, useState } from "react";
import type {
  CapturedImage,
  Credential,
  ImageDefinition,
  OpenCuriaClient,
  Plugin,
  Runner,
  RunnerImageBuild,
} from "@opencuria/client";
import { errorText } from "./workspaceOperations";

/** Server catalogs the workspace forms choose from. */
export interface WorkspaceCatalog {
  runners: Runner[];
  plugins: Plugin[];
  credentials: Credential[];
  capturedImages: CapturedImage[];
  definitions: ImageDefinition[];
  buildsByDefinition: Record<string, RunnerImageBuild[]>;
  /** Plugins and credentials loaded; selections can be validated. */
  ready: boolean;
  loading: boolean;
  /** Plugins or credentials failed to load; saving is blocked until a retry. */
  error: string | null;
  /** Images or runners failed to load; creation may lack options. */
  imageError: string | null;
  reload: () => Promise<void>;
}

const EMPTY = {
  runners: [] as Runner[],
  plugins: [] as Plugin[],
  credentials: [] as Credential[],
  capturedImages: [] as CapturedImage[],
  definitions: [] as ImageDefinition[],
  buildsByDefinition: {} as Record<string, RunnerImageBuild[]>,
};

/** Defer a request so a synchronous failure rejects only its own result. */
function settle<T>(load: () => Promise<T>): Promise<T> {
  return Promise.resolve().then(load);
}

async function loadDefinitions(client: OpenCuriaClient): Promise<{
  definitions: ImageDefinition[];
  buildsByDefinition: Record<string, RunnerImageBuild[]>;
}> {
  const definitions = await client.images.listDefinitions();
  const pairs = await Promise.all(
    definitions.map(
      async (definition) =>
        [
          definition.id,
          await client.images.listRunnerBuilds(definition.id),
        ] as const,
    ),
  );
  return { definitions, buildsByDefinition: Object.fromEntries(pairs) };
}

/**
 * Loads runners, plugins, credentials and images while `active`, and again on
 * `reload()`. Results of an older client or request are discarded.
 */
export function useWorkspaceCatalog(
  client: OpenCuriaClient,
  active: boolean,
): WorkspaceCatalog {
  const [data, setData] = useState(EMPTY);
  const [ready, setReady] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [imageError, setImageError] = useState<string | null>(null);
  const generation = useRef(0);

  const reload = useCallback(async () => {
    const request = ++generation.current;
    setLoading(true);
    const [runners, plugins, credentials, captured, definitions] =
      await Promise.allSettled([
        settle(() => client.runners.list()),
        settle(() => client.plugins.list()),
        settle(() => client.credentials.list()),
        settle(() => client.images.listCaptured()),
        settle(() => loadDefinitions(client)),
      ]);
    if (request !== generation.current) return;
    setData({
      runners: runners.status === "fulfilled" ? runners.value : [],
      plugins: plugins.status === "fulfilled" ? plugins.value : [],
      credentials: credentials.status === "fulfilled" ? credentials.value : [],
      capturedImages: captured.status === "fulfilled" ? captured.value : [],
      definitions:
        definitions.status === "fulfilled" ? definitions.value.definitions : [],
      buildsByDefinition:
        definitions.status === "fulfilled"
          ? definitions.value.buildsByDefinition
          : {},
    });
    const selectionFailure = [plugins, credentials].find(
      (result) => result.status === "rejected",
    );
    const imageFailure = [runners, captured, definitions].find(
      (result) => result.status === "rejected",
    );
    setError(
      selectionFailure?.status === "rejected"
        ? errorText(
            selectionFailure.reason,
            "Could not load plugins and credentials.",
          )
        : null,
    );
    setImageError(
      imageFailure?.status === "rejected"
        ? errorText(imageFailure.reason, "Could not load images and runners.")
        : null,
    );
    setReady(!selectionFailure);
    setLoading(false);
  }, [client]);

  useEffect(() => {
    generation.current += 1;
    setData(EMPTY);
    setReady(false);
    setError(null);
    setImageError(null);
  }, [client]);

  useEffect(() => {
    if (active) void reload();
  }, [active, reload]);

  useEffect(
    () => () => {
      generation.current += 1;
    },
    [],
  );

  return { ...data, ready, loading, error, imageError, reload };
}

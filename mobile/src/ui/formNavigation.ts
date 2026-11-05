import { createContext, useContext } from "react";

/** Push navigation for `link` form rows inside the settings navigator. */
export interface FormNavigation {
  push: (route: string) => void;
}

export const FormNavigationContext = createContext<FormNavigation | null>(null);

/** The enclosing settings navigator; `null` in a standalone form. */
export function useFormNavigation(): FormNavigation | null {
  return useContext(FormNavigationContext);
}

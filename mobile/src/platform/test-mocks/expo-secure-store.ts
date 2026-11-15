const values = new Map<string, string>();
export const getItemAsync = async (key: string): Promise<string | null> =>
  values.get(key) ?? null;
export const setItemAsync = async (
  key: string,
  value: string,
): Promise<void> => {
  values.set(key, value);
};
export const deleteItemAsync = async (key: string): Promise<void> => {
  values.delete(key);
};
export const __values = values;

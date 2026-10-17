export const requestCameraPermissionsAsync = async (): Promise<{
  granted: boolean;
  canAskAgain: boolean;
}> => ({ granted: true, canAskAgain: true });
export const requestMediaLibraryPermissionsAsync = async (): Promise<{
  granted: boolean;
  canAskAgain: boolean;
}> => ({ granted: true, canAskAgain: true });
let nextResult: unknown = { canceled: true, assets: null };
export const __setNextResult = (result: unknown): void => {
  nextResult = result;
};
export const launchCameraAsync = async (): Promise<unknown> => nextResult;
export const launchImageLibraryAsync = async (): Promise<unknown> => nextResult;

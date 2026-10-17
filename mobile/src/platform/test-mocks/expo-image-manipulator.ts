export const SaveFormat = { JPEG: "jpeg" };
let nextSize = 3;
export const __setNextSize = (size: number): void => {
  nextSize = size;
};
export const ImageManipulator = {
  manipulate: () => ({
    resize: () => undefined,
    renderAsync: async () => ({
      saveAsync: async () => {
        const uri = `file:///cache/resized-${Math.random()}.jpg`;
        const { File } = await import("./expo-file-system");
        const file = new File(uri);
        file.create();
        file.write("x".repeat(nextSize));
        return { uri };
      },
    }),
  }),
};

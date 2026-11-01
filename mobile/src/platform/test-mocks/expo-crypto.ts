import { createHash, randomUUID } from "node:crypto";

export const CryptoDigestAlgorithm = { SHA256: "SHA-256" };
export const digestStringAsync = async (
  algorithm: string,
  value: string,
): Promise<string> =>
  createHash(algorithm.replace("-", "").toLowerCase()).update(value).digest("hex");
export { randomUUID };

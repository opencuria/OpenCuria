/** Shared by the terminal (PTY bytes) and the file viewer. */
/**
 * Base64 <-> UTF-8 for the PTY wire format. Output arrives as arbitrary byte
 * chunks, so the decoder keeps an incomplete multi-byte sequence until the
 * next chunk instead of emitting replacement characters mid-character.
 * Self-contained: Hermes does not guarantee `TextDecoder`.
 */

const ALPHABET =
  "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
const LOOKUP = new Uint8Array(256).fill(255);
for (let index = 0; index < ALPHABET.length; index += 1)
  LOOKUP[ALPHABET.charCodeAt(index)] = index;
LOOKUP["-".charCodeAt(0)] = 62;
LOOKUP["_".charCodeAt(0)] = 63;

/** Decode base64 (whitespace and padding tolerated) into bytes. */
export function base64ToBytes(input: string): Uint8Array {
  const bytes = new Uint8Array(Math.floor((input.length * 3) / 4) + 3);
  let length = 0;
  let buffer = 0;
  let bits = 0;
  for (let index = 0; index < input.length; index += 1) {
    const value = LOOKUP[input.charCodeAt(index)]!;
    if (value === 255) continue;
    buffer = (buffer << 6) | value;
    bits += 6;
    if (bits >= 8) {
      bits -= 8;
      bytes[length++] = (buffer >> bits) & 0xff;
    }
  }
  return bytes.subarray(0, length);
}

/** Encode bytes as standard padded base64. */
export function bytesToBase64(bytes: Uint8Array): string {
  let output = "";
  let index = 0;
  for (; index + 2 < bytes.length; index += 3) {
    const value =
      (bytes[index]! << 16) | (bytes[index + 1]! << 8) | bytes[index + 2]!;
    output +=
      ALPHABET[(value >> 18) & 63]! +
      ALPHABET[(value >> 12) & 63]! +
      ALPHABET[(value >> 6) & 63]! +
      ALPHABET[value & 63]!;
  }
  const rest = bytes.length - index;
  if (rest === 1) {
    const value = bytes[index]! << 16;
    output += `${ALPHABET[(value >> 18) & 63]}${ALPHABET[(value >> 12) & 63]}==`;
  } else if (rest === 2) {
    const value = (bytes[index]! << 16) | (bytes[index + 1]! << 8);
    output += `${ALPHABET[(value >> 18) & 63]}${ALPHABET[(value >> 12) & 63]}${ALPHABET[(value >> 6) & 63]}=`;
  }
  return output;
}

/** UTF-8 bytes of a JavaScript string (lone surrogates become U+FFFD). */
export function utf8Encode(text: string): Uint8Array {
  const bytes: number[] = [];
  for (let index = 0; index < text.length; index += 1) {
    let code = text.charCodeAt(index);
    if (code >= 0xd800 && code <= 0xdbff && index + 1 < text.length) {
      const next = text.charCodeAt(index + 1);
      if (next >= 0xdc00 && next <= 0xdfff) {
        code = 0x10000 + ((code - 0xd800) << 10) + (next - 0xdc00);
        index += 1;
      } else code = 0xfffd;
    } else if (code >= 0xd800 && code <= 0xdfff) code = 0xfffd;
    if (code < 0x80) bytes.push(code);
    else if (code < 0x800) bytes.push(0xc0 | (code >> 6), 0x80 | (code & 63));
    else if (code < 0x10000)
      bytes.push(
        0xe0 | (code >> 12),
        0x80 | ((code >> 6) & 63),
        0x80 | (code & 63),
      );
    else
      bytes.push(
        0xf0 | (code >> 18),
        0x80 | ((code >> 12) & 63),
        0x80 | ((code >> 6) & 63),
        0x80 | (code & 63),
      );
  }
  return Uint8Array.from(bytes);
}

/** Terminal input as the base64 UTF-8 payload the PTY expects. */
export function encodeTerminalInput(text: string): string {
  return bytesToBase64(utf8Encode(text));
}

/** Expected length of a UTF-8 sequence from its lead byte; 0 if invalid. */
function sequenceLength(lead: number): number {
  if (lead < 0x80) return 1;
  if (lead >= 0xc2 && lead <= 0xdf) return 2;
  if (lead >= 0xe0 && lead <= 0xef) return 3;
  if (lead >= 0xf0 && lead <= 0xf4) return 4;
  return 0;
}

/** Streaming UTF-8 decoder: `decode(chunk)` returns the complete characters. */
export function createUtf8StreamDecoder(): {
  decode: (bytes: Uint8Array) => string;
  reset: () => void;
} {
  let pending: number[] = [];
  return {
    reset() {
      pending = [];
    },
    decode(chunk) {
      const bytes = pending.length
        ? Uint8Array.from([...pending, ...chunk])
        : chunk;
      pending = [];
      let output = "";
      let index = 0;
      while (index < bytes.length) {
        const lead = bytes[index]!;
        const length = sequenceLength(lead);
        if (length === 1) {
          output += String.fromCharCode(lead);
          index += 1;
          continue;
        }
        if (length === 0) {
          output += "�";
          index += 1;
          continue;
        }
        if (index + length > bytes.length) {
          // Keep a sequence split across chunks only if it is valid so far.
          let valid = true;
          for (let next = index + 1; next < bytes.length; next += 1)
            if ((bytes[next]! & 0xc0) !== 0x80) valid = false;
          if (valid) {
            pending = Array.from(bytes.subarray(index));
            break;
          }
        }
        let code = lead & (0xff >> (length + 1));
        let ok = true;
        for (let next = 1; next < length; next += 1) {
          const byte = bytes[index + next];
          if (byte === undefined || (byte & 0xc0) !== 0x80) {
            ok = false;
            break;
          }
          code = (code << 6) | (byte & 63);
        }
        if (!ok) {
          output += "�";
          index += 1;
          continue;
        }
        output += String.fromCodePoint(code);
        index += length;
      }
      return output;
    },
  };
}

import { test } from 'node:test';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { fileURLToPath } from 'node:url';

test('Linux accessibility discovery, activation and stale bridge recovery', async () => {
  await promisify(execFile)('/usr/bin/python3', [
    fileURLToPath(new URL('./fixtures/native_ax.py', import.meta.url)),
  ]);
});

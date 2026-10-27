import { build } from 'esbuild';
import { mkdir, copyFile, readFile, writeFile, readdir } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const root = path.dirname(fileURLToPath(import.meta.url));
const output = path.join(root, 'dist');
await mkdir(output, { recursive: true });
const bundled = await build({
  entryPoints: [path.join(root, 'src/server.ts'), path.join(root, 'src/worker.ts')],
  outdir: output,
  bundle: true,
  metafile: true,
  platform: 'node',
  format: 'esm',
  target: 'node22',
  banner: { js: "import { createRequire as __createRequire } from 'node:module'; const require = __createRequire(import.meta.url);" },
});
await copyFile(path.join(root, 'native/linux.py'), path.join(output, 'linux.py'));
const packages = new Set();
for (const input of Object.keys(bundled.metafile.inputs)) {
  if (!input.includes('node_modules')) continue;
  let directory = path.dirname(path.resolve(input));
  while (directory.includes('node_modules')) {
    try {
      const pkg = JSON.parse(await readFile(path.join(directory, 'package.json'), 'utf8'));
      if (pkg.name) { packages.add(directory); break; }
    } catch {}
    directory = path.dirname(directory);
  }
}
const notices = [];
for (const directory of [...packages].sort()) {
  const pkg = JSON.parse(await readFile(path.join(directory, 'package.json'), 'utf8'));
  const licenses = (await readdir(directory)).filter(name => /^(licen[sc]e|copying|notice)([.\-]|$)/i.test(name));
  for (const name of licenses) {
    notices.push(`${pkg.name} ${pkg.version} — ${name}\n${await readFile(path.join(directory, name), 'utf8')}`);
  }
  if (!licenses.length) throw new Error(`Missing license notice for ${pkg.name}`);
}
await writeFile(path.join(output, 'NOTICES.txt'), notices.join('\n\n')); 
const files = {};
for (const name of ['server.js', 'worker.js', 'linux.py', 'NOTICES.txt']) {
  files[name] = createHash('sha256').update(await readFile(path.join(output, name))).digest('hex');
}
await writeFile(path.join(output, 'manifest.json'), JSON.stringify({ version: '0.1.0', protocol_version: 1, files }, null, 2) + '\n');

import { readdir } from 'node:fs/promises';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const project = fileURLToPath(new URL('../', import.meta.url));
let checked = 0;
for (const directory of ['web/src', 'scripts', 'tests']) {
  for (const file of await readdir(path.join(project, directory))) {
    if (!/\.(mjs|js)$/.test(file)) continue;
    const result = spawnSync(process.execPath, ['--check', path.join(project, directory, file)], { stdio: 'inherit' });
    if (result.status !== 0) process.exit(result.status || 1);
    checked++;
  }
}
console.log(`JavaScript 语法检查通过（${checked} 个文件）。`);

import { readdir, readFile, mkdir, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { createHash } from 'node:crypto';

const project = fileURLToPath(new URL('../', import.meta.url));
const runner = path.join(project, 'runner');
const roots = new Set(['README.md', 'NOTICE.md', 'package.json', 'package-lock.json', 'requirements.txt', 'requirements.lock', '.gitignore', '.env.example', '.onelap-web.json']);
const dirs = new Set(['onelap_garmin_sync', 'uploader', 'tests', 'scripts']);
const workflows = new Set([
  '.github/workflows/sync.yml',
  '.github/workflows/reconcile.yml',
  '.github/workflows/garmin-session.yml',
]);
const files = [];

async function walk(relative = '') {
  const entries = await readdir(path.join(runner, relative), { withFileTypes: true });
  for (const entry of entries.sort((a, b) => a.name.localeCompare(b.name, 'en'))) {
    const filename = relative ? `${relative}/${entry.name}` : entry.name;
    if (entry.isSymbolicLink()) throw new Error(`Runner 模板不允许符号链接：${filename}`);
    if (entry.isDirectory()) {
      if (filename === '.github' || filename === '.github/workflows' || dirs.has(filename) || (dirs.has(filename.split('/')[0]) && !entry.name.startsWith('.') && entry.name !== '__pycache__' && entry.name !== 'node_modules')) await walk(filename);
      continue;
    }
    const inSource = dirs.has(filename.split('/')[0]) && /\.(py|js|json|md|txt)$/.test(filename) && !entry.name.endsWith('.session.json');
    if (!roots.has(filename) && !workflows.has(filename) && !inSource) continue;
    const content = await readFile(path.join(runner, filename));
    if (content.length > 1_000_000) throw new Error(`模板文件过大：${filename}`);
    const installedPath = filename === 'README.md' ? '同步程序说明.md' : filename;
    files.push({ path: installedPath, content: content.toString('base64'), sha256: createHash('sha256').update(content).digest('hex') });
  }
}

await walk();
for (const required of ['.onelap-web.json', 'onelap_garmin_sync/cli.py', 'uploader/garmin-upload.js', 'package-lock.json', 'requirements.lock', ...workflows]) {
  if (!files.some(file => file.path === required)) throw new Error(`模板缺少 ${required}`);
}
const marker = JSON.parse(await readFile(path.join(runner, '.onelap-web.json'), 'utf8'));
const output = path.join(project, 'web/public');
await mkdir(output, { recursive: true });
await writeFile(path.join(output, 'runner-template.json'), `${JSON.stringify({ version: marker.version, files })}\n`);
console.log(`已打包 ${files.length} 个同步程序文件。模板不包含账号、会话或活动台账。`);

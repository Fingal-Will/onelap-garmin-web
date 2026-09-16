import { readFile, stat } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const dist = fileURLToPath(new URL('../dist/', import.meta.url));
const html = await readFile(path.join(dist, 'index.html'), 'utf8');
for (const match of html.matchAll(/(?:src|href)="([^"]+)"/g)) {
  const value = match[1];
  if (/^(?:https?:|#|mailto:)/.test(value)) continue;
  if (value.startsWith('/')) throw new Error(`不兼容项目 Pages 路径的绝对资源：${value}`);
  await stat(path.join(dist, value.split(/[?#]/)[0]));
}
const bundle = JSON.parse(await readFile(path.join(dist, 'runner-template.json'), 'utf8'));
if (!bundle.files?.length) throw new Error('同步程序安装模板为空。');
for (const file of bundle.files) {
  if (file.path.startsWith('data/') || file.path.includes('node_modules/') || file.path === '.env' || file.path.endsWith('.session.json') || file.path.endsWith('.db')) throw new Error(`模板包含禁止发布的文件：${file.path}`);
}
console.log(`Pages 构建检查通过，静态资源路径有效，安装模板包含 ${bundle.files.length} 个文件。`);

/**
 * 从后端的 OpenAPI 生成前端类型。
 *
 * 优先直接问 Python 要 schema（`app.openapi()`），不要求后端正在运行：此前这一步只能
 * 对着跑起来的服务生成，于是「刚加的接口」必须先起服务才生成得出来，而起服务又可能
 * 去动用户正在用的那个实例。跑不动 Python 时退回到 HTTP。
 */
import { execFileSync, execSync } from 'node:child_process';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import process from 'node:process';

const OUT = 'src/lib/api/types.gen.ts';
const REPO_ROOT = path.resolve(process.cwd(), '../..');
const FALLBACK_URL = process.env.AGENTMEM_API ?? 'http://127.0.0.1:8765/openapi.json';

function fromPython() {
  const code =
    'import json;from apps.api.main import create_app;print(json.dumps(create_app().openapi()))';
  const json = execFileSync('uv', ['run', 'python', '-c', code], {
    cwd: REPO_ROOT,
    encoding: 'utf8',
    maxBuffer: 64 * 1024 * 1024,
  });
  const file = path.join(mkdtempSync(path.join(tmpdir(), 'agentmem-openapi-')), 'openapi.json');
  writeFileSync(file, json);
  return file;
}

let source;
try {
  source = fromPython();
  process.stdout.write('OpenAPI 来自本地代码（无需后端在跑）\n');
} catch (error) {
  process.stdout.write(`直接从代码取 schema 失败（${error.message.split('\n')[0]}），改用 HTTP\n`);
  source = FALLBACK_URL;
}

execSync(`npx openapi-typescript ${source} -o ${OUT}`, { stdio: 'inherit' });

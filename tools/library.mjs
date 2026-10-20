#!/usr/bin/env node
/** Standalone CLI bootstrap; deliberately does not import the DSH host. */
import { spawnSync } from 'node:child_process'
import { existsSync, mkdirSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { homedir } from 'node:os'
import { ensureUv } from '../lib/uvfetch.mjs'

const root = dirname(dirname(fileURLToPath(import.meta.url)))
const parent = join(process.env.DSH_HOME || join(homedir(), '.dsh'), 'bili-duty')
const home = resolve(process.env.BILI_LIBRARY_HOME || join(parent, 'library'))
const venv = join(home, '.venv')
const executable = join(venv, process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python')
const args = process.argv.slice(2)
const run = (command, parameters, env = {}) => spawnSync(command, parameters, {
  cwd: root, stdio: 'inherit', env: { ...process.env, ...env, PYTHONUTF8: '1' },
})
if (process.platform === 'win32') {
  console.error('资料库第一阶段支持 macOS / Linux（使用 POSIX 进程锁）。')
  process.exit(1)
}
if (args[0] === 'setup') {
  mkdirSync(home, { recursive: true, mode: 0o700 })
  let uv = 'uv'
  if (spawnSync(uv, ['--version'], { stdio: 'ignore' }).status !== 0) {
    uv = await ensureUv({ dir: join(home, 'bin'), log: m => console.error(m) })
  }
  if (!uv) { console.error('uv 下载失败，请按 AGENTS.md 的离线安装说明放入 uv。'); process.exit(1) }
  const cache = { UV_CACHE_DIR: join(home, '.uv-cache'), UV_PYTHON_INSTALL_DIR: join(home, '.python') }
  if (!existsSync(executable)) {
    const result = run(uv, ['venv', '--python', '3.12', venv], cache)
    if (result.status !== 0) process.exit(result.status || 1)
  }
  const result = run(uv, ['pip', 'install', '--python', executable, '-r', join(root, 'requirements-library.txt')], cache)
  process.exit(result.status ?? 1)
}
const python = process.env.BILI_LIBRARY_PYTHON || (existsSync(executable) ? executable : 'python3')
const result = run(python, ['-m', 'bili_library', ...args])
if (result.error) console.error('Python 不可用；请先运行 node tools/library.mjs setup。')
process.exit(result.status ?? 1)

// 读 back_end/.env（不引依赖，几十行够了）
//
// 为什么需要：harness 下几个 .mjs 是 node 脚本，而配置放在 back_end/.env。
// node 不会自动读，过去只能在 shell 里 export —— 用户要求「启动参放到 env 里配」，
// 所以这里做一个最小 dotenv 解析。
//
// 优先级：**进程环境变量 > .env**。这样临时覆盖（`QQ_MCP_PKG=... node ...`）仍然有效。

import { readFileSync, existsSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const ENV_PATH = process.env.AMARANTINE_ENV_FILE ?? resolve(here, "../back_end/.env");

function parseEnv(text) {
  const out = {};
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || line.startsWith("#")) continue;
    const eq = line.indexOf("=");
    if (eq <= 0) continue;
    const key = line.slice(0, eq).trim();
    let val = line.slice(eq + 1).trim();
    if (
      (val.startsWith('"') && val.endsWith('"')) ||
      (val.startsWith("'") && val.endsWith("'"))
    ) {
      val = val.slice(1, -1);
    }
    out[key] = val;
  }
  return out;
}

const fileEnv = existsSync(ENV_PATH) ? parseEnv(readFileSync(ENV_PATH, "utf8")) : {};

/** 取配置：进程环境变量优先，其次 .env，最后 fallback。 */
export function env(name, fallback = "") {
  const fromProcess = process.env[name];
  if (fromProcess !== undefined && fromProcess !== "") return fromProcess;
  const fromFile = fileEnv[name];
  if (fromFile !== undefined && fromFile !== "") return fromFile;
  return fallback;
}

/** 把 .env 里的键值并进子进程 env（拉 qq-mcp 时要用）。 */
export function childEnv(extra = {}) {
  const base = Object.fromEntries(
    Object.entries(process.env).filter(([, v]) => typeof v === "string"),
  );
  for (const [k, v] of Object.entries(fileEnv)) {
    if (base[k] === undefined) base[k] = v;
  }
  return { ...base, ...extra };
}

export { ENV_PATH };

/**
 * 把 Windows 盘符路径与 WSL 挂载路径互相转换。
 *
 * 为什么需要：这套脚本在 WSL 里跑，但 qq-mcp 是 **Windows 侧** node 包。
 * `.env` 里写 `D:/nodejs/...` 时，WSL 的 node 解析不了；写 `/mnt/d/...` 时
 * 传给 Windows 侧进程又不认。按当前平台归一是最省心的做法。
 */
export function resolvePath(p) {
  if (!p) return p;
  if (process.platform === "linux") {
    // D:/x 或 D:\x → /mnt/d/x
    const m = /^([A-Za-z]):[\\/](.*)$/.exec(p);
    if (m) return `/mnt/${m[1].toLowerCase()}/${m[2].replace(/\\/g, "/")}`;
    return p;
  }
  // win32：/mnt/d/x → D:/x
  const m = /^\/mnt\/([a-z])\/(.*)$/.exec(p);
  if (m) return `${m[1].toUpperCase()}:/${m[2]}`;
  return p;
}

/**
 * 口令：`QQ_MCP_PASSPHRASE` 直接给，或 `QQ_MCP_PASSPHRASE_FILE` 指向文件。
 * 后者适合 `.env` 里不写明文口令（文件本身可以 chmod 600）。
 */
export function passphrase() {
  const inline = env("QQ_MCP_PASSPHRASE");
  if (inline) return inline;
  const file = resolvePath(env("QQ_MCP_PASSPHRASE_FILE"));
  if (file && existsSync(file)) {
    try {
      return readFileSync(file, "utf8").trim();
    } catch {
      return "";
    }
  }
  return "";
}

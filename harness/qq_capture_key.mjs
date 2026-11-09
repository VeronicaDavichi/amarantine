#!/usr/bin/env node
/**
 * 抓 qq-mcp 的设备口令并保存（spec 040 的 QQ 导入前置步骤）。
 *
 * ## 为什么要单独一个脚本
 *
 * `@powercess/qq-mcp` 的 `capture_offline_key` 要**读 QQ 进程内存**才能拿到
 * 设备口令。非管理员运行时 Windows 会拦住对其它进程的 `PROCESS_VM_READ`，
 * 表现就是：候选扫了一堆、全部解不开（实测 3754 个候选全失败）。
 *
 * 所以这个脚本要用**管理员 PowerShell** 跑一次：
 *
 *     # 管理员 PowerShell
 *     node E:\PycharmProjects\amarantine\harness\qq_capture_key.mjs
 *
 * 成功后口令写到 `%USERPROFILE%\.qq-mcp\qqkey`，之后 `export_qq.mjs` 直接用。
 *
 * ## 用法
 *
 *     node harness/qq_capture_key.mjs                    # 自动找 nt_msg.db
 *     node harness/qq_capture_key.mjs --dir "D:/TencentData/Chats/Tencent Files"
 *     node harness/qq_capture_key.mjs --db "<nt_msg.db>"
 *     node harness/qq_capture_key.mjs --verify           # 只校验已保存的口令
 */

import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";
import { existsSync, readFileSync, readdirSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

const PKG = process.env.QQ_MCP_PKG ?? "D:/nodejs/node_modules/@powercess/qq-mcp";
const DEFAULT_DIRS = [
  "D:/TencentData/Chats/Tencent Files",
  "C:/Users/Public/Documents/Tencent Files",
];

const require = createRequire(import.meta.url);
const resolveSdk = (sub) =>
  pathToFileURL(require.resolve(`@modelcontextprotocol/sdk/${sub}`, { paths: [PKG] })).href;

const { Client } = await import(resolveSdk("client/index.js"));
const { StdioClientTransport } = await import(resolveSdk("client/stdio.js"));

function parseArgs(argv) {
  const out = { dirs: [], db: "", verify: false };
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === "--dir") out.dirs.push(argv[++i]);
    else if (argv[i] === "--db") out.db = argv[++i];
    else if (argv[i] === "--verify") out.verify = true;
  }
  return out;
}

const transport = new StdioClientTransport({
  command: process.execPath,
  args: [`${PKG}/dist/index.js`],
  env: Object.fromEntries(Object.entries(process.env).filter(([, v]) => typeof v === "string")),
});
const client = new Client({ name: "amarantine-qq-key", version: "0.1.0" });
await client.connect(transport);

const call = async (name, args, timeout = 900000) => {
  const res = await client.callTool({ name, arguments: args }, undefined, { timeout });
  const text = res.content?.[0]?.text ?? "{}";
  try {
    return JSON.parse(text);
  } catch {
    return { raw: text };
  }
};

try {
  const args = parseArgs(process.argv.slice(2));
  const keyFile = join(homedir(), ".qq-mcp", "qqkey");

  if (args.verify) {
    if (!existsSync(keyFile)) {
      console.log(`没有已保存的口令：${keyFile}`);
      process.exit(1);
    }
    const passphrase = readFileSync(keyFile, "utf8").trim();
    const res = await call("verify_offline_key", { passphrase, all: true });
    console.log(JSON.stringify(res, null, 2).slice(0, 1500));
    process.exit(res.ok === false ? 1 : 0);
  }

  // 1) 找库。`list_qq_databases` 要的是 **nt_db 目录本身**，
  // 但用户手上通常是「Tencent Files」这种上层目录 —— 这里自己展开一层。
  const expandDirs = (roots) => {
    const out = [];
    for (const root of roots) {
      if (!existsSync(root)) continue;
      const direct = join(root, "nt_qq", "nt_db");
      if (existsSync(direct)) out.push(direct);
      for (const entry of readdirSync(root, { withFileTypes: true })) {
        if (!entry.isDirectory()) continue;
        const candidate = join(root, entry.name, "nt_qq", "nt_db");
        if (existsSync(candidate)) out.push(candidate);
      }
    }
    return out;
  };

  const dirs = expandDirs(args.dirs.length ? args.dirs : DEFAULT_DIRS);
  if (!dirs.length && !args.db) {
    console.log("没在默认目录里找到 nt_qq/nt_db。用 --dir 指定「Tencent Files」目录。");
    process.exit(1);
  }
  let target = args.db;
  if (!target) {
    const found = await call("list_qq_databases", { dirs });
    const dbs = (found.databases ?? []).filter((d) => d.name === "nt_msg.db");
    if (!dbs.length) {
      console.log("没找到 nt_msg.db。用 --dir 指定 QQ 数据目录。");
      console.log(JSON.stringify(found, null, 2).slice(0, 800));
      process.exit(1);
    }
    target = dbs.sort((a, b) => (b.bytes ?? 0) - (a.bytes ?? 0))[0].path;
  }
  console.log(`目标库：${target}`);

  // 2) 抓口令
  const cap = await call("capture_offline_key", { dbPath: target, save: true, maxCandidates: 8000 });
  console.log(JSON.stringify(cap, null, 2).slice(0, 1200));

  if (!cap.found) {
    console.log("\n✗ 没抓到口令。检查两件事：");
    console.log("  1) 是不是**管理员** PowerShell（非管理员读不到 QQ 进程内存）");
    console.log("  2) 当前登录的 QQ 是不是这个库所属的账号（口令按账号派生，换个账号进不去）");
    process.exit(1);
  }
  console.log(`\n✓ 口令已保存：${keyFile}`);
  console.log("下一步：node harness/export_qq.mjs --db \"<nt_msg.db>\" --list");
} finally {
  await client.close();
}

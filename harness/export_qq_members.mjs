#!/usr/bin/env node
/**
 * 导出群成员表（spec 047 T1）：`uid → 昵称/群名片`，供导入时解析发言人。
 *
 * ## 为什么要单独导
 *
 * `list_group_members` 能拿到 `card`（群名片），但**消息导出里没有它**。
 * 实测 1999 条消息里 1040 条 `senderName=undefined` —— 不导成员表，
 * 别人的群名片在导入时根本解析不出来。
 *
 * ## 用法
 *
 *     node harness/export_qq_members.mjs --group 485490399 --out-dir ../back_end/data/corpus/qq-identities
 *     node harness/export_qq_members.mjs --all --out-dir ...
 *
 * 产出：`<out-dir>/group-<gid>.json`
 *
 *     {"groupQq":"485490399","members":[{"uid":"u_...","qq":3883585327,
 *       "nickname":"潮间带","card":"往汐（北瑀家的鱼）"}]}
 *
 * 分类（处罚/空白/伪装/小号）由 Python 侧 `name_marker` 做，避免两边逻辑漂移。
 */

import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";
import { writeFileSync, mkdirSync } from "node:fs";

const PKG = process.env.QQ_MCP_PKG ?? "D:/nodejs/node_modules/@powercess/qq-mcp";
const DEFAULT_DB = "D:/TencentData/Chats/Tencent Files/505456846/nt_qq/nt_db/nt_msg.db";

const require = createRequire(import.meta.url);
const resolveSdk = (sub) =>
  pathToFileURL(require.resolve(`@modelcontextprotocol/sdk/${sub}`, { paths: [PKG] })).href;

const { Client } = await import(resolveSdk("client/index.js"));
const { StdioClientTransport } = await import(resolveSdk("client/stdio.js"));

function parseArgs(argv) {
  const out = { db: DEFAULT_DB, group: 0, all: false, outDir: "../back_end/data/corpus/qq-identities" };
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === "--db") out.db = argv[++i];
    else if (argv[i] === "--group") out.group = Number(argv[++i]);
    else if (argv[i] === "--all") out.all = true;
    else if (argv[i] === "--out-dir") out.outDir = argv[++i];
  }
  return out;
}

const transport = new StdioClientTransport({
  command: process.execPath,
  args: [`${PKG}/dist/index.js`],
  env: Object.fromEntries(Object.entries(process.env).filter(([, v]) => typeof v === "string")),
});
const client = new Client({ name: "amarantine-members", version: "0.1.0" });
await client.connect(transport);

const call = async (name, args) => {
  const res = await client.callTool({ name, arguments: args }, undefined, { timeout: 600000 });
  const text = res.content?.[0]?.text ?? "{}";
  try {
    return JSON.parse(text);
  } catch {
    return { raw: text };
  }
};

try {
  const args = parseArgs(process.argv.slice(2));
  mkdirSync(args.outDir, { recursive: true });

  let groupIds = [];
  if (args.all) {
    const contacts = await call("list_contacts", { dbPath: args.db, limit: 200 });
    groupIds = (contacts.groups ?? []).map((g) => Number(g.qq));
  } else if (args.group) {
    groupIds = [args.group];
  } else {
    throw new Error("需要 --group <gid> 或 --all");
  }

  let written = 0;
  for (const gid of groupIds) {
    try {
      const res = await call("list_group_members", { dbPath: args.db, groupQq: gid, limit: 500 });
      const members = (res.members ?? []).map((m) => ({
        uid: m.uid,
        qq: m.qq,
        nickname: m.nickname ?? "",
        card: m.card ?? "",
      }));
      const file = `${args.outDir}/group-${gid}.json`;
      writeFileSync(file, JSON.stringify({ groupQq: String(gid), members }, null, 1), "utf8");
      written += 1;
      console.log(`  ✓ ${gid}: ${members.length} 人 → ${file}`);
    } catch (err) {
      console.log(`  ✗ ${gid}: ${String(err.message).slice(0, 80)}`);
    }
  }
  console.log(`\n导出 ${written}/${groupIds.length} 个群`);
} finally {
  await client.close();
}

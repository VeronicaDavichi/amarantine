#!/usr/bin/env node
/**
 * 用 mcp-qq（@powercess/qq-mcp）把 QQ 本地库导成 amarantine 的语料格式（spec 040 的输入契约）。
 *
 * 为什么要这个脚本：
 *   mcp-qq 的 19 个工具是给 LLM 调用的，一次一个工具、返回一大段 JSON。
 *   「列出群 → 选群 → 导出消息 → 拼成 corpus」是多步流程，用 shell 拼 JSON 会碎，
 *   所以这里起一个 stdio MCP 会话，把多步串起来，直接落成 seed 脚本认得的文件。
 *
 * 用法：
 *   node harness/export_qq.mjs --db <nt_msg.db> --list
 *   node harness/export_qq.mjs --db <nt_msg.db> --group 123456 --out harness/corpus/qq-123456.json
 *   node harness/export_qq.mjs --db <nt_msg.db> --c2c --out harness/corpus/qq-c2c.json
 *   node harness/export_qq.mjs --db <nt_msg.db> --raw --group 123456     # 打印原始返回，便于对齐字段
 *
 * 依赖：全局装好的 @powercess/qq-mcp；口令在 ~/.qq-mcp/qqkey（或 QQ_MCP_PASSPHRASE）。
 */

import { writeFileSync, mkdirSync } from "node:fs";
import { dirname } from "node:path";
import { pathToFileURL } from "node:url";
import { createRequire } from "node:module";

const PKG =
  process.env.QQ_MCP_PKG ??
  "C:/Users/Administrator/AppData/Roaming/npm/node_modules/@powercess/qq-mcp";

// SDK 在 qq-mcp 自己的 node_modules 里；本仓库没有 node 依赖。
// 裸 specifier 从本文件解析不到，所以用 createRequire 带着 paths 去解析
// （这样 exports 映射也生效，不能拼死路径 —— SDK 的入口是 dist/esm/**）。
const require = createRequire(import.meta.url);
const resolveSdk = (sub) =>
  pathToFileURL(require.resolve(`@modelcontextprotocol/sdk/${sub}`, { paths: [PKG] })).href;

const { Client } = await import(resolveSdk("client/index.js"));
const { StdioClientTransport } = await import(resolveSdk("client/stdio.js"));

function parseArgs(argv) {
  const out = { limit: 5000 };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--list") out.list = true;
    else if (a === "--raw") out.raw = true;
    else if (a === "--c2c") out.c2c = true;
    else if (a === "--all-groups") out.allGroups = true;
    else if (a === "--out-dir") out.outDir = argv[++i];
    else if (a === "--db") out.db = argv[++i];
    else if (a === "--group") out.group = argv[++i];
    else if (a === "--out") out.out = argv[++i];
    else if (a === "--limit") out.limit = Number(argv[++i]);
    else if (a === "--keyword") out.keyword = argv[++i];
  }
  return out;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (!args.db) throw new Error("--db <nt_msg.db> 必填");

  const transport = new StdioClientTransport({
    command: process.execPath,
    args: [`${PKG}/dist/index.js`],
    env: Object.fromEntries(
      Object.entries(process.env).filter(([, v]) => typeof v === "string")
    ),
  });
  const client = new Client({ name: "amarantine-export", version: "0.1.0" });
  await client.connect(transport);

  const call = async (name, a) => {
    const res = await client.callTool({ name, arguments: a });
    const text = res.content?.[0]?.text ?? "{}";
    return JSON.parse(text);
  };

  try {
    // 一次导出全部群，每个群一个 corpus 文件（seed 脚本一次吃一个会话）
    if (args.allGroups) {
      if (!args.outDir) throw new Error("--all-groups 需要 --out-dir");
      const contacts = await call("list_contacts", { dbPath: args.db, limit: 500 });
      const names = new Map((contacts.groups ?? []).map((g) => [Number(g.qq), g.name]));

      const res = await call("query_chat_messages", {
        dbPath: args.db,
        chat: "group",
        limit: 5000,
        order: "asc",
      });
      const buckets = new Map();
      for (const m of res.messages ?? []) {
        const gid = Number(m.groupQq);
        if (!gid) continue;
        if (!buckets.has(gid)) buckets.set(gid, []);
        buckets.get(gid).push(m);
      }

      mkdirSync(args.outDir, { recursive: true });
      const summary = [];
      for (const [gid, msgs] of [...buckets.entries()].sort((a, b) => b[1].length - a[1].length)) {
        const corpus = toCorpus(msgs, {
          cid: `group_${gid}`,
          title: names.get(gid) ?? `QQ群 ${gid}`,
        });
        const file = `${args.outDir}/qq-${gid}.json`;
        writeFileSync(file, JSON.stringify(corpus, null, 2), "utf8");
        summary.push({ file, title: corpus.title, kept: corpus.messages.length });
      }
      console.log(JSON.stringify({ groups: summary.length, files: summary }, null, 2));
      return;
    }

    if (args.list || (!args.group && !args.c2c)) {
      const contacts = await call("list_contacts", { dbPath: args.db, limit: 500 });
      console.log(
        JSON.stringify(
          { groups: contacts.groups ?? [], friends: contacts.friends ?? [] },
          null,
          2
        )
      );
      return;
    }

    const query = {
      dbPath: args.db,
      limit: args.limit,
      order: "asc",
      chat: args.c2c ? "c2c" : "group",
    };
    if (args.group) query.groupQq = Number(args.group);

    const res = await call("query_chat_messages", query);
    if (args.raw) {
      console.log(JSON.stringify(res, null, 2));
      return;
    }

    const messages = res.messages ?? res.rows ?? [];
    const corpus = toCorpus(messages, {
      cid: args.c2c ? "c2c" : `group_${args.group}`,
      title: args.c2c ? "QQ 私聊" : `QQ 群 ${args.group}`,
    });

    if (!args.out) {
      console.log(JSON.stringify({ total: messages.length, kept: corpus.messages.length }));
      return;
    }
    mkdirSync(dirname(args.out), { recursive: true });
    writeFileSync(args.out, JSON.stringify(corpus, null, 2), "utf8");
    console.log(
      JSON.stringify({
        out: args.out,
        cid: corpus.cid,
        kept: corpus.messages.length,
        roster: corpus._roster.length,
      })
    );
  } finally {
    await client.close();
  }
}

/** mcp-qq 的消息 → seed 脚本的 corpus 格式（与 seed_dingtalk 同构）。 */
function toCorpus(messages, { cid, title }) {
  const out = [];
  const roster = new Set();
  for (const m of messages) {
    // mcp-qq 已经把多段合并成 `text` 了；只有它为空时才回头拼 segments
    let text = typeof m.text === "string" ? m.text.trim() : "";
    if (!text) {
      const segments = Array.isArray(m.segments) ? m.segments : [];
      text = segments
        .filter((s) => s && s.type === "text")
        .map((s) => s.text ?? s.content ?? "")
        .join(" ")
        .trim();
    }
    if (!text) continue;
    const sender = m.senderName ?? m.senderQq ?? "unknown";
    if (typeof sender === "string" && sender.length >= 2 && sender.length <= 12) {
      roster.add(sender);
    }
    out.push({
      type: 1,
      text,
      ts: m.iso ?? (m.timestamp ? new Date(Number(m.timestamp) * 1000).toISOString() : null),
      sender,
    });
  }
  return { cid, title, messages: out, _roster: [...roster] };
}

await main();

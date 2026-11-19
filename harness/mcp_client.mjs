// MCP 客户端薄封装：**按能力解析工具名**，换实现只改配置（spec 050 延伸）
//
// 背景：harness 下几个脚本硬编码了 `list_contacts` / `query_chat_messages` 这些
// 工具名。换一个 MCP 实现（qq-mcp → im-mcp）时名字可能不同，过去必须改脚本。
// 现在：脚本只声明「我要什么能力」，名字由这里按别名表解析。
//
// 用法：
//   import { connect, capability, callCap, close } from "./mcp_client.mjs";
//   await connect();                 // 拉起 MCP 子进程 + initialize + tools/list
//   await callCap("list_contacts", { dbPath, limit: 50 });

// 说明：本模块**不 import SDK** —— `@modelcontextprotocol/sdk` 装在 MCP 实现包自己的
// node_modules 里，解析方式（createRequire + paths）只有调用方知道。
// 这里只用 `attach(client)` 拿一个已经连上的实例。
let client = null;
let toolNames = [];
let aliasMap = {};

/** 能力 → 候选工具名（按优先级）。新增实现时往这里加别名即可。 */
export const CAPABILITY_ALIASES = {
  list_databases: ["list_qq_databases", "list_databases", "listDatabases"],
  list_contacts: ["list_contacts", "listContacts", "get_contacts", "list_friends"],
  list_group_members: [
    "list_group_members",
    "listGroupMembers",
    "get_group_members",
    "list_members",
  ],
  query_messages: [
    "query_chat_messages",
    "queryChatMessages",
    "get_chat_messages",
    "query_messages",
  ],
  search_messages: [
    "search_messages_fts",
    "searchMessages",
    "search_messages",
    "search_fts",
  ],
  capture_key: [
    "capture_offline_key",
    "captureOfflineKey",
    "capture_key",
  ],
  verify_key: [
    "verify_offline_key",
    "verifyOfflineKey",
    "verify_key",
  ],
};

/** 已有的 SDK Client 直接注入（避免本模块依赖具体解析方式）。 */
export function attach(c) {
  client = c;
}

export async function loadTools() {
  const res = await client.listTools();
  toolNames = (res.tools ?? []).map((t) => t.name);
  return toolNames;
}

export function availableTools() {
  return [...toolNames];
}

/** 解析某能力对应的工具名；找不到返回 null。 */
export function capability(cap) {
  const want = CAPABILITY_ALIASES[cap];
  if (!want) throw new Error(`未知能力：${cap}`);
  for (const name of want) {
    if (toolNames.includes(name)) return name;
  }
  return null;
}

/**
 * 按能力调用工具。
 *
 * 找不到时报错并**列出实际可用工具**——换实现时这行错误就是最直接的线索。
 */
export async function callCap(cap, args, { timeout = 900000 } = {}) {
  let name = capability(cap);
  if (!name) {
    throw new Error(
      `MCP 实现缺少能力 ${cap}（候选名：${CAPABILITY_ALIASES[cap].join(", ")}）。` +
        `实际可用：${toolNames.join(", ") || "（没拿到 tools/list）"}`,
    );
  }
  const res = await client.callTool({ name, arguments: args }, undefined, { timeout });
  const text = res?.content?.[0]?.text ?? "";
  try {
    return JSON.parse(text);
  } catch {
    return { raw: text, structured: res?.structuredContent ?? null };
  }
}

export async function close() {
  if (client) await client.close();
  client = null;
}

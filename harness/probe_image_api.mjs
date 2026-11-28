// 问 qq-mcp：图片类消息能不能拿到本地路径/URL？
// 这决定「让 LLM 看图片」在数据层面是否可行。
import { env, childEnv, passphrase, resolvePath } from "./load_env.mjs";
import { attach, callCap, loadTools } from "./mcp_client.mjs";
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";

const PKG = resolvePath(env("QQ_MCP_PKG"));
const require = createRequire(import.meta.url);
const resolveSdk = (sub) =>
  pathToFileURL(require.resolve(`@modelcontextprotocol/sdk/${sub}`, { paths: [PKG] })).href;

const { Client } = await import(resolveSdk("client/index.js"));
const { StdioClientTransport } = await import(resolveSdk("client/stdio.js"));

const client = new Client({ name: "img-probe", version: "0.1.0" });
const transport = new StdioClientTransport({
  command: "node",
  args: [`${PKG}/dist/index.js`],
  env: childEnv(passphrase() ? { QQ_MCP_PASSPHRASE: passphrase() } : {}),
});
await client.connect(transport);
attach(client);
const tools = await loadTools();

console.log("=== qq-mcp 工具里和图片/文件相关的 ===");
for (const t of tools) {
  if (/img|image|pic|file|media/i.test(t)) console.log("  -", t);
}

const dbPath = resolvePath(env("QQ_NT_MSG_DB"));
console.log("\n=== 试 query_files（看能否返回图片路径）===");
try {
  const r = await callCap("query_files", { dbPath, limit: 3 });
  console.log(JSON.stringify(r, null, 1).slice(0, 1200));
} catch (e) {
  console.log("  ✗", String(e.message).slice(0, 200));
}

await client.close();

// 验证：resolvePath 跨平台 + 口令文件读取
import { env, passphrase, resolvePath } from "./load_env.mjs";
import { existsSync } from "node:fs";

const cases = [
  "D:/nodejs/node_modules/@powercess/qq-mcp",
  "/mnt/d/nodejs/node_modules/@powercess/qq-mcp",
  "relative/path",
  "",
];
console.log("platform:", process.platform);
for (const c of cases) {
  console.log(`  ${JSON.stringify(c).padEnd(52)} → ${resolvePath(c)}`);
}

const pf = resolvePath(env("QQ_MCP_PASSPHRASE_FILE"));
console.log("\n口令文件:", pf || "（未配置）");
console.log("文件存在:", pf ? existsSync(pf) : false);
const pw = passphrase();
console.log("口令已读取:", pw ? `是（长度 ${pw.length}）` : "否");

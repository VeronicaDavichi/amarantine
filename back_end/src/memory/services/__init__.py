"""服务层（编排外部依赖）。

入口层（MCP / Admin HTTP）只做协议适配，业务编排放在这里；
外部依赖（Weaviate / Neo4j / embedding / LLM）各自单独一个模块，便于 mock 测试。
"""

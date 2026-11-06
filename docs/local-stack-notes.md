# 本地中间件运维笔记

记录 P0 中间件在本机（Windows + WSL2）跑起来时踩过的坑，避免重复排查。

> 环境：WSL2 Ubuntu 22.04 / Docker 29.5.3 / 宿主 i7-1360P（Raptor Lake，**无 AVX512**）

## 端口分配

| 服务 | 宿主端口 | 容器端口 | 备注 |
|---|---|---|---|
| Weaviate REST | 8081 | 8080 | 8080 被同机的 Dify weaviate 占用 |
| Weaviate gRPC | **50061** | 50051 | 50051 被 Dify weaviate 占用 |
| Embedding | 8082 | 80 | |
| Neo4j HTTP | 7474 | 7474 | |
| Neo4j Bolt | 7687 | 7687 | |

**教训**：本机还跑着一套 Dify（`docker-*` 前缀容器），端口 `8080` / `50051` 已被占。
`docker compose up` 报 `Bind for 0.0.0.0:50051 failed: port is already allocated` 就是这个原因。

## 镜像与模型来源

国内网络下 `hf-mirror.com` 与 `ghcr.io` 都不稳定，实测对比：

| 源 | 速度 | 备注 |
|---|---|---|
| hf-mirror 大文件 | ~44 KB/s | 2.27 GB 需 14 小时，不可用 |
| **ModelScope** | **~4.7 MB/s** | 2.27 GB 约 8 分钟，首选 |

镜像方面：

- Weaviate 用 Docker Hub 的 `semitechnologies/weaviate:1.28.4`
  （`cr.weaviate.io/...` 在国内常 `i/o timeout`）
- TEI 用 `infiniflow/text-embeddings-inference:cpu-1.8`
  （`ghcr.io/...` 拉不动时可用；1.7 与本机 CPU 不兼容，见下）

## bge-m3 只有 pytorch_model.bin

`BAAI/bge-m3` 仓库**不提供** `model.safetensors`，只有 `pytorch_model.bin`（2.17 GB）。
而 TEI（Rust/candle 后端）只加载 safetensors，必须先转换：

```bash
# back_end 目录下
uv pip install torch safetensors numpy
.venv\Scripts\python.exe ..\harness\convert_to_safetensors.py
```

**三个坑**：

1. **必须用 mmap 加载**。本机可用内存常只有 2~3 GB，`torch.load` 全量读入会被 OOM 杀掉。
   `torch.load(..., mmap=True, weights_only=True)` 按需映射，峰值内存降到几百 MB。
2. **不能用 `uv run`**。`uv run` 会按 `pyproject.toml` 同步环境，把手工装的
   `torch` / `numpy` 卸掉，报 `No module named 'numpy'`。改用 `.venv\Scripts\python.exe` 直接跑。
3. **转换前确认 bin 是 zip 格式**（前 4 字节 `50 4B 03 04`），否则 mmap 无效。

## TEI 的 Candle 后端在本机崩溃

现象：容器反复重启（`RestartCount` 增长），日志停在
`INFO ... Warming up model` 之后没有下文，`ExitCode=0`、`OOMKilled=false`，
中间夹杂大量 `Intel MKL ERROR: Parameter 13 was incorrect on entry to SGEMM`。

原因：**i7-1360P 不支持 AVX512**（Intel 12 代起消费级禁用），而 TEI 的
Candle + MKL kernel 按 AVX512 编译，warmup 时触发 SIGILL。

解决：走 **ORT（onnxruntime）后端**——它会按 CPU 实际能力选择 kernel。

```bash
bash harness/fetch-onnx-modelscope.sh
```

ONNX 文件放到 `models/bge-m3/onnx/` 后，TEI 会自动优先尝试 ORT 后端
（日志里原先的 `Could not start ORT backend` 错误消失，转而直接加载）。

## 权重文件现状：embedding 只保留 ONNX（2026-11-06）

本机的 `models/bge-m3/` 现在**只有 ONNX 权重**，`pytorch_model.bin` 和
`model.safetensors` 都已删除，共省 4.4 GB。

### 为什么能删

TEI 的加载优先级是「有 `onnx/` 就走 ORT，否则走 Candle（safetensors）」。
本机因为 AVX512 问题**必须**走 ORT，所以那两份 PyTorch 权重永远轮不到。

**实测验证过，不是推测**：

```
1. 存基线向量（2 条 × 1024 维）
2. 把 model.safetensors 改名藏起来
3. 重启容器 -> 仍然 healthy
4. 重新算向量 -> 与基线逐位相同
5. 日志无任何 error / not found
```

结论：safetensors 在这台机器上确实没被加载过。

### 换机器怎么办

如果搬到**支持 AVX512 的机器**（Intel 10 代及以前的部分型号、AMD Zen4+），
TEI 会优先走 Candle 后端，那时需要：

```bash
# 从 HuggingFace 重新拉 safetensors
huggingface-cli download BAAI/bge-m3 --local-dir models/bge-m3 \
    --include "*.safetensors" "*.json" "*.model" "1_Pooling/*"
```

或者继续用 ORT（只要有 `onnx/` 目录，TEI 就会走 ORT，与 CPU 无关）。

**简单判据**：容器日志出现 `Intel MKL ERROR ... SGEMM` 或反复重启
停在 `Warming up model` → 是 AVX512 问题 → 需要 `onnx/`。

### reranker 是唯一副本，不能删

`models/bge-reranker-v2-m3/` 只有 `model.safetensors`（2.2 GB），**没有 onnx**。
它只有一个权重文件，删了服务直接挂 —— 和 embedding 的情况完全不同。

（它能正常跑说明 reranker 的模型结构 / 算子路径没触发 AVX512 的问题。）

### 总占用

| 目录 | 大小 | 说明 |
|---|---|---|
| `bge-m3` | 2.2 G | 只有 `onnx/`（`model.onnx` 725 KB + `model.onnx_data` 2.2 G） |
| `bge-reranker-v2-m3` | 2.2 G | 只有 `model.safetensors`（唯一副本） |
| **合计** | **4.3 G** | 清理前 8.6 G |

`models/` 在 `.gitignore` 里，这些是本地产物，不入库。

## ORT 后端的两个必调参数

即便走 ORT，默认参数在本机也会把内存打爆——实测容器内存冲到 **12.34 GiB**，
WSL 只剩 214 MB，内核直接 OOM 杀进程（此时 `docker inspect` 的 `OOMKilled=false`，
`ExitCode=0`，很容易误判成"指令集问题"）。

`docker-compose.yml` 里必须调小：

```yaml
      - "--max-batch-tokens"
      - "2048"          # 默认 16384，是内存爆炸的主因
      - "--max-client-batch-size"
      - "4"             # 默认 16
      - "--tokenization-workers"
      - "4"             # 默认 16
```

调后稳定在 **2.54 GiB**，P0 负载（每次嵌入几条记忆）完全够用。

启动成功的标志：

```
INFO text_embeddings_router::http::server: Starting HTTP server: 0.0.0.0:80
INFO text_embeddings_router::http::server: Ready
```

## embedding 接口格式（TEI 1.8）

**注意字段名是 `input`，不是 `inputs`**（1.7 是 `inputs`，1.8 改成了 OpenAI 兼容格式）：

```bash
curl -s -X POST http://127.0.0.1:8082/v1/embeddings \
  -H "Content-Type: application/json" \
  -d '{"input":"要编码的文本","model":"bge-m3"}'
```

响应：

```json
{"object":"list","data":[{"object":"embedding","embedding":[...1024 个 float...]}]}
```

`/health` 返回 **200 + 空 body**，这是正常的（不是故障）。

实测单条嵌入约 **0.2 秒**。

## 健康检查

- `semitechnologies/weaviate` 镜像**没有 curl**，healthcheck 必须用 `wget`
  （镜像内有 `/usr/bin/wget`），否则会一直 `unhealthy` 但其实服务正常。
- `neo4j` 镜像用 `wget` 同样可行。

## 模型目录结构（挂载到容器 `/models/bge-m3`）

```
models/bge-m3/
  config.json                     # 必需
  tokenizer.json                  # 必需
  tokenizer_config.json
  special_tokens_map.json
  sentencepiece.bpe.model
  modules.json
  config_sentence_transformers.json
  sentence_bert_config.json
  1_Pooling/config.json
  colbert_linear.pt
  sparse_linear.pt
  pytorch_model.bin               # 原始权重（转换后可删）
  model.safetensors               # 转换产物
  onnx/                           # ORT 后端用
    model.onnx
    model.onnx_data
    config.json
    tokenizer.json
    ...
```

`models/` 已在 `.gitignore` 中排除（约 6.5 GB）。

## 后端跑在 WSL 里（不在 Windows）

**结论：代码用 WSL 跑。** Windows 那边的坑（gRPC 转发、CRLF、编码、路径分隔符）
一个都不想过第二遍。

### 一次性准备

```bash
# 在 WSL 内执行
export PATH="$HOME/.local/bin:$PATH"
cd /mnt/e/PycharmProjects/amarantine/back_end
export UV_PROJECT_ENVIRONMENT=.venv-linux   # 独立 venv，不碰 Windows 的 .venv
uv sync
```

- `uv` 自带 Python 管理：`uv python install 3.12`（Ubuntu 22.04 自带 3.10，不满足 `>=3.12`）
- **必须用 `.venv-linux`**：Windows 的 `.venv` 里是 `Scripts/`，Linux 要 `bin/`，
  同一个目录不能混用（`uv sync` 会直接报错或装出诡异状态）

### 日常用法

```bash
# 仓库根目录
./harness/dev-linux.sh pytest -q
./harness/dev-linux.sh python -c "from memory.config import load_settings; print(load_settings().WEAVIATE_URL)"
```

脚本负责三件事：`PATH` 加 `~/.local/bin`、切到 `back_end/`、指定 `.venv-linux`。

### 性能：`/mnt/e` 是 drvfs，冷启动慢约 10 倍

实测 `recall()`（真实容器）：

| 次数 | 耗时 |
|---|---|
| 第 1 次（冷） | **1456 ms** |
| 第 2 次（热） | 162 ms |
| 第 3 次（热） | 179 ms |

**影响**：冷启动那一下逼近 `RECALL_BUDGET_MS=1500`，会误报 `partial=true`。
**不是 bug**：MCP stdio 是长驻进程，只在启动时冷一次；HTTP admin 同理。

若想让开发调试也快，可把仓库放进 WSL 原生目录（`~/amarantine`），
但那样 git 会变两份、容易漂移 —— **默认留在 `/mnt/e`，接受冷启动**。

### 端口在 WSL 里怎么写

Docker 端口映射到 WSL 的 `0.0.0.0`，所以 **WSL 内访问 `127.0.0.1:8081` 直接通**，
不需要 `host.docker.internal`，也不需要 WSL 的宿主 IP。
`.env` 里的地址可以照抄 Windows 的写法。


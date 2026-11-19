# OCI Layer Tools

两个面向隔离网（air-gapped）搬运的 OCI 镜像工具，通过同一个 FastAPI 服务提供：

1. `POST /merge` —— 把两个连续的、**未压缩的** OCI 差异层合并成一个可复用的差异层（原有功能）。
2. `POST /repack` —— 把 OCI Image Layout 归档重打包成适合用移动介质运送的形式：
   **未压缩层流式转 gzip，共用层只压一次**，各平台镜像清单与顶层索引同步更新，
   去掉已无引用的原层。

所有产物只在完整校验通过后才生成并可下载；任何校验失败都整包拒绝（HTTP 400），
不提供部分产物。

## `/repack` 做了什么

输入是一个未压缩 tar，根目录下是标准 [OCI Image Layout](https://github.com/opencontainers/image-spec/blob/main/image-layout.md)
（`oci-layout` + `index.json` + `blobs/sha256/*`），也接受归档内唯一的顶层目录。

处理流程：

1. **安全解包**：只接受普通文件与目录；绝对路径、`..`、反斜杠/NUL、符号链接、
   硬链接、设备节点、FIFO、重复成员、截断归档一律拒绝，解包不会写到临时目录之外。
2. **校验 `oci-layout` / `index.json`**：版本必须是 `1.0.0`，`schemaVersion` 必须是 2。
3. **展开顶层索引**：顶层 `index.json` 若指向嵌套索引（多平台布局的常见形式），
   递归展开到各平台镜像清单；平台信息、`org.opencontainers.image.ref.name` 标签
   以及其它 annotations 从父级索引向下继承。循环引用和过深嵌套会被拒绝。
4. **逐个镜像清单重写**：
   - 未压缩层（`…layer.v1.tar`、`…diff.tar` 等）：按 1 MiB 分块流式压缩成 gzip
     （level 9），压缩后立即**流式解压回读并与原 blob 逐字节比对**，不一致即整包失败；
   - 已是 gzip 的层：原样复制（blob 自校验，声明的 `+gzip` 必须真的是 gzip 头）；
   - zstd / 其它不支持的媒体类型：整包失败；
   - 共用的层按源摘要缓存，**只压缩一次**，多个平台复用同一个新 blob；
   - 每个 layer descriptor 的 `mediaType`（`+gzip`）、`size`、`digest` 同步更新；
   - 镜像配置（config blob）原样保留，不做任何改动。
5. **生成新清单**：规范化 JSON 后重新计算 manifest 摘要与大小；索引描述符只保留
   `mediaType` / `digest` / `size`，并在已知时带上 `platform`（来自父索引，缺失时
   从 config 的 `architecture` / `os` / `variant` 推断）与标签 annotations。
6. **扁平化顶层索引**：输出 `index.json` 的 `manifests` 直接列出各平台镜像清单，
   不再包含嵌套索引。
7. **收集输出 blob**：只写出从新索引可达的 blob（新 gzip 层、新清单、config、
   原样保留的 gzip 层）。已无引用的未压缩原层、旧清单、嵌套索引全部丢弃。

输出仍是一个未压缩 tar 的 OCI Image Layout。解包后，任何按 `index.json` 解析的
导入工具都能按平台取镜像；由于不同平台的清单描述符带有 `platform`，也可以用
清单 digest 精确指定：

```bash
tar xf repacked-layout.tar -C layout
# 用清单 digest 导入某个平台（regctl / go-containerregistry）
regctl image export "ocidir://$PWD/layout@sha256:<manifest-digest>" rootfs.tar
# 或用标签（标签保留在 index.json 的 annotation 上）
skopeo copy oci:layout:demo:latest docker-archive:demo.tar
```

## `/merge` 做了什么

把两个连续的未压缩差异层合并成一个，语义等价于依次应用两层（whiteout、opaque
目录标记、文件↔目录互换均已处理）。详见下方“支持的 tar 成员”。

## 运行

### Docker Compose（推荐）

```bash
REPACK_TMPFS_SIZE=8g docker compose up --build
# 打开 http://localhost:8000/docs
#   /repack：上传 layout tar，下载 repacked-layout.tar
#   /merge ：上传 lower 与 upper 两个 tar，下载 merged-layer.tar
```

`docker-compose.yaml` 以只读根文件系统、tmpfs `/tmp`、非 root 用户和
`no-new-privileges` 运行，内置 `/health` 健康检查。请求工作目录（上传 spool、
解包后的布局、重新压缩的层、输出归档）都在 `/tmp` 下，可通过环境变量调整：

| 变量 | 默认 | 含义 |
| --- | --- | --- |
| `REPACK_TMPFS_SIZE` | `8g` | Compose 中 `/tmp` 的大小 |
| `REPACK_MAX_UPLOAD_BYTES` | `4294967296`（4 GiB） | `/repack` 单个上传上限 |
| `REPACK_GZIP_LEVEL` | `9` | 新 gzip 层的压缩级别（1–9） |

`REPACK_MAX_UPLOAD_BYTES` 必须小于 `REPACK_TMPFS_SIZE`，否则 413 之前就会先写满 tmpfs。

### 本地直接运行

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## API

- `GET /health` —— 存活探针，`{"status":"ok"}`
- `GET /docs` —— Swagger UI，可直接上传/下载
- `POST /merge` —— `multipart/form-data`：`lower`、`upper` 两个未压缩 tar
  - `200 application/x-tar`（`merged-layer.tar`），否则 `400`
- `POST /repack` —— `multipart/form-data`：`layout` 一个未压缩的 OCI Image Layout tar
  - `200 application/x-tar`（`repacked-layout.tar`）
  - `400 application/json`：缺少引用 blob、摘要/大小不符、tar 截断或损坏、
    不支持的成员/媒体类型、压缩回读不一致等，均无下载产物
  - `413`：上传超过 4 GiB；`422`：缺字段
- 单次上传上限：`/merge` 每层 2 GiB，`/repack` 4 GiB。

失败时响应形如：

```json
{"detail": "index.json manifest 0 child 0 layer 2: referenced blob sha256:… is missing (expected at blobs/sha256/…)"}
```

## `/merge` 支持的 tar 成员

| 成员 | 处理方式 |
| --- | --- |
| 普通文件 | 内容落盘到请求级临时目录，原样保留 mode/uid/gid/mtime 等元数据 |
| 目录 | 保留元数据；在“文件↔目录互换”、“删除后重建”时自动加 opaque 标记 |
| whiteout（`.wh.<name>`） | 删除同名子树；若在合并结果中仍需遮蔽下层则保留为 whiteout |
| opaque 目录标记（`.wh..wh..opq`） | 清除该目录下来自更低层的内容，同层条目始终保留 |

符号链接、硬链接、设备节点、FIFO 等其它成员类型一律拒绝（HTTP 400）。压缩 tar
（gzip/bzip2/xz/zstd，按魔数识别）、绝对路径、含 `..` 的路径、反斜杠/NUL 字符、
非零长度 whiteout、截断或损坏的 tar 同样拒绝。

### 为什么合并层能在“任意下层”上等价

合并器在内存里维护一棵带 `origin`（来自第一层还是第二层）与 `opaque` 标记的树：

1. 顺序应用两个输入层，whiteout 删除子树，opaque 仅清除更低 `origin` 的节点；
2. 当一个目录建在“曾经是文件/被 whiteout 的目录”的位置时，强制打 opaque 标记，
   保证下层目录里可能存在的子项在任何基础层上都被隐藏；
3. 输出时，仍需遮蔽下层的路径输出 `.wh.` whiteout；opaque 目录输出
   `.wh..wh..opq`；其余按确定性顺序（目录、标记、文件）写出。

## 测试

```bash
pip install -r requirements.txt pytest httpx
python -m pytest
```

- `tests/test_merger.py`：独立参考叠加器（与生产代码结构不同）在两个不同下层上
  逐场景比较 `base+lower+upper` 与 `base+merged`。
- `tests/test_layout.py`：重打包的单元/集成测试，包括
  - 扁平化索引、平台与标签保留；
  - 层 media type / size / digest 更新且与新 blob 一致；
  - 共用层只产生一个 gzip blob，解压后与原层逐字节一致；
  - 已有 gzip 层原样保留；
  - 原未压缩层、无关 blob 不再出现；
  - 缺失 blob、摘要/大小不符、损坏 JSON、缺失 `oci-layout`/`index.json`、
    路径穿越、符号链接、非 tar、截断、不支持的层类型、过深嵌套索引等
    全部返回错误且无产物；
  - HTTP 层的 200/400/422 行为与 `/docs`、`/openapi.json`。

针对已部署服务的端到端脚本（仅标准库，可用 `python -I` 运行）：

```bash
python -I scripts/selftest.py         --base-url http://127.0.0.1:8000   # /merge
python -I scripts/selftest_repack.py  --base-url http://127.0.0.1:8000   # /repack
```

`selftest_repack.py` 构造含**两个平台共享层**和已有 gzip 层的布局，验证索引扁平化、
平台/标签、共享层单 blob、逐字节还原、原层剔除、按索引导入各平台，以及缺失 blob
时返回 400 且无下载。

## 已知限制

- `/repack` 只支持 `sha256` 摘要；只处理常规镜像清单（`schemaVersion: 2`），
  不接受带 `subject` 的清单、zstd 层、Docker schema1 清单。
- 顶层索引中若有指向非索引/非镜像清单的 descriptor（如 artifacts），整包拒绝。
- `/merge` 只依据两个输入 tar 的可见信息决定 opaque 标记；输入层只含普通目录成员
  而某个未知下层在同路径上恰好是普通文件时，单差异层模型本身不可判定。
- `/merge` 仅接受未压缩 tar；不支持符号链接、硬链接、设备节点、FIFO。
- 处理全程在请求级临时目录（Compose 下为 tmpfs）中进行，需要的空间约为
  “上传归档 + 解包布局 + 压缩层 + 输出归档”之和。

## 目录结构

```
app/
  merger.py   # 差异层合并逻辑（无 Web 依赖，可单独复用）
  layout.py   # OCI Image Layout 解包/校验/重打包（无 Web 依赖）
  main.py     # FastAPI 路由、上传落盘、错误处理
tests/        # pytest 单元与集成测试
scripts/      # 针对运行中服务的独立自测脚本
Dockerfile
docker-compose.yaml
requirements.txt
```

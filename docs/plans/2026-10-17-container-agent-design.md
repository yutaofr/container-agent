# Container Agent 设计文档

- 日期：2026-10-17
- 状态：设计已确认，待实现（POC）

## 1. 目标

为开发、测试和架构师提供一个 AI agent：用户描述验证目的，agent 用 docker compose 在用户本机搭建 cluster，自动排错，直到 cluster 完全可用。

核心约束：

- 用户未说明的 compose 要素，由 agent 主动询问并给出默认值。
- compose YAML 生成后必须经用户确认才能启动。
- 启动后 agent 自动监控、诊断、修复，直到通过分层验收。
- 覆盖公司所有项目。服务来源包括已发布二进制包、snapshot、CI 构建产物和本地工作目录。
- 模型可选 AWS Bedrock 或自托管开源模型；全自动时按子任务复杂度选模型，用户可自定义。
- 平台提供 Web 界面，cluster 跑在用户本机。

## 2. 关键决策

| 问题 | 决策 |
|---|---|
| 交付形态 | 内部平台提供 Web；cluster 跑在用户本机 |
| 平台与本机分工 | 平台负责推理，本机 Runner 负责执行；Runner 主动外连，只执行白名单工具 |
| 项目知识来源 | 以 `.cagent/service.yaml` 登记为主，未登记时现场探查，跑通后提议生成描述文件 |
| 自愈边界 | 只改环境，不改源码；根因在源码时停下来给出诊断 |
| 验收标准 | 分层验收（L1–L4）加稳定期 |
| 制品来源 | Nexus / Artifactory、CI 构建产物；没有现成镜像，需本地构建 |
| 交付后职责 | 用规则轻量守护、cluster 管理、配方复用 |
| 架构方案 | 确定性状态机加受限 LLM 节点 |
| 技术栈 | 平台 Python（LangGraph + LiteLLM），Runner Go（compose-go + Docker SDK） |
| 模型部署 | POC 可用本机 Ollama；上线后统一经 LiteLLM gateway |

**为什么选状态机，而不是单一自主 agent。** 审批、只改环境、预算、守护期不调用模型，这些都是硬规则，必须写在代码里，不能只写在提示词里。状态机也让小模型只负责它能做好的窄任务。

**为什么 Runner 用 Go。** `compose-go` 是 docker compose 官方使用的 Compose Spec 解析和校验库。Runner 能在启动前用与 Docker 相同的语义校验 YAML，省掉一轮"启动 → 报错 → LLM 修复"。Go 还能交叉编译出单个二进制文件，分发简单。

## 3. 架构

```
┌──────────────────── 内部平台 ─────────────────────┐        ┌────── 用户本机 ──────┐
│ Web UI ──► Orchestrator (Python + LangGraph)       │        │ Runner (Go 单个二进制)  │
│   对话/审批/    │  每个 cluster 一个 run，            │  WSS   │  ├ 工具白名单执行器      │
│   状态/日志    │  状态存入 Postgres                  │◄──────►│  ├ compose-go 校验       │
│                ├─► Model Gateway (LiteLLM)          │ Runner │  ├ 制品获取（本机凭证）  │
│                │     ├ Bedrock                      │ 主动外连 │  ├ 构建镜像（builder 模板） │
│                │     └ vLLM/Ollama 开源小模型        │        │  ├ 守护规则引擎          │
│                ├─► Catalog Service                  │        │  └ Docker Engine        │
│                │     项目描述 / 中间件模板 / 配方     │        └─────────────────────┘
│                └─► Runner Hub（连接管理 + 工具 RPC）  │
└────────────────────────────────────────────────────┘
```

### 3.1 平台侧（Python）

- **Web UI**：对话窗口、YAML diff 审批、cluster 状态面板、实时日志。
- **Orchestrator**：LangGraph 状态机，每个 cluster 任务一次运行。checkpointer 把状态存入 Postgres，平台重启或用户关闭网页后都能继续；等待审批时停在 interrupt 处。
- **Model Gateway**：LiteLLM 统一对接 Bedrock 和自托管模型，负责路由、用量和成本统计。
- **Catalog Service**：收集各仓库的 `.cagent/service.yaml`，维护中间件模板，保存用户分享的配方。
- **Runner Hub**：管理 Runner 的 WebSocket 长连接，把工具调用转成发给 Runner 的 RPC。

### 3.2 本机侧（Go Runner）

- 不开放任何端口。凭平台签发的 token 主动连接平台，token 绑定用户和设备。
- 只执行固定白名单里的工具，不提供任意 shell。
- 每个 cluster 一个隔离的工作目录：`~/.cagent/clusters/<name>/`，存放 compose 文件、生成的 Dockerfile 和调优记录。
- Nexus 和 CI 的凭证只从本机读取（`~/.m2/settings.xml`、`.npmrc`、CI token），不上传平台。

### 3.3 POC 与上线的差异

Orchestrator 只调用 LiteLLM。从 POC 切换到上线只改 gateway 配置，不改代码。

- **POC**：平台、Runner、LiteLLM 在同一台开发机上用 compose 运行；small 档指向本机 Ollama，large 档指向 Bedrock。
- **上线**：平台部署到内部环境，LiteLLM 指向内部 GPU 集群上的 vLLM 和 Bedrock，Runner 连接远程平台。

## 4. 工作流状态机

所有阶段围绕 **ClusterSpec**（结构化的 cluster 意图）展开。对话只负责把 ClusterSpec 填完整，compose YAML 由 ClusterSpec 生成。

| # | 阶段 | 内容 | 执行者 |
|---|---|---|---|
| 1 | 澄清 | 提取用途和服务列表，标注每个服务的来源（`release:1.4.2`、`snapshot:2.0-SNAPSHOT`、`workdir:/path`、`ci:job#123`）。对照描述文件的必填项逐个追问，每个问题附默认值 | LLM（large）+ 描述文件 |
| 2 | 生成 | ClusterSpec → compose YAML、Dockerfile、构建计划。已登记服务走模板；未登记服务由 LLM 探查补全 | 模板为主，LLM 补缺 |
| 3 | 预校验 | compose-go 校验，检查端口冲突、镜像架构、本机剩余资源、安全规则 | 规则 |
| 4 | 审批 | 展示 YAML、Dockerfile、构建计划；用户通过、直接修改或驳回并附意见（回到第 2 步） | 人 |
| 5 | 准备 | 获取制品，构建镜像 | Runner |
| 6 | 启动 | `docker compose up -d` | Runner |
| 7 | 验收 | L1 容器运行 → L2 健康检查 → L3 冒烟探针 → L4 用户自定义验证 → 稳定期（默认 2 分钟内没有重启） | 规则 + 探针 |
| 8 | 诊断修复 | 任一步失败即进入。先规则后 LLM；修复后按改动范围回到第 5 或第 6 步 | 规则 → LLM |
| 9 | 交付 | 输出访问地址、账号、调优记录；可保存为配方，或把修复后的配置回写描述文件 | 模板 |
| 10 | 守护 | 用规则监控，异常时通知用户，由用户决定是否重新进入自愈 | Runner 规则 |

自动修复回到第 5 或第 6 步，不回到审批。超出"只改环境"白名单的改动会触发新的审批。

## 5. 自愈循环

### 5.1 收集信号

Runner 每轮收集：容器状态、退出码、重启次数、是否被 OOM kill、healthcheck、docker events、每个服务最近 200 行日志（先按 ERROR / Exception / FATAL 过滤）、构建日志、探针结果、资源占用。整理成结构化的**诊断快照**，脱敏后上报。原始日志不全量上传。

### 5.2 提取错误特征

去掉时间戳、容器 ID、IP、端口号等变量，计算特征值，例如 `bind:address_already_in_use`、`jdbc:connection_refused@mysql`、`oom:exit137@app`。

### 5.3 分级处理

| 级别 | 处理方式 | 典型问题 |
|---|---|---|
| T0 规则 | 确定性修复，不调用 LLM | 宿主机端口冲突、退出码 137、依赖未就绪（补 `depends_on: service_healthy`）、镜像架构不匹配 |
| T1 小模型 | 单个服务内的问题 | 缺少环境变量、配置字段写错 |
| T2 大模型 | 跨服务根因、从未见过的错误 | Kafka `advertised.listeners`、跨服务鉴权链路 |

按依赖拓扑排序，先修上游再修下游。上游未健康时，下游的连接报错只当作症状。

### 5.4 修复的形式与边界

修复方案必须是结构化补丁：对 compose 的 JSON Patch，或者对配置文件、Dockerfile 的 diff。白名单校验器逐条检查补丁路径，碰到源码路径即停止，输出"源码问题"诊断报告。

### 5.5 预算与停止条件

- 同一错误特征最多尝试 3 次；整体最多 10 轮；时间上限 30 分钟；设置 token 和成本上限。可按 cluster 覆盖。
- 同一补丁（按哈希判断）不会执行第二次。修复后错误特征未变，该节点升一档模型。
- 新补丁撤销了之前的补丁，判定为来回修改，立即升级给人处理。

### 5.6 升级给人

Web 展示根因判断、证据日志、尝试过的方案及结果、建议的下一步。用户补充提示后，状态机从断点继续。

### 5.7 经验积累

记录每次成功的"错误特征 → 补丁"。同一特征在不同 cluster 中被同一种补丁修复 N 次后，生成候选 T0 规则，人工审核后上线。

## 6. 服务描述文件与构建流程

### 6.1 `.cagent/service.yaml`

```yaml
name: order-service
kind: app
stack: java-springboot          # 选择 builder 模板
sources:
  release:  { nexus: "com.acme:order-service:{version}:jar" }
  snapshot: { nexus: "com.acme:order-service:{version}-SNAPSHOT:jar" }
  ci:       { jenkins: "order-service/main", artifact: "target/*.jar" }
  workdir:  { build: "mvn -q package -DskipTests", output: "target/*.jar" }
runtime:
  ports: [{ name: http, container: 8080 }]
  env:
    required: [SPRING_PROFILES_ACTIVE]
    defaults: { JAVA_OPTS: "-Xmx512m" }
  healthcheck: { http: "/actuator/health", expect: 200 }
requires:                        # 声明需要的能力，不写死具体实例
  - capability: mysql
    version: ">=8.0"
    bind: { SPRING_DATASOURCE_URL: "jdbc:mysql://{{host}}:{{port}}/{{db}}" }
  - capability: kafka
    bind: { KAFKA_BOOTSTRAP: "{{host}}:{{port}}" }
probes:                          # L3 冒烟探针
  - http: { path: "/api/orders/ping", expect: 200 }
```

中间件模板使用同一格式（`kind: middleware`），由平台维护，声明 `provides`、支持的版本、默认配置、健康检查和资源档位（`tiny` / `standard`）。生成器按 `requires` / `provides` 匹配服务，把连接参数渲染进环境变量。只要 TiDB 声明 `provides: mysql`，同一个应用就既能连 MySQL 8.0，也能连 TiDB。

### 6.2 从来源到镜像

| 来源 | Runner 的处理 |
|---|---|
| release / snapshot | 用本机 Nexus 凭证下载。snapshot 解析到带时间戳的具体版本（如 `2.0-20261016.031522-7`），写入调优记录，保证可复现 |
| ci | 用用户的 CI token 调用 Jenkins / GitLab 接口下载产物 |
| workdir | 在本机执行 `build` 命令，读取 `output` 产物；工作目录只读 |

拿到制品后套用对应技术栈的 builder 模板（Java 用 `eclipse-temurin:<jdk>-jre` 加 jar）生成 Dockerfile 并构建。

- **镜像缓存**：标签为 `cagent/<svc>:<来源>-<制品sha256前12位>`，制品不变就跳过构建。
- **单服务重建**：本地工作目录的项目改完代码后，一键只替换该服务的容器。
- **未登记项目**：agent 读取 pom、gradle、package.json、go.mod、已有 Dockerfile 推断描述文件草稿；cluster 跑通后用户可将草稿以 PR 提交到项目仓库。

## 7. 模型路由

### 7.1 三层映射

```yaml
tiers:                     # 档位 → 模型，随环境变化
  small:  ollama/qwen2.5-coder:7b
  medium: vllm/qwen2.5-32b-instruct
  large:  bedrock/claude-sonnet
nodes:                     # 节点 → 默认档位
  clarify: large
  generate_unregistered: medium
  log_classify: small
  fix_single_service: small
  fix_cross_service: large
  handover_summary: small
```

节点代码只引用档位名，不引用模型名。

### 7.2 自动选择

复杂度由规则打分，不让 LLM 自评。打分依据：涉及的服务数、错误特征是否见过、未登记服务数、之前失败的次数。得分决定起始档位；修复失败或输出不符合 schema 时升一档。所有 LLM 输出用 Pydantic schema 校验，失败先重试一次，再失败就升档。

### 7.3 能力标签

每个模型标注 `tool_use`、`json_mode`、`context` 等能力。路由器拒绝把需要工具调用的节点分配给不支持的模型，并说明原因。

### 7.4 用户自定义

覆盖顺序：平台默认 < 团队 < 个人 < 本次 cluster。Web 提供快捷选项（"全部用大模型"、"只用本地模型"），也允许为单个节点指定模型。

## 8. 安全

- **Runner 认证**：用户在 Web 上通过 SSO 登录后注册设备，获得绑定用户和设备的 token，可随时吊销；连接使用 WSS。
- **工具参数校验**：文件读写限定在 `~/.cagent/` 内，声明的工作目录只读；Docker 操作只作用于带 `cagent.managed=true` 标签的 compose 项目。
- **Compose 安全检查**：默认拒绝 `privileged`、挂载 `docker.sock`、`network_mode: host`、挂载工作区以外的宿主机路径、`cap_add`。用户在审批时可逐项放行。
- **凭证与脱敏**：凭证不离开本机。Runner 在上传前脱敏诊断快照（密码、token、AK/SK、JDBC URL 中的密码），平台和模型都看不到原始日志。
- **审计**：平台记录每次工具调用、补丁和审批，可按 cluster 回放。

## 9. 测试策略

| 层 | 内容 | 方法 |
|---|---|---|
| 单元测试 | Runner：compose-go 校验、白名单、脱敏、错误特征提取。平台：ClusterSpec → compose 生成、复杂度打分、预算与来回修改检测 | Go 表驱动测试；生成器用 golden file |
| LLM 节点 | 澄清、生成、诊断的输出结构和决策 | CI 中录制并回放；每晚用真实模型跑一次 |
| 故障场景库 | 预先构造的坏 cluster：端口冲突、OOM、缺少环境变量、MySQL 未就绪、Kafka listeners 配错、架构不匹配、配置拼写错误等。每个场景标注根因类别和最大轮次 | 每个场景在每个档位上运行，统计修复成功率、轮次、成本，据此调整默认路由表 |
| 端到端 | 用 agent 部署它自己的平台，再加 2～3 个公司真实项目 | 每晚运行 |

新出现的失败案例都加入故障场景库。

## 10. POC 范围

**包含：**

- 单机部署：平台和 Runner 在同一台机器上用 compose 启动；模型用 Ollama 加 Bedrock
- Web：对话、YAML diff 审批、状态面板、日志
- 完整状态机（第 1–9 步），含预算、来回修改检测、升级给人
- 中间件模板：MySQL、Redis、Kafka
- builder：Java Spring Boot
- 来源：Nexus release / snapshot、本地工作目录
- 约 10 条 T0 规则，加上 T1 / T2 的 LLM 修复
- 验收 L1–L4

**暂不包含：**

- CI 制品来源
- 配方分享
- 通过 Git webhook 收集描述文件（POC 读取本地 catalog 目录）
- 以 PR 回写描述文件
- 守护期的外部通知
- 团队级配置覆盖
- SSO（POC 使用固定 token）
- 从修复记录中挖掘规则

**成功标准：**

- 故障场景库在预算内的自动修复率不低于 80%
- 2 个真实项目端到端跑通
- 已登记项目从输入描述到 cluster 健康不超过 15 分钟

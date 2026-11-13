# Proposal 14：Patio 作为 RoleInstance Callback Executor

| 项目 | 内容 |
| --- | --- |
| 状态 | draft |
| 日期 | 2026-11-04 创建，2026-11-13 更新 |
| 作者 | inference-engine-runtime 维护者 |
| 参考 | `rbg/keps/527-roleinstance-lifecycle/README.zh-CN.md` |
| 范围 | Patio 定义 callback，RBG 通过 signalLabel 触发调用；以路由上下线和排空为首批能力 |

## 摘要

本 proposal 将业务能力拆成独立的异步 Callback plugin。Patio 侧只配置 `start`、`end` 的有序 callback 名称列表，以及 `signalLabel -> callback` 绑定列表；RBG 通过写入 signalLabel 发出调用信号。启动、signalLabel 触发和退出操作由统一的 Executor 执行。hook 收到的输入统称 signal；signalLabel 是当前唯一的 signal 实现。

RoleInstance（RI）是 RBG 的实例级资源；signalLabel 是编排系统写入 RI labels 的 `key=value` 触发标记。Patio 直接 LIST/WATCH RI，按配置顺序检测 signalLabel，串行执行匹配的 callback，成功后逐个清除对应 signalLabel。每个 signalLabel 只绑定一个 callback，多个操作使用不同 signalLabel。

```text
程序启动 -> 注册插件并校验配置 -> 执行 start callbacks
  -> 监听 RI labels -> 按配置顺序匹配、执行、清除 signalLabel
  -> 收到 SIGTERM / SIGINT -> 执行 end callbacks -> 退出
```

## 动机

### 现状

当前 `topo_client` 的行为是：

```text
Patio 启动
  -> 推导 router / worker / engine endpoint
  -> 等待 engine ready
  -> register
  -> 周期性 re-register
  -> SIGTERM / SIGINT 时 unregister
```

这适合“启动即上线”的部署，但不适合由 RBG 控制的实例生命周期：

1. RBG 允许接流前，实例可能已经注册并接收流量；
2. RBG 开始重建时，router / proxy 成员可能仍指向失败实例；
3. RBG 要求摘流后，进程重启可能重新注册；
4. SIGTERM 是进程事件，不代表 RBG 生命周期信号；
5. endpoint 推导、注册协议、heartbeat 和信号处理耦合在 `topo_client` / `app.py` 中。

### 目标

1. 用同一个 Executor 执行 start、signalLabel callback 和 end；通过默认绑定兼容存量启动行为，而不增加模式分支。
2. 支持独立 callback 注册和复用，用户直接配置操作及顺序。
3. 多个操作各用独立 signalLabel；编排系统同时写入有依赖的 signalLabel，Patio 按配置顺序串行处理，失败不绕过。
4. 下线 callback 内部先停止 heartbeat，等待最后一次 register 完成，再注销；需要排空时继续等待 drain。
5. 提供不等待 engine 排空的路由清理 callback，供恢复前或退出时使用。
6. Executor 只负责有序执行、失败策略和确认时机；RI hook adapter 负责 signalLabel 观察与确认协议，registration / engine adapters 负责框架差异，部署方配置哪个信号调用哪个 callback。

### 非目标

1. 不修改 RBG API、RoleInstance spec 或 controller 状态机；
2. 不实现 Executor 选举、Lease 或授权；
3. 不管理 Pod finalizer 和 Pod 删除时序；
4. 不定义多 Pod RoleInstance 的成员发现与动作聚合协议；
5. 首批内置 plugin 不实现业务自定义排空检查或 KVCache 保存恢复；此类 plugin 的业务契约另行定义。
6. 不实现任意脚本执行、通过 YAML 动态导入 Python 模块、跨 Executor 全局排序或通用依赖 DAG。
7. 不把 Recovery 清理实现为 engine 排空 barrier；engine 可能已经死亡。

### 用户影响

| 用户 | 影响 | 是否需要行动 |
| --- | --- | --- |
| Kubernetes / RBG 运维人员 | 用 `event_callbacks` 配置 signalLabel 与操作，失败保留 signalLabel 并阻塞后续绑定 | 注入配置和 RI 名称、配置 RBAC，并保证 signalLabel 组合、顺序和唯一所有者 |
| 应用开发者 | 是否启动上线、退出下线均由配置决定，不再由模式隐式决定 | RBG 控制上线时省略 start 中的 `router_onboard`；确认长连接及排空需求 |
| SGLang / vLLM 集成开发者 | 协议 adapter 需要提供可确认的 unregister 和排空状态 | 确认 router / proxy API 与指标语义 |
| Patio 维护者 | 新增公共插件注册表、统一 Executor 与 RI 观察层 | 移除旧启动注册和信号处理旁路，避免重复执行 |

## 提案

### 用户故事

```text
作为一名 Kubernetes 运维人员，
我希望 Patio 只在 RBG 发出上线信号后注册推理 worker，
以便实例在受控操作就绪前不会接收流量。
```

```text
作为一名 Kubernetes 运维人员，
我希望 Patio 在更新或删除前停止 heartbeat 注册、注销并排空请求，
以便 RBG 可以安全替换或删除 RoleInstance。
```

```text
作为一名 Kubernetes 运维人员，
我希望 Patio 在 RBG Recovery 前移除陈旧 router 成员，且不等待 engine 排空，
以便 RBG 重建死亡实例时 router 不继续向它转发流量。
```

```text
作为一名 SGLang / vLLM 集成开发者，
我希望注册和排空行为由显式 adapter 表达，
以便框架差异不会泄漏到 signalLabel 调度逻辑中。
```

```text
作为一名插件开发者，
我希望独立操作能复用于启动、退出和任意 signalLabel，
并通过不同 signalLabel 的配置顺序表达先摘流后保存、先恢复后上线。
```

## 术语

| 术语 | 含义 |
| --- | --- |
| Patio | 本项目 inference-engine-runtime 的运行时进程，作为推理引擎 sidecar。 |
| RoleInstance（RI） | RBG 中的实例级资源，一个 RI 代表一个推理实例；本 proposal 首版只支持单 Pod / 单 Executor。 |
| signal | hook 收到的触发输入；来源不限于 RI label。 |
| signalLabel | signal 的一种实现，值为 RI label 中的 `key=value`；每个操作独立触发、独立确认，成功后由唯一 Executor 删除。 |
| Callback plugin | 以名称注册的独立异步操作，不感知 RBG hook 和 signalLabel，不负责 Kubernetes 写回。 |
| `HookBinding` | 一个 signal 来源与一个 callback 的绑定；当前 signal 来源是 signalLabel，列表顺序就是检测和串行处理顺序。 |
| Executor | 运行 start / end callbacks，按序处理 hook 绑定、重试，并在 callback 成功后请求 hook adapter 确认完成的组件。 |
| hook adapter | 外部信号协议的接入组件，负责观察、匹配复核与完成确认；首版实现为 RI signalLabel。 |
| ack / 完成确认 | callback 成功后对触发信号的确认；RI 实现为条件清除 signalLabel 并确认结果，不是无条件运行的后处理。 |
| registration | worker 加入 / 退出 router 或 proxy 成员列表的协议。 |
| endpoint | router / proxy、worker、engine health 的访问地址。 |
| heartbeat | 周期性调用 register 的自愈机制，用于 router 重启后恢复注册。 |
| drain / 排空 | 从 router / proxy 成员中移除后，等待已分配请求完成的过程。 |
| fail closed | 无法确认结果时保持阻塞状态，不清 signalLabel，让 controller 继续等待。 |
| start / end | 进程启动和终止事件，不是 RBG hook。 |

## 架构调整

### 当前架构

当前 `topo_client` 同时承担：

- endpoint 推导；
- engine health 等待；
- register / unregister；
- heartbeat re-register；
- SIGTERM 注销。

它不是 RI 级流量控制器，也不是 lifecycle Executor。

### 目标架构

以“可配置的操作执行”为核心，而不是以 RoleInstance lifecycle 为核心：`application` 装配资源，`events` 调度操作，`hooks` 接入并确认信号，`callbacks` 暴露业务动作，`registration` / `engine` 实现业务能力。Patio 不解释 RBG 生命周期阶段；RI label 是当前唯一的 hook 接入协议。

```mermaid
flowchart TB
    C["RBG / 编排系统"] -->|写入 signalLabel 组合| R[("RoleInstance labels")]
    F["event_callbacks 配置"] --> B["application：依赖装配、注册与绑定解析"]
    B --> X["events/executor：start / 有序 hook / end"]
    B -->|创建所需资源| W["events/hooks/roleinstance：RI hook adapter"]
    R -->|直接 LIST/WATCH| W
    W -->|最新快照、匹配与复核结果| X
    X -->|callback 成功后请求 ack| W
    W -->|GET / 条件 PATCH / 确认| R
    R -->|各 signalLabel 完成确认| C
    X -->|调用已绑定的 callable| P["callbacks：独立操作入口"]
    P --> S["registration/service：注册业务协调"]
    S --> H["registration/heartbeat：周期注册与在途收束"]
    S --> A["registration adapters"]
    H --> A
    P --> E["engine：就绪与排空能力"]
    S --> E
    A --> U["Router / Proxy"]
    E --> I["Engine"]
```

start、hook 和 end 可以同时配置，复用 callback 调用机制，但保留各自的失败策略。没有 hook 绑定时不创建 Kubernetes client / watcher；没有注册相关 callback 时不创建注册服务、不启动 heartbeat，也不修改 router 成员。插件可以共享业务依赖，但不共享 RBG hook 接口。

不设顶层 `lifecycle/`、统管业务的 `runtime/` 或 `events/handler/` 中间层。Executor 维护启动中、处理中、关闭中等执行状态；注册服务维护注册与 heartbeat 状态；RI hook adapter 维护观察快照和连接状态。它们不组成一个推断 RBG 生命周期的总状态机。

### 目录组织变化

保留 `api/`、`engine/`、`metrics/` 的现有职责。将 topo client 中的 engine health 检查归入 `engine/`，并补充就绪等待与排空状态能力；不重组现有 LoRA API 或指标暴露链路。

当前结构：

```text
patio/
├── app.py
├── envs.py
├── api/
├── engine/
├── metrics/
└── topo/
    ├── client/
    │   ├── base_topo_client.py
    │   ├── sgl_topo_client.py
    │   └── vllm_topo_client.py
    ├── factory.py
    ├── server/
    └── utils.py
```

目标结构（省略 `__init__.py`）：

```text
patio/
├── app.py                      # CLI、进程入口
├── application.py              # 依赖装配、任务管理、退出收束
├── config.py
├── envs.py
├── endpoints.py                # 地址解析，暂不扩成顶层目录
├── events/
│   ├── config.py               # 事件配置校验、兼容默认值
│   ├── bindings.py             # 有序绑定及配置解析
│   ├── registry.py             # callback 注册机制
│   ├── executor.py             # start、hook、end 执行流程
│   └── hooks/
│       └── roleinstance.py     # RI 观察、匹配复核、条件清除与确认
├── callbacks/
│   ├── engine.py               # engine_wait_ready 等操作入口
│   └── router.py               # onboard / offboard / cleanup 入口
├── registration/
│   ├── service.py              # 业务安全顺序、共享注册状态
│   ├── heartbeat.py            # 周期注册启停、在途请求收束
│   ├── base.py                 # 注册协议接口
│   ├── factory.py
│   ├── sglang.py               # SGLang router 协议
│   └── vllm.py                 # vLLM proxy 协议
├── engine/                     # health、排空状态、现有 LoRA 能力
├── api/
├── metrics/
└── topo/                       # 保留 server 和迁移期必要兼容入口
```

迁移关系：

| 当前 | 目标 | 变化 |
| --- | --- | --- |
| `topo/client/*` 中的注册协议 | `registration/base.py`、`sglang.py`、`vllm.py` | 只保留 register / unregister / status 原语 |
| `topo/factory.py` 中的 client 工厂 | `registration/factory.py` | 选择注册 adapter；server 工厂仍留在 topo |
| topo clients 与 `topo/utils.py` 中的地址推导 | `endpoints.py` | 独立解析 router / worker / engine 地址 |
| topo clients 中的 health 等待 | `engine/` | 区分引擎真正就绪与 HTTP 地址可达 |
| `app.py` 与 topo 基类中的 heartbeat | `registration/heartbeat.py` | 统一为可启停、可收束的周期注册任务 |
| `app.py` 中的启动和信号编排 | `application.py`、`events/executor.py` | 分离资源管理与操作调度，删除直接注册 / 注销旁路 |
| 新增 | `events/registry.py`、`events/config.py`、`events/bindings.py`、`callbacks/` | 注册、校验、绑定独立操作 |
| 新增 | `registration/service.py` | 协调 adapter、heartbeat 与 engine 能力，保证操作内部安全顺序 |
| 新增 | `events/hooks/roleinstance.py` | 提供 RI 快照、匹配复核与完成确认 |
| `topo/server/` | 保持现状 | 不随 client 整体迁移，与本次主线无关 |

### 模块边界

| 模块 | 职责 | 不负责 |
| --- | --- | --- |
| `application.py` | 按配置装配资源与 callback，协调 HTTP 服务、后台任务和退出 | 直接执行注册业务、解释 RI 生命周期 |
| `events/config.py`、`bindings.py`、`registry.py` | 配置校验、名称注册与有序绑定；label 语法规则由 RI hook 模块提供 | 创建业务 client、执行 callback |
| `events/executor.py` | 顺序扫描、串行调用、失败重试、退出期限、决定 ack 时机 | Kubernetes GET / PATCH、router 协议、heartbeat 控制 |
| `events/hooks/roleinstance.py` | RI LIST/WATCH、快照匹配、GET 复核、条件 PATCH 与确认 | 调用 callback、决定业务完成、finalizer、选举 |
| `callbacks/` | 暴露命名异步操作，调用已绑定的业务服务或 engine 能力 | 信号观察与确认、决定操作绑定位置 |
| `registration/service.py` | 上线 / 下线 / 清理的安全顺序，协调注册状态、heartbeat、排空 | 事件调度、RI 状态、Kubernetes 写回 |
| `registration/heartbeat.py` | 周期 register 的启停与在途收束 | 决定何时上线或下线 |
| `registration` adapters | SGLang / vLLM 注册、注销及成员状态确认 | heartbeat 调度、事件重试、排空决策 |
| `engine/` | 引擎就绪检查、排空状态与框架差异 | 路由注册、signalLabel 确认 |
| `endpoints.py` | 根据环境变量和实例配置解析地址 | 网络注册、事件调度 |

`events/executor.py` 只调用已绑定的 callable 和 hook adapter，不导入具体 callback 模块或 Kubernetes SDK。`events/registry.py` 不反向导入业务插件；`application.py` 显式装配这些模块。`callbacks/` 独立于 `events/` 放置，使业务操作与触发协议分开；注册 / 引擎能力层不反向依赖事件层。

### 信号观察与完成确认

监听与确认属于同一个 hook 协议，放在同一 adapter 中，不另设通用后处理模块。Executor 决定何时确认，adapter 决定如何确认：

```text
hook adapter 提供当前快照
  -> Executor 按配置顺序匹配
  -> adapter 复核当前信号
  -> Executor await callback()
  -> adapter ack：条件清除并确认
  -> Executor 处理下一条绑定
```

RI label 表示当前仍待处理的请求，而非每条 WATCH 通知都对应一次业务调用。watcher 更新最新快照并唤醒 Executor，不把每个通知排队成独立 callback 任务。失败重试也能唤醒处理循环。callback 成功是 ack 的前提，不能在 `finally` 中无条件确认；撤销或改值只要求重新扫描，不代表业务成功。不提供 exactly-once 保证。

| 入口 | 失败策略 | 完成确认 |
| --- | --- | --- |
| start | 停止后续 start / hook，进入限时清理 | 无 |
| hook | 保留未完成信号，停止本轮并退避重试 | callback 成功后由 adapter 执行 |
| end | 共用退出期限，best-effort 执行 | 无 |

### 应用装配与资源所有权

1. `application.py` 加载配置、显式导入受信任插件、创建应用级 callback 注册表，并在首次调用前完成依赖校验与绑定。只要求所选操作需要的资源；例如仅配置 `router_cleanup` 不要求 engine 排空能力。
2. 同一应用中的 router callbacks 共享一个注册服务、adapter 和 heartbeat。进程级注册表只保存无状态插件定义；绑定 client 的 callable 属于当前应用，不放入跨应用共享的全局表。
3. 应用层与 Uvicorn 使用一个关闭协调入口，在任何 start callback 前建立信号处理。先收束 Executor 当前操作，再请求已创建的业务资源停止后台活动，然后由 Executor 执行 end，最后关闭资源；所有步骤共用同一关闭预算。Executor 不直接调用 heartbeat。
4. 即使 end 为空也必须停止本地后台任务，但不能因此隐式注销。start 业务等待运行在受管理任务中，不阻塞 HTTP 服务启动；start 完成前不处理 hook。组装或启动失败也要清理已创建的资源。
5. 事件路径使用可取消、带超时的异步 I/O 和退避等待，不能仅把现有同步 `requests` / `time.sleep` 包在 `async def` 中。迁移期若桥接同步调用，必须跟踪并等待在途调用收束；取消等待协程不代表底层请求已终止。

### Registration 语义

`registration` 分为业务服务与协议 adapter。`service.py` 保证上线 / 下线操作的内部顺序并管理共享状态；adapter 只封装成员注册原语：

```text
register
unregister
status
```

SGLang 成员身份：

```text
稳定逻辑身份 = worker endpoint
worker_id = router 内部句柄
```

Patio 不把 `worker_id` 作为持久状态。SGLang router 支持 list worker；`status` 由 list worker 结果实现，`unregister` 的语义是：list 当前成员，按稳定 worker endpoint 查找并移除对应成员，直到 list 结果确认成员已不再存在。成员不存在时视为幂等成功；无法确认成员状态时 fail closed，不清 signalLabel。如果一个 endpoint 对应多个逻辑成员，必须覆盖全部匹配成员。

vLLM 成员身份：

```text
register   -> POST /instances/add
unregister -> POST /instances/remove
```

vLLM proxy 已提供 `/instances/remove` 接口。当前 Patio 实现中 `unregister` 是 no-op，属于落后实现，需要补齐。remove 调用失败或无法确认结果时 fail closed，不清 signalLabel。

### Endpoint 与 Heartbeat

Endpoint 推导独立于注册协议，负责根据环境变量和实例信息推导：

```text
router / proxy endpoint
worker endpoint
engine health endpoint
```

Heartbeat 位于 `registration/heartbeat.py`，由注册服务持有，是周期性调用 register 的自愈机制，不是事件调度器或下线控制器：

1. `start()` 后周期性 register；
2. `stop()` 后不再调度新的 register；
3. start / stop 幂等；
4. `router_offboard` 和 `router_cleanup` 内部先 stop heartbeat；
5. 等待最后一次 register 请求完成后再 unregister；
6. `router_onboard` 完成注册后 start heartbeat。

`router_cleanup` 不等待 engine 排空，可绑定到恢复前 signalLabel；`router_offboard` 在注销后继续等待排空。两者的差异由 callback 定义，不由 Executor 根据 RBG hook 选择。

heartbeat 存在时，单次 register 失败可以由下一次 heartbeat 重试；registration 原语不需要额外叠加长时间 retry。

## RBG 通过 label 调用 Patio callback

### Kubernetes API 协议

有 hook 绑定时，Patio 直接访问以下固定资源：

```text
group:   workloads.x-k8s.io
version: v1alpha2
kind:    RoleInstance
plural:  roleinstances
scope:   Namespaced
```

读写对象都是 RI；不经过 RI label 下传到 Pod 再由 Downward API 文件读取，避免下传延迟导致旧 label 被重复观察。

RI hook adapter 只读取已配置 signalLabel 的 key/value 进行匹配，Executor 使用其快照与复核结果，不依赖 `rbg.workloads.x-k8s.io/role-instance-hook-state` 或 `rbg.workloads.x-k8s.io/role-instance-lifecycle-state`。这些 label 缺失、取值未知或发生变化，都不改变 callback 的匹配结果，也不触发任务抢占。

### 协作流程

1. RBG / 编排系统需要调用 Patio callback 时写入 signalLabel；有依赖的操作必须在同一次 RI 更新中写入各自的 signalLabel。signalLabel 是 signal 的当前实现。
2. Patio 完成 start callbacks 后，LIST 当前 RI，再 WATCH 后续变化；已有 signalLabel 也必须处理。
3. Executor 按 `hook_bindings` 配置顺序扫描同一个 labels 快照，不按 labels 字典顺序执行。
4. signalLabel 匹配时，先 GET 最新 RI 复核，再执行绑定的单个 callback。
5. callback 成功后清除自己的 signalLabel，确认清除后才处理下一项；失败停止本轮后续处理并退避重试。
6. RBG 依据其等待的全部 signalLabel 的完成情况推进流程，Patio 不判断业务流程是否完成。

协议边界：

- controller 不调用 Patio HTTP API；Patio 不向 controller 上报结构化业务结果。
- 一次编排动作可以写入多个 signalLabel；每个 signalLabel key 只分配给一个 Executor。
- 同一 key 在 RI labels 上同一时刻只能有一个 value；复用同一 key 的不同 value 的操作必须由编排方分时触发，有依赖且需同次写入的 signalLabel 必须使用不同 key。
- signalLabel 仍匹配就代表待处理，Patio 不依据 RBG 生命周期状态使其失效。
- 配置顺序只保证同一 Executor、同批可见 signalLabel 的顺序。后置 signalLabel 若先单独出现，前置项因不匹配被跳过，后置项会先执行；跨 Executor 无全局顺序保证。

GET 复核不是分布式锁。执行语义依赖唯一所有者、编排时序和 callback 幂等。

### signalLabel 删除

删除 signalLabel 使用幂等的 JSON Merge Patch：

```json
{
  "metadata": {
    "resourceVersion": "<latest-resource-version>",
    "labels": {
      "patio.engine/update-block": null
    }
  }
}
```

只有 callback 成功后才允许清除 signalLabel。删除前 GET 最新 RI，确认 key/value 仍匹配；PATCH 只删除该 key，不覆盖其他 labels，并携带本次 GET 得到的 `metadata.resourceVersion` 作为乐观并发前置条件。请求中的 resourceVersion 占位符必须替换为该次 GET 的值；冲突时重新 GET，不能盲目重发旧 patch。

以成功 PATCH 响应或随后 GET 确认 signalLabel 已消除。PATCH 结果未知时，不执行后续 callback；重新读取确认，仍匹配则重试。若 key 已删除或值已变化，不清除新值，结束本轮并从最新快照重新扫描；这只表示旧触发不再待处理，不证明业务成功。

业务成功但写回尚未确认时，重启可能重放 callback。resourceVersion 检查保护标签写入，不提供业务事务、RI UID fencing 或相同 key/value 的 ABA 防护。

### 删除流程权责边界

删除前后的时序由 RBG controller 和编排系统保证：

1. RBG controller 负责 Pod finalizer 的注入、等待和清除；
2. RBG controller 保证 signalLabel 清除前不主动删除业务 Pod；
3. Callback Executor / sidecar 可用性和故障恢复由部署系统负责；
4. Patio 执行配置绑定的 callback；部署方可将删除前摘流 signalLabel 绑定到 `router_offboard`；
5. Patio 不注入、不删除、不校验 Pod finalizer，也不保证 sidecar 存活。

如果 Executor 不可用，signalLabel 保留，controller 继续等待。外部直接删除 Pod 的行为由 RBG 定义，Patio 不额外处理。

## 用户配置

### 事件绑定

本 proposal 新增可选启动参数 `--event-config=/etc/patio/events.yaml`，读取 YAML 配置。以下是绑定结构示例：启动时上线，update / delete signalLabel 复用摘流排空操作，ready signalLabel 上线，退出时执行下线。signalLabel 值由部署方定义。

```yaml
event_callbacks:
  start:
    - router_onboard
  hook:
    # ready hook：上线
    - signalLabel: "patio.com/router=onboard"
      callback: router_onboard
    # update / delete hook：复用摘流排空操作
    - signalLabel: "patio.com/router=offboard"
      callback: router_offboard
  end:
    - router_offboard
```

规则：

1. `start`、`end` 是按序执行的 callback 名称列表；`hook` 是按序检测的绑定列表，每项只含 `signalLabel` 与单个 `callback`。
2. 不使用 `enabled` 或命名 hook 键。省略某部分等于空列表，显式文件中的 `event_callbacks: {}` 表示不执行任何事件操作。
3. callback 可以跨位置复用；每条 hook 只绑定一个 callback，多操作分别使用不同 signalLabel。
4. 启动前拒绝未知字段、未知 plugin、非列表字段、错误条目类型、重复 YAML key、非法 signalLabel，以及完全相同的 signalLabel（同 key 同 value）重复绑定；允许同 key 不同 value 的绑定，用于在不同时刻复用同一 key。空列表合法，显式 null 不等同于空列表。
5. signalLabel 按首个 `=` 拆分并校验 Kubernetes label 语法；缺少 `=`、空 key、非法 key/value 拒绝启动。空 value 按 Kubernetes label 规则允许，匹配时必须区分 key 缺失和空字符串。
6. 配置在启动时加载，不提供热更新。文件不可读、解析失败或格式错误时拒绝启动，不能静默回退到启动注册。
7. 只有未提供 `--event-config` 时才使用兼容默认值：原本会运行 topo client 的部署（提供 `--instance-info`）转换为 `start: [router_onboard]`、`end: [router_cleanup]`、`hook: []`；原本不运行 topo client 的部署转换为空绑定。仍使用统一 Executor，不保留独立模式分支。
8. 显式配置完全替代上述默认值，不追加默认 callback。RBG 指定的 Executor 必须显式挂载并指定配置；其他实例用空绑定，不做选举或 Lease。
9. hook 输入统称 signal；首版只实现 signalLabel，后续可在不改变 callback 语义的前提下扩展其他 signal 来源。

### 运行参数与注入

| 参数 / 变量 | 必填条件 | 含义 |
| --- | --- | --- |
| `--event-config` | 使用 signalLabel 的 RBG 部署必须显式提供 | 事件绑定 YAML 路径，属本 proposal 新增接口 |
| `LIFECYCLE_RI_NAME` | hook 列表非空 | 要观察的 RI 名称 |
| `LIFECYCLE_DRAIN_CHECK_INTERVAL` | 可选，默认 `1s` | `router_offboard` 的排空轮询间隔，必须为正 |
| `LIFECYCLE_DRAIN_STABLE_SAMPLES` | 可选，默认 `3` | 连续成功 idle 样本数，必须为正整数 |
| `HEARTBEAT_INTERVAL` | 沿用现有配置 | heartbeat 周期 |

signalLabel 是 signal 的首版实现，全部来自 YAML hook 绑定。router、worker 和 engine 的参数继续使用现有 endpoint / `--instance-info` 配置，并在启动前校验已选 callback 的依赖。

hook 非空时，Patio 从 Pod service account 获取 namespace，只访问该 namespace 中 `LIFECYCLE_RI_NAME` 指定的 RI；不支持跨 namespace 或成员发现。无 hook 绑定时不要求 Kubernetes 凭据。

RBG Role 的 `lifecycle.labelsHandler` 定义 hook 到 signalLabel 的映射；编排系统需将其与 Patio 的 callback 绑定配置保持一致，配置可以生成并挂载为文件。Patio 只消费 signalLabel，不感知 hook 名称。谁执行、何时设置哪些 signalLabel、依赖 signalLabel 是否同次写入，均属于部署协议。

### 信号行为

`application.py` 在任何 start callback 执行前建立与 Uvicorn 统一的 SIGTERM / SIGINT 关闭协调入口，不保留独立的 topo 注销 signal handler。收到信号后：

1. 停止接收新的 hook 工作，终止重试等待，并协作取消 / 收束当前 callback；不让它与 end callback 并发执行。
2. 应用层请求已创建的注册服务停止 heartbeat 并等待在途 register 收束，再让 Executor 按配置顺序执行 end callbacks；Executor 不感知 heartbeat 具体实现。
3. end 共用一个退出期限，best-effort 执行；失败记录后可继续下一项，期限耗尽则退出。退出期限由进程关闭预算提供，必须小于 Pod 剩余 termination grace period，不为每项重新计时。
4. 信号不会被解释成任何 RBG hook，也不会为中断的 signalLabel 调用清除 signalLabel；end callback 不拥有 signalLabel 写回权限。
5. 最终关闭 watcher、clients 和所有本地后台任务；即使 end callback 启动了后台任务，也不能让它延长进程退出期限。

callback 必须支持取消和有界 I/O；若当前任务无法在退出期限内收束，不并行运行 end，由进程退出终止剩余工作。SIGKILL / 崩溃无法保证 end 执行。即使 end 配置 `router_offboard`，也不能替代可靠的删除前 signalLabel 流程。

## 执行逻辑和示意代码

以下代码是伪代码，只表达执行顺序和模块协作，不代表最终接口或实现。

### Callback plugin 注册

公共注册模块 `events/registry.py`；全局表只保存无状态 callback，带依赖的操作注册到应用级表：

```python
from collections.abc import Awaitable, Callable


EventCallback = Callable[[], Awaitable[None]]
callback_plugins: dict[str, EventCallback] = {}


def register_callback(
    name: str,
    *,
    registry: dict[str, EventCallback] | None = None,
) -> Callable[[EventCallback], EventCallback]:
    target = callback_plugins if registry is None else registry

    def decorator(callback: EventCallback) -> EventCallback:
        if name in target:
            raise ValueError(f"Callback plugin 重复注册：{name}")
        target[name] = callback
        return callback

    return decorator
```

独立插件模块 `callbacks/router.py` 在装配时注册绑定同一个注册服务的操作；安全步骤由服务实现，不需要全局 client：

```python
from patio.events.registry import register_callback


def register_callbacks(registry, registration):
    @register_callback("router_onboard", registry=registry)
    async def router_onboard() -> None:
        await registration.onboard()

    @register_callback("router_offboard", registry=registry)
    async def router_offboard() -> None:
        await registration.offboard()

    @register_callback("router_cleanup", registry=registry)
    async def router_cleanup() -> None:
        await registration.cleanup()
```

`application.py` 先复制无状态注册表，再调用所需插件模块的装配函数。`callbacks/engine.py` 用相同方式绑定 engine 能力。每个注册名在应用级表中必须唯一；配置引用未装配的能力时拒绝启动。

装饰器只保存函数引用，不执行业务操作：无状态插件可在模块导入时用 `@register_callback("插件名")` 登记；带依赖插件在装配函数内指定应用级 `registry`。应用启动时显式导入受信任模块，完成所选能力的装配和名称校验后再生成绑定；只增加文件不会自动发现，也不允许 YAML 指定任意模块路径或使用 `eval`。公共注册模块不反向导入业务插件。

也可用 `register_callback(name, registry=app_callbacks)(bound_callback)` 注册绑定依赖后的 callable；同名不能再用装饰器重复注册。注册后 callable 仍是无参数异步操作，不要求实现按 RBG hook 区分的统一多事件 handler。不同应用实例使用不同注册表和业务对象，避免重复装配时名称冲突或误用其他实例的 client。

### 配置到执行结构

`events/bindings.py` 将配置转换成有序执行结构；以下输入已完成“用户配置”一节的结构、插件名和 signalLabel 校验，`callback_plugins` 参数是已装配的应用级注册表：

```python
from dataclasses import dataclass

from .registry import EventCallback


@dataclass(frozen=True)
class HookBinding:
    signal_label: str
    callback: EventCallback


def resolve_callbacks(callback_names, callback_plugins) -> list[EventCallback]:
    return [callback_plugins[name] for name in callback_names]


def build_event_bindings(config, callback_plugins):
    events = config.get("event_callbacks", {})
    start_callbacks = resolve_callbacks(events.get("start", []), callback_plugins)
    end_callbacks = resolve_callbacks(events.get("end", []), callback_plugins)
    hook_bindings: list[HookBinding] = []

    for hook_config in events.get("hook", []):
        hook_bindings.append(
            HookBinding(
                signal_label=hook_config["signalLabel"],
                callback=callback_plugins[hook_config["callback"]],
            )
        )

    return start_callbacks, end_callbacks, hook_bindings
```

三个列表都保留配置顺序；注册阶段不调用 callback，不创建 handler，也不枚举 RBG hook。

YAML 字段名为 `signalLabel`；Python 内部字段使用 `signal_label`。

### Executor 主流程与恢复

```mermaid
flowchart TD
    A["启动、注册插件、校验配置"] --> B["按序执行 start callbacks"]
    B -->|成功| C{"有 hook 绑定？"}
    B -->|失败| H["application 协调：收束当前任务、停止后台注册"]
    C -->|否| W["等待退出"]
    C -->|是| L["hook adapter：LIST/WATCH RI labels"]
    L --> D["从配置首项扫描当前快照"]
    D --> E{"当前 signalLabel 匹配？"}
    E -->|否| N{"还有下一条绑定？"}
    E -->|是| F["adapter 复核，再由 Executor await callback"]
    F -->|成功| G["hook adapter ack：条件清除并确认"]
    G -->|成功| N
    F -->|失败| R["停止本轮；退避后 GET 最新快照"]
    G -->|失败| R
    F -->|复核失配| M["GET 最新快照"]
    G -->|撤销或改值| M
    M --> D
    R --> D
    N -->|是| J["同一快照的下一条绑定"]
    J --> E
    N -->|否| L
    L -. SIGTERM / SIGINT .-> H
    W -. SIGTERM / SIGINT .-> H
    H --> I["Executor 按序执行 end callbacks"]
    I --> Z["application 关闭资源；共用期限内退出"]
```

此图表示 application、Executor 与 hook adapter 的协作，不表示所有步骤都在 Executor 内实现。信号入口适用于整个进程运行期，包括 start 等待期间；无法在期限内收束当前任务时不并行执行 end。HTTP API 可独立服务；start 完成前不执行 signalLabel callback。

单轮有序处理位于 `events/executor.py`。`hook` 是 RI hook adapter：`revalidate` 通过 GET 复核当前信号；`ack` 遵循“signalLabel 删除”一节，已确认清理返回 `True`，发现撤销 / 改值需重新扫描时返回 `False`，调用失败抛出异常。RI 快照及其匹配方法由 adapter 提供，Executor 不直接访问 Kubernetes：

```python
async def process_snapshot(snapshot, hook_bindings, hook):
    for binding in hook_bindings:
        if not snapshot.matches(binding.signal_label):
            continue

        if not await hook.revalidate(binding.signal_label):
            return

        await binding.callback()
        if not await hook.ack(binding.signal_label):
            return
```

执行约定：

1. start 按顺序执行；失败终止启动，不执行后续 start 或 hook，并进入有期限的退出清理。重启可能重放 start，callback 必须幂等。
2. 同一 Executor 只有一个 callback 执行通道。watcher 可持续收集最新快照，但不为每个事件启动并行 callback；自身 PATCH 产生的事件也不能重复并发执行。
3. 单轮只检测该轮快照中的 signalLabel。扫描中新增的 signalLabel 留到下一轮，从配置开头检测，不能在已跳过前置项后拼接新快照中的后置项。
4. callback 或清除失败时停止本轮；按有界退避重新 GET，从配置开头扫描。即使没有新 watch 事件，也需由重试定时器唤醒；已清除项跳过，失败项仍匹配则重试，不绕过它处理后续项。
5. GET 复核失配或清除时发现 signalLabel 被撤销 / 改值，结束本轮，使用最新快照重新扫描；不清除替换值，不将撤销视为业务成功。已发生的外部副作用不能回滚。
6. watch 断连 / resourceVersion 过期时重新 LIST/GET；RI 不存在时不执行或清理 signalLabel，不根据本地旧事件操作。
7. 重启后不依赖本地完成记录，只读取 RI。业务成功但 signalLabel 未清除时可重复执行；signalLabel 缺失不代表某个业务阶段完成，也不能据此自动启动 heartbeat。
8. RBG 控制上线且 start 为空时，进程重启不会自动恢复已清除上线 signalLabel 对应的 heartbeat。部署方必须在确认允许上线后重新触发上线 signalLabel，或另行定义持久化恢复协议；不能通过 hook 推断或隐式启动注册补偿。
9. RBG 生命周期 label 的变化不抢占当前任务。signalLabel 组合和时序由编排方保证；长期失败会阻塞列表中后续匹配项，这是单通道 fail-closed 的代价。

### 内置 callback 业务契约

| Callback | 必须保证的内部顺序 | 常见信号（非限制） |
| --- | --- | --- |
| `engine_wait_ready` | 等待引擎真正就绪，不执行注册 | start，或需要等待就绪的 signalLabel |
| `router_onboard` | 确认 engine ready → register 成功 → 启动 heartbeat | start，或上线 signalLabel |
| `router_offboard` | 停止 heartbeat → 等待在途 register 收束 → unregister / 确认不存在 → 等待稳定 idle | 下线 / 删除前 signalLabel，或 end |
| `router_cleanup` | 停止 heartbeat → 等待在途 register 收束 → unregister / 确认不存在；不做 engine drain | 恢复前 signalLabel，或 end |

router callbacks 是操作入口，表中的内部顺序由共享的 `registration/service.py` 实现；`engine_wait_ready` 直接调用 engine 能力。即使提供独立等待 callback，`router_onboard` 也必须自行保证就绪前置条件，不能假定它之前已执行 `engine_wait_ready`。

必要的安全步骤不能交给用户拆分排序。例如 `router_offboard` 经注册服务保证注销前停止 heartbeat；失败重试期间 heartbeat 保持停止。未知注销结果必须报错，不能作为成功返回。注册服务协调 heartbeat 和 adapter，保证在途注册收束后再确认成员移除；仅取消本地 register 协程不证明远端请求未生效。

`router_cleanup` 不依赖 engine 指标；engine 已死或 drain 为 `unknown` / `unsupported` 时，只要成员移除已确认即可成功。需要排空的 `router_offboard` 则继续 fail closed。

这些 callback 不接收 signalLabel，不读取 RI 生命周期状态，不自行 PATCH。成功返回后由 Executor 请求 hook adapter 确认并清理 signalLabel；start / end 没有 signalLabel 完成确认。

### 跨功能顺序：路由与 KVCache

下面假设未来注册了 `kv_cache_dump` 和 `kv_cache_restore`，只展示配置表达能力，不属于首批业务实现：

```yaml
event_callbacks:
  hook:
    # 恢复 KVCache，并将 worker 注册到 router
    - signalLabel: "patio.com/kv-cache=restore"
      callback: kv_cache_restore
    - signalLabel: "patio.com/router=onboard"
      callback: router_onboard

    # 将 worker 从 router 注销，并保存 KVCache
    - signalLabel: "patio.com/router=offboard"
      callback: router_offboard
    - signalLabel: "patio.com/kv-cache=dump"
      callback: kv_cache_dump
```

- 下线前：编排系统同次写入 `patio.com/router=offboard`、`patio.com/kv-cache=dump` 两个 signalLabel；先摘流排空并清除前者，再保存并清除后者。
- 上线前：同次写入 `patio.com/kv-cache=restore`、`patio.com/router=onboard`；先恢复并清除前者，再上线并清除后者。
- 每个操作有自己的 signalLabel，未匹配项跳过；任一操作或清除失败则不执行后续项。重启后从仍存在的 signalLabel 继续。
- 必须由同一 Executor 处理，且有依赖的 signalLabel 使用不同 key 并同次设置；后置 signalLabel 提前单独出现时，配置列表不是依赖屏障。

不需要把 `callback` 改为列表。Executor 只知道检测顺序，不知道 signalLabel 属于哪个 RBG hook。

### 排空快照

`engine/` 定义通用排空快照，由各框架 adapter 解释“什么是 active / queued / drained”；`registration/service.py` 消费快照并等待稳定 idle，Executor 不解释排空指标。

```text
state: idle | draining | unknown | unsupported
active_requests: number | unknown
queued_requests: number | unknown
swapped_requests: number | unknown | not-applicable
source: sglang-metrics | vllm-metrics | engine-api
```

规则：

1. `unknown` 和 `unsupported` 不能视为 idle；
2. `/metrics` 可访问但必需指标不存在时返回 `unsupported`；网络失败、超时、5xx、解析失败或 NaN 时返回 `unknown`；
3. 框架无法提供排空状态时，`router_offboard` fail closed，`router_cleanup` 不使用此检查；
4. 判定 idle 需要框架适用的 active / queued / swapped 连续为 0，且达到 `LIFECYCLE_DRAIN_STABLE_SAMPLES`；
5. 任意非 idle 样本（包括 `unknown` / `unsupported`）、抓取或解析失败都重置稳定样本计数；
6. signalLabel 路径不提供超时后自动放行；持续失败时 signalLabel 保留，由运维处理。同一 callback 用于 end 时受退出期限约束，不产生 signalLabel 完成确认。
7. 首批内置 plugin 不支持业务自定义排空检查；额外业务约束需另行定义 plugin 契约，不能声称默认指标已经覆盖。

SGLang 首版映射：

```text
sglang:num_running_reqs -> active_requests
sglang:num_queue_reqs   -> queued_requests
```

两者连续为 0 且达到 `LIFECYCLE_DRAIN_STABLE_SAMPLES` 时报告 `idle`。

默认 vLLM 排空快照必须包含三个当前状态 gauge：

```text
vllm:num_requests_running -> active_requests
vllm:num_requests_waiting -> queued_requests
vllm:num_requests_swapped -> swapped_requests
```

判定 idle 的高层条件：

```text
active_requests == 0
and queued_requests == 0
and swapped_requests == 0
```

`vllm:num_requests_swapped` 表示被换出到 CPU 但尚未完成的请求；只看 running / waiting 可能在 swapped 请求存在时误判为 idle。`vllm:num_preemptions_total` 是累计 counter，不是当前状态，不能参与排空判定。

指标缺失时按以下规则分类：

1. `/metrics` 可访问但必需指标不存在，返回 `unsupported`；
2. 网络失败、超时、5xx、解析失败或 NaN，返回 `unknown`；
3. `unsupported` 和 `unknown` 都不能视为 idle，signalLabel 保留。

vLLM 指标可以覆盖仍在生成的流式请求，但不能覆盖已生成完毕但仍保持的 HTTP / SSE / WebSocket 长连接、router / proxy 内部缓冲或外部业务事务。如果业务要求所有外部连接关闭后才能删除，必须使用业务自定义排空检查。

## 权责边界与限制

### RBG / 编排系统负责

- 指定哪个 Pod / sidecar 是 Callback Executor；
- 注入事件配置文件、RI 名称和 callback 所需依赖；
- 管理 RoleInstance 生命周期、hook 到 signalLabel 的映射、signalLabel 组合及写入时序；有依赖的 signalLabel 同次写入；
- 管理 Pod finalizer 和删除时序；
- 保证需要处理相关 signalLabel 时 Executor 可用；
- 处理 controller / Executor 故障恢复。

### Patio 负责

- watch 指定 RoleInstance；
- 注册独立 callback，校验配置并运行 start / end；
- 只按 signalLabel 匹配，并按配置顺序串行执行；
- 由业务 callback 控制 register / unregister、heartbeat 与排空；
- 成功后逐个清除自己的 signalLabel，失败停止后续处理并重试；
- 不推断 RBG hook、业务流程完成或新旧操作优先级。

### 当前限制

1. 多 Pod RI 的成员发现与动作调用协议需要 RBG 定义。本 proposal 只定义单 Pod / 单 Executor 语义，不做成员发现。
2. 首批 plugin 不做业务自定义排空检查或 KVCache 操作；默认指标不能覆盖生成结束后的长连接、router 缓冲和外部事务。`router_cleanup` 有意不检查 engine 排空。
3. 首版不做 RI UID fencing 或持久化执行日志；同名 RI 重建需由编排系统确保旧 Executor 已收束。finalizer 和幂等性不能提供跨对象代际隔离。
4. signalLabel 缺失不能恢复进程内 heartbeat 状态；上线后的 Executor 重启需要编排方明确重新触发。
5. 配置不能验证业务意图；把 `router_onboard` 绑定到下线 signalLabel 在结构上合法。用户需负责 hook 到 signalLabel 的映射、顺序及 start / end 选择。

## 兼容性

1. 未提供事件文件时，按“事件绑定”一节生成兼容默认绑定；保留原本启用 topo client 时启动等待、注册、heartbeat 和退出清理的行为。不把历史 vLLM unregister no-op 作为兼容保证。
2. 显式事件配置是完整替换，不追加隐式注册动作；空 start 不会上线，空 hook 不访问 Kubernetes，空 end 不修改成员关系。
3. 保留 `TOPO_TYPE`、`--instance-info`、endpoint 推导与业务 API；新增事件文件仅决定何时调用操作。旧草案的模式 ENV 和按 hook 命名的 signalLabel ENV 必须迁移，不能与新配置混用。
4. RBG 需要正确实现 signalLabel 写入、等待和完成语义；Patio 不要求生命周期 label 的 key/value ABI。只要资源和 signalLabel 协议不变且 callback 已存在，RBG 新增 hook 时调整配置即可。
5. SGLang 需支持 list worker 和按 endpoint 移除；vLLM 需支持 `/instances/remove`。无法确认注销时，`router_cleanup` / `router_offboard` 均失败，不能忽略错误放行。
6. 需要 vLLM 排空时必须提供 running / waiting / swapped gauges，缺失返回 `unsupported`；此要求不适用于无排空的 `router_cleanup`。
7. 先升级 Patio 和 router / proxy，再部署 signalLabel 绑定；回滚前停止新编排操作并处理所有未完成 signalLabel，不能直接改成启动上线配置或移除事件文件。
8. 新增 plugin 不等于支持任意脚本或 HTTP 检查入口。新增指标和日志为附加能力，旧指标变更需另行提供迁移说明。
9. 本次模块重组保留 `signalLabel` 配置字段、`LIFECYCLE_*` 参数和下文 `patio_lifecycle_*` 指标命名，不因移除顶层 lifecycle 模块而更改对外接口；这些名称不表示 Patio 管理 RBG 生命周期。

## 可观测性

日志记录有效 callback 顺序、RI 与 resourceVersion、signalLabel key、callback 名称、执行入口（start / hook / end）、重试及失败位置、watch 重连、注册 / 排空和清理确认结果。不得输出 service account token 或其他敏感配置。

建议新增指标：

| 指标 | 类型 | 标签 | 含义 |
| --- | --- | --- | --- |
| `patio_lifecycle_event_total` | Counter | `matched` | 收到的 RI 事件数，以及是否存在匹配 signalLabel |
| `patio_lifecycle_action_total` | Counter | `event`, `callback`, `result` | start / hook / end callback 成功、失败、取消次数 |
| `patio_lifecycle_action_duration_seconds` | Histogram | `event`, `callback`, `step` | callback 执行及 signalLabel 清理耗时 |
| `patio_registration_state` | Gauge | `engine`, `protocol` | 当前本地 traffic state：`unregistered`、`registering`、`serving`、`draining`、`recover_cleanup`、`unregistered_confirmed`；`draining` 表示注册侧正在下线，细粒度排空状态见 `patio_drain_state` |
| `patio_drain_state` | Gauge | `engine`, `source` | adapter 报告的 idle / draining / unknown / unsupported |
| `patio_lifecycle_watch_errors_total` | Counter | `operation`, `reason` | watch / patch / get 失败次数 |

指标不以 RBG hook 分类；标签不包含 RI 名称、signalLabel key/value 或 resourceVersion，callback 只取受信任注册名称。实例级排查通过日志关联。

Patio 的 HTTP health / readiness 不因 hook 处理中而自动失败。Patio Ready 只表示进程和 API 可用，不表示业务注册完成或排空完成；对外流量准入由 RBG、router / proxy 和部署方控制。

排查指引：

1. signalLabel 长期保留：检查有效绑定、start 是否完成、watch / RBAC、callback 失败及清理错误，还需检查配置列表中更早的失败项。
2. 下线失败：`router_offboard` 检查 drain；`router_cleanup` 只检查 heartbeat 和 router 注销，不依赖 engine 指标。
3. 未接流量：检查是否发出了上线 signalLabel、绑定是否正确、engine readiness、注册和 heartbeat；Executor 重启后不能从 signalLabel 缺失自动恢复 heartbeat。
4. controller 未推进：检查其等待的全部 signalLabel，包括其他 Executor 所有的 signalLabel；不要把手动删除 signalLabel 当作业务成功。

## 安全性

1. 有 hook 绑定时使用专用 ServiceAccount，授予当前 namespace 中 `roleinstances` 的 `get`、`list`、`watch`、`patch`；LIST/WATCH 使用 `metadata.name` field selector。无 hook 绑定时不要求这些权限。
2. Kubernetes 原生 RBAC 不能限制 PATCH 只修改某个 label key。需要接受该限制并使用专用 ServiceAccount、最小权限和审计日志；如需严格字段隔离，需要 admission 或代理层。
3. Patio 使用 Pod 挂载的 service account token，并自动推导 namespace，不引入长期明文 token 配置。RI 名称和 signalLabel 是非敏感配置，但 signalLabel key / value 必须按 Kubernetes label 语法校验，防止拼接出非法 patch。
4. signalLabel 清理用携带 resourceVersion 前置条件的 JSON Merge Patch，只删除自己配置的 key，不整体替换 `metadata.labels`。
5. router / proxy 和 engine endpoint 属于集群内信任边界。现有 HTTP endpoint 不新增认证协议；跨信任域部署时应由网络策略、mTLS 或代理层保护，而不是由本 proposal 扩大信任模型。
6. watch、PATCH 和 adapter 调用必须有超时和有界退避，避免 API 故障或慢响应耗尽线程 / 协程和文件描述符。
7. YAML 使用安全加载并拒绝重复键；只解析受信任注册表中的 callback 名称。注册机制不是沙箱，添加 Python plugin 属于受信任代码部署。

## 性能与可扩展性

1. 每个 Callback Executor 只 watch 一个指定 RoleInstance，并使用 field selector 或客户端过滤，避免全 namespace informer。
2. RI hook adapter 缓存当前 RI 对象，注册服务单独维护本地 traffic / heartbeat 状态；resourceVersion 越界或 watch 重连时重新 LIST/GET，不维护历史事件队列。
3. heartbeat 周期沿用 `HEARTBEAT_INTERVAL`；只有相关 callback 启动 heartbeat 才周期注册，signalLabel 监听本身不增加周期写。
4. drain 轮询使用独立间隔和最小样本数，稳定窗口由 `LIFECYCLE_DRAIN_CHECK_INTERVAL` × `LIFECYCLE_DRAIN_STABLE_SAMPLES` 隐含确定；请求失败计入 backoff，不把失败样本当作 idle。
5. 有 hook 绑定时每个进程增加一个 Kubernetes watch 连接；每轮按 O(N) 检测 N 条绑定。指标基数受已注册 callback / event / result / engine / source 组合约束。
6. 排空耗时主要取决于业务请求和 engine 指标刷新，不属于 Patio 可承诺的性能 SLO；benchmarks 只用于确认 hook 和 watch 自身开销可忽略。

## 风险与缓解

| 风险 | 影响 | 检测 | 缓解 |
| --- | --- | --- | --- |
| signalLabel 配置与编排方不一致 | 操作不触发或错误执行 | 有效绑定、matched 日志 | 从生效配置生成绑定并做集成验证 |
| callback 位置或顺序误配 | 摘流前保存、恢复前上线等业务错误 | 配置审查、顺序测试 | 提供部署示例；内部安全顺序由 callback 保证 |
| 依赖 signalLabel 分次写入或跨 Executor | 后置操作先执行 | RI 更新日志 | 同次写入、同一 Executor；否则需要编排方额外协调 |
| callback / PATCH 长期失败 | 后续匹配项饥饿 | 失败项及 signalLabel 存留日志 | fail closed、退避和告警；不静默跳过 |
| 陈旧事件、signalLabel 重用或同名 RI 重建 | 重复 / 过期业务副作用 | resourceVersion、callback 日志 | GET 复核、条件 PATCH、幂等；编排方约束重用并收束旧 Executor，不宣称 UID fencing |
| 上线后 Executor 重启 | heartbeat 丢失 | 重启日志、router 成员状态 | 允许上线时由编排方重新触发 signalLabel，不从 signalLabel 缺失推断 |
| 恢复前信号到达时 Executor 不可用 | 恢复流程阻塞 | signalLabel 存留和进程日志 | 保证 Executor 可用，否则不要将该 signalLabel 指给此 sidecar |
| 缺少 router list / remove 或 engine 排空指标 | 无法确认完成 | action / drain 指标 | 发布能力矩阵，无法确认时 fail closed |
| 长连接不在默认指标覆盖范围 | 过早清理 signalLabel | 业务错误率、drain 日志 | 显式评估业务约束，额外 plugin 契约另行定义 |
| 误在 start 配置上线 | 摘流后重启重新注册 | 有效配置日志 | RBG 部署显式配置空 start，校验部署参数 |
| 配置路径错误或文件缺失 | 拒绝启动，实例不可用 | 启动失败日志 | 文件失败不回退到启动注册，校验部署参数并告警 |

## 缺点

1. 有 signalLabel 绑定的部署新增 Kubernetes API、RBAC 和配置依赖，部署复杂度上升。
2. signalLabel 没有结构化业务结果或执行编号；只能通过日志和指标定位等待原因。
3. 串行 fail closed 会阻塞后续匹配项，可能无限期等待，需要运维处理。
4. 独立插件提升复用，但用户承担 hook 到 signalLabel 的映射和跨功能排序的正确性责任。
5. 仍耦合 RoleInstance 资源和 signalLabel 协议；低耦合仅指不理解 RBG hook，不表示与 RBG 完全无关。

## 替代方案

### 只保留 startup 注册

优点是改动最小，缺点是无法参与 RBG 控制的上线、更新、恢复、删除协作，无法在恢复前清理陈旧路由，也无法安全摘流和排空。

### RBG controller 直接调用 Patio HTTP API

优点是控制直接，缺点是 controller 必须理解业务 API、网络连通和 Executor 可用性，职责耦合。RBG KEP 选择 signalLabel 协作，本 proposal 保持该边界。

### 保留注册模式与 hook 分发

模式互斥和 hook 枚举能约束固定流程，但限制 start / signalLabel / end 的组合，并要求 runtime 随 RBG hook 变化。本文采用统一绑定，不保留这些分支；存量行为通过默认 callback 列表表达。

### 在 Patio 内实现 Executor 选举

可以自动决定 Executor，但引入 Lease、授权和脑裂问题。本 proposal 认为谁执行 hook 是部署 / 编排决策，不是 runtime 决策。

## 测试计划

### 单元测试

1. 注册：导入不执行业务；无状态定义与应用级绑定分开；重复名称拒绝；新模块需显式导入；装配多个应用时注册表和 client 隔离，同一应用的 router callbacks 共享注册服务。
2. 配置：未知字段 / plugin、错误类型、重复 YAML key、null 列表、非法 signalLabel、完全相同的 signalLabel 重复绑定拒绝，同 key 不同 value 允许；省略和空列表合法；空 label value 与 key 缺失区分。
3. 默认值：无事件文件时按是否启用 topo client 生成默认绑定；显式空配置不注册；文件读取失败和旧草案 ENV 不静默回退；hook 非空要求 RI 名称。
4. 映射：三类列表保留顺序，每条 `HookBinding` 只有一个 callback，无事件名或 handler 枚举。
5. 调度：同批 signalLabel 按配置顺序执行，与 label 字典顺序无关；缺失项跳过；执行期间新 signalLabel 留到下一轮；重复事件不并发执行。
6. 失败：callback 或清除失败不执行后续项；没有 watch 新事件也会重试；重启后跳过已清除项，允许重放未确认项。
7. hook 无关：生命周期 label 缺失、未知或变化都不影响 signalLabel 匹配；不因生命周期变化抢占，不从 signalLabel 缺失推断上线。
8. signalLabel 清理：仅 callback 成功后条件 PATCH，保护其他 labels；覆盖 409、响应丢失、GET 失败、改值、删除和 RI 不存在。
9. 业务 callback：onboard 自检 ready；offboard 停 heartbeat、等待在途 register、注销、排空；cleanup 不访问 engine drain；重试不重启 heartbeat。
10. drain：SGLang running / queue、vLLM running / waiting / swapped 的全部适用 gauge 连续为零才 idle；缺失为 unsupported，网络 / 解析 / NaN 为 unknown；任何非 idle 重置计数。
11. 信号：start 等待期间可退出；当前 callback 与 end 不并发；end 按序共用期限，失败 best-effort，不清理 hook signalLabel；无 end 时只收束本地资源。
12. 模块边界：使用 fake callback / hook adapter 独立测试 Executor；确认 callback 失败不调用 ack，ack 失败不处理下一项；事件层不直接控制 heartbeat 或导入业务 client。
13. 能力依赖：空 hook 不创建 Kubernetes client；仅 cleanup 不要求 engine drain；未选用的业务不要求相应依赖；独立 engine_wait_ready 不注册，onboard 不依赖此前已执行该 callback。

### 集成测试

1. fake Kubernetes API 覆盖初始 LIST、WATCH 重连、resourceVersion 过期、陈旧事件、条件 PATCH 冲突及对象删除。
2. mock router / proxy 验证幂等注册注销、句柄丢失、remove 失败和 heartbeat；用不可达 engine 验证 cleanup 仍可成功。
3. mock engine metrics 验证 offboard 完整顺序和部分失败保留 signalLabel。
4. 用测试 callback 模拟路由 / KVCache：同次设置两个 signalLabel，验证逐项确认与失败阻塞，不要求实现 KVCache。
5. 验证缺省绑定兼容、显式空 start 不注册、空 hook 不访问 Kubernetes，以及信号 end 配置生效。
6. 与 Uvicorn 联合验证统一关闭入口：start 等待不阻塞 HTTP 服务，启动失败清理部分资源，关闭时先收束当前操作和后台注册再执行 end；空 end 不隐式注销，所有资源受同一退出预算约束。

### 端到端测试

仅在 mock 集成无法覆盖 Kubernetes 与真实 engine 行为时执行：

1. 真实 RBG controller + Patio + SGLang router 的 create / ready / recover / update / delete 流程。
2. 真实 RBG controller + Patio + vLLM proxy 的 `/instances/add` / `/instances/remove` 流程。
3. Executor 重启、router 重启、Kubernetes API 短暂不可用和 engine 指标暂时不可用。
4. 版本偏差矩阵：缺省兼容绑定、RBG signalLabel 配置、新增 RBG hook；验证与生命周期 label 无关。
5. 上线 signalLabel 清除后重启 Executor，由编排系统明确重新触发上线以恢复 heartbeat。

### 失败与对抗测试

1. 注入非法配置、缺失 RBAC、403 / 409 / 5xx、超时、慢 router 和损坏响应，验证不误清 signalLabel 或回退启动注册。
2. 在途 heartbeat register 与下线竞争，验证注销确认后不再被旧请求重新注册。
3. 在扫描中途同次新增两个依赖 signalLabel，验证下一轮从头检测；后置 signalLabel 先单独出现的反例应表明本地列表不能提供依赖屏障。
4. callback 成功但 PATCH 响应丢失、重启、signalLabel 改值 / 撤销、同名 RI 重建；验证保守重试、仅清自己的 key，并记录无 UID fencing 的边界。
5. end 超时、SIGKILL、start 等待 engine 死亡，以及长期失败阻塞后续匹配项；不能伪报业务完成。

### 基准测试

不承诺业务排空时间 SLO。测量有 / 无 hook 绑定时的 CPU、内存、文件描述符、网络开销，以及不同绑定数量下的扫描和事件合并开销。

## 上线计划

1. 先拆业务能力：提取 registration adapters、heartbeat、注册服务和 `endpoints.py`，将 health / 排空能力归入 `engine/`；保留 topo server 与必要兼容入口。
2. 建立 `application.py`、`events/` 和独立 `callbacks/`，实现应用级依赖绑定、事件文件和兼容默认值；删除旧启动注册 / 信号注销旁路，验证异步 I/O 与统一退出收束。
3. 接入 `events/hooks/roleinstance.py` 的观察、复核与 ack，不改变 callback 接口；补充 RBAC、能力矩阵、日志、指标及顺序 / 重试测试。
4. 编排系统注入明确的 start / signalLabel / end 配置，小范围验证现有 RBG hook 映射、多 signalLabel 顺序和 Executor 重启后的重新触发。
5. 确认无遗留 signalLabel、顺序错误和误注册后扩大范围。
6. 回滚前停止新操作并处理未完成 signalLabel；确认允许恢复启动注册后才替换配置或回滚版本，不把手动删除 signalLabel 当作成功。
7. signalLabel 协议变化时显式迁移；RBG 仅调整 hook 名称或 hook 到 signalLabel 的映射而 signalLabel 协议不变时，无需修改 Executor 分发。

## 开放问题

本 proposal 的执行规则已确定：Patio 定义独立 callback，RBG 通过 signalLabel 调用；一 signalLabel 一 callback；同次设置依赖 signalLabel 并由同一 Executor 按配置顺序处理；不读取生命周期 label；start / end 显式配置；直接观察 RI；失败不绕过；end 仅 best-effort。

注册协议与默认排空参数沿用上述定义。首版不提供 UID fencing、执行代际、跨 Executor 依赖或持久化 heartbeat 恢复。

上线前需由部署侧验证：RBG 能否在同次更新中设置依赖 signalLabel、Recovery 期间 Executor 是否可用、上线后重启如何重新触发，以及进程关闭预算如何与 Pod grace period 配合。KVCache 和业务自定义排空的具体 plugin 契约不在本 proposal 内。

## 参考

1. RBG KEP-527：`rbg/keps/527-roleinstance-lifecycle/README.zh-CN.md`
2. RBG KEP 模板：`rbg/keps/NNNN-kep-template/README.md`
3. 本仓库 proposal 模板：`proposals/NNNN-template/README.md`

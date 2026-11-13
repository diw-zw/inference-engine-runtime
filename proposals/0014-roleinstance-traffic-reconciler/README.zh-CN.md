# 提案 0014：Patio 作为 RoleInstance 流量调和器

| 条目 | 内容 |
| --- | --- |
| 状态 | 草案 |
| 日期 | 创建于 2026-11-04；更新于 2026-11-13 |
| 作者 | inference-engine-runtime maintainers |
| 参考 | sgl-project/rbg#546（KEP-527 草案） |
| 范围 | 面向单个 RoleInstance 的单写者流量调和；先支持 SGLang；vLLM 后续跟进 |

## 概述

Patio 成为单个 RoleInstance（RI）的流量调和器。

只有一个 reconcile loop 是路由成员关系的写入者。每一轮都先计算当前 worker 是否应该注册，再与路由器报告的实际状态比较，并据此执行 add 或 remove。心跳只是这个循环的周期性执行。启动、SIGTERM、hook 状态变化和 Pod 注解变化都只是事件，它们更新各自的条件并唤醒循环，绝不直接写路由成员关系。

这个设计移除了 `REGISTRY_MODE`、先停心跳再反注册的顺序，以及 hook 任务抢占。Patio 重启后从 RI 当前状态收敛，而不是重放历史事件。

## 动机

当前 `topo_client` 的行为是：

```text
Patio 启动
  -> 推导 router / worker / engine 地址
  -> 等待 engine ready
  -> 注册
  -> 周期性重注册
  -> SIGTERM / SIGINT 时反注册
```

这适合不受管理的 startup 注册，但不适合由 RBG 控制的实例：

1. worker 可能在 RBG 允许之前就接流量。
2. RBG Recovery 期间，路由成员关系可能残留。
3. RBG 已经移除流量后，进程重启可能把 worker 再次注册回去。
4. startup 注册、心跳和 SIGTERM 处理是三个独立的写入者，却写同一个路由状态。

## 目标

1. 默认保留现有 startup 注册行为。
2. 让一个 reconcile loop 成为路由成员关系的唯一写入者。
3. 从相互独立、可观测的条件推导期望状态。
4. 让 router adapter 和 engine probe 可以独立变化。
5. 只有确认移除后才清除 RBG marker。
6. 让 Patio 重启后能从当前进程、Pod 和 RI 状态收敛。

## 非目标

1. 不修改 RBG API、RoleInstance spec 或 controller 状态机。
2. 不实现 executor 选举、lease 或授权。
3. 不管理 Pod finalizer 或删除顺序。
4. 不定义多 Pod RoleInstance 的成员发现协议。
5. 不实现业务自定义 drain 检查。
6. v1 不在 in-Pod executor 中实现 `preRecover`；当 Pod 可能已经消失时，需要独立部署的 executor。

## 期望状态模型

循环计算：

```text
should_register =
    process_condition
AND engine_condition
AND manual_drain_condition
AND rbg_condition
```

### 条件

| 条件 | 来源 | 为真的条件 |
| --- | --- | --- |
| Process | 进程生命周期 | 进程已启动且未收到 SIGTERM/SIGINT |
| Engine | 由 `INFERENCE_ENGINE` 选择的 engine probe | engine 上报 ready |
| Manual drain | 通过 downward API 投影到文件的 Pod 注解 | drain 注解不存在或为 false |
| RBG | RoleInstance status 和 hook-state 标签 | `status.phase` 为 Running，且 hook-state 不存在或为 PostReady |

Manual drain 使用 Pod 注解：

```text
patio.workloads.x-k8s.io/drain=true
```

该注解通过 downward API 投影到：

```text
/etc/patio/annotations
```

这个文件能跨越 Patio 重启，且不需要 Pod RBAC。Patio 会周期性重读该文件，也可以通过 inotify 监听投影目录以更快响应。

### 调和规则

每一轮执行：

1. 重新计算 `should_register`。
2. 通过 router adapter 查询当前成员事实。
3. 如果 `should_register` 为真且成员不存在，执行 add。
4. 如果 `should_register` 为假且成员存在，执行 remove。
5. 其他情况不执行动作。
6. 对 unknown 成员或失败操作，在下一轮以有限退避重试。

心跳不是独立的注册线程，而是执行相同比较的周期性 pass。

### 发布事实

循环发布两个事实：

| 事实 | 取值 | 含义 |
| --- | --- | --- |
| Member | present / absent / unknown | 该 instance identity 在路由器中的成员状态 |
| Quiescent | yes / no / unknown | engine 是否没有在途或排队工作 |

RBG marker 的确认依赖这两个事实：

| RBG hook | 需要的事实 |
| --- | --- |
| postReady | member present |
| preUpdate | member absent 且 quiescent |
| preDelete | member absent 且 quiescent |
| preRecover | member absent（in-Pod v1 不支持） |

## 架构

### Router adapter

由 `TOPO_TYPE` 选择的 router adapter 实现：

```text
add(instance)
remove(instance)
query(instance)
```

Adapter 自己拥有 instance identity 和成员检查。SGLang 使用稳定的 worker endpoint；Motor 类 adapter 可以把一组 DP endpoint 视为一个 instance。这样通用模型就不会假设一个 instance 只有一个 endpoint。

Adapter 内不做重试循环。一次调用就是一次带超时的尝试，退避属于 reconcile loop。

### Engine probe

由 `INFERENCE_ENGINE` 选择的 engine probe 实现：

```text
ready()
quiescent()
```

Probe 独立于 router。一个 vLLM engine 可以位于 vLLM proxy、Motor 或其他 router 后面。

SGLang PD 必须按 `worker_type` 选择指标：

```text
prefill:
  sglang:num_running_reqs
  sglang:num_queue_reqs
  sglang:num_prefill_bootstrap_queue_reqs
  sglang:num_prefill_inflight_queue_reqs

decode:
  sglang:num_running_reqs
  sglang:num_queue_reqs
  sglang:num_decode_prealloc_queue_reqs
  sglang:num_decode_transfer_queue_reqs
```

只有所有适用 gauge 在配置的连续采样次数内都保持为零，才判定 idle。

vLLM V1 只使用 running 和 waiting。`vllm:num_requests_swapped` 不是 V1 指标，因此不是必需项。

### vLLM 状态

v1 不声称支持 vLLM。上游示例 proxy 只文档化了 `/instances/add`，可用的 remove API 尚未确认。在找到具体 proxy 和版本并提供可确认的 `/instances/remove` 之前，选择 vLLM 并启用 RBG reconciliation 属于 unsupported，启动时 fail closed。

### RBG marker 处理

Patio 只 watch 由 `LIFECYCLE_RI_NAME` 指定的 RI。

Marker key 名称和状态值有意跟随 KEP-527。由于 sgl-project/rbg#546 仍然打开，本提案保持 draft，并会在该 PR 合并后固定最终 ABI。

清除 marker 之前：

1. GET 最新 RI。
2. 确认 marker key 和 value 仍然匹配。
3. 使用 JSON Merge Patch 只修改该 key。
4. 使用 GET 返回的 `resourceVersion` 作为乐观并发前置条件。
5. 冲突时重新 GET 并重试。

Patio 不会覆盖整个 label map，不写 spec，也不管理 finalizer。

### 模块布局

```text
patio/
├── app.py
├── application.py
├── registration/
│   ├── reconciler.py       # 单写者和期望状态比较
│   ├── conditions.py       # 进程、Pod 文件和 RBG 条件
│   ├── adapters/
│   │   ├── sglang.py
│   │   └── vllm.py         # 后续；被 remove API 阻塞
├── engine/
│   └── probes/
│       ├── sglang.py
│       └── vllm.py
├── metrics/
├── api/
└── topo/                   # 保留现有 server 兼容性
```

从现有代码迁移：

| 现状 | 目标 | 变化 |
| --- | --- | --- |
| `topo/client/sgl_topo_client.py` | `registration/adapters/sglang.py` | 保留 add、remove 和 list-worker 语义；移除心跳和重试所有权 |
| `topo/client/vllm_topo_client.py` | 未来的 `registration/adapters/vllm.py` | remove 可验证前不启用 |
| `topo/factory.py` | `application.py` 中的 adapter 选择 | `topo/server` 保持不变 |
| topo client 中的 engine 健康等待 | `engine/probes` | 把 readiness 和 quiescence 与注册分离 |
| `app.py` 的 startup 和信号处理 | `registration/reconciler.py` | 用条件更新替代直接 register/unregister |
| `metrics/engine_metric_rules.py` | 不变 | 由 engine probe 消费 |

## 安全

1. 为 Patio 使用专用 ServiceAccount。
2. 只授予 Pod namespace 内 `roleinstances` 的 `get`、`list`、`watch`、`patch`。
3. Pod 上设置 `automountServiceAccountToken: false`。
4. 只把 projected ServiceAccount token volume 挂载到 Patio 容器。
5. 不把 token 暴露给 engine 容器。

## 兼容性

1. 没有 RBG reconciliation 配置时，保留现有 startup 注册行为。
2. startup 注册和 SIGTERM 反注册使用同一个 reconcile loop。
3. 不存在 `REGISTRY_MODE`。
4. 现有 `TOPO_TYPE`、`INFERENCE_ENGINE` 和 endpoint 配置继续作为 adapter 选择输入。
5. 行为保持不变的重构不依赖 KEP-527 合并。

## 风险与缓解

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| router 或 engine probe 响应慢，阻塞循环 | 注册收敛延迟 | 每轮只尝试一次，严格超时和有限退避；下一轮重试 |
| KEP-527 修改 marker ABI | marker 处理需要调整 | 把 marker 观察隔离在 RBG condition 和 adapter 中；不耦合 router 或 engine adapter |
| vLLM remove 仍未确认 | vLLM 无法安全支持 pre-hook | 在确认具体 proxy 和版本前 fail closed |
| manual-drain 文件变化延迟 | 移除时机晚于预期 | 周期性重读，辅以可选 inotify |
| unknown membership 阻塞 marker 清理 | RBG 等待 | 暴露 member 和 quiescent 事实，记录阻塞条件并告警 |
| engine 指标误报 idle | 过早移除流量 | 要求所有适用指标连续多次为零 |

## 备选方案

### 保留当前多写者设计

改动最小，但 startup、心跳和 SIGTERM 会竞争，并可能在 RBG 移除后重新注册 worker。还必须显式维护先停心跳再反注册的顺序。

### 让 RBG 直接调用 Patio

单次请求更容易理解，但会把 controller 绑定到 Patio 的网络、可用性和业务 API。基于 label 的 marker 让 controller 不依赖具体 executor。

### 使用显式注册模式

`REGISTRY_MODE=startup | hook | none` 看似简单，但混合了时间、机制和开关。它还让 startup 和 hook 互斥，并且重启后无法收敛。期望状态模型彻底移除了这个模式。

## 可观测性

日志记录生效条件、RI `resourceVersion`、router 查询结果、reconcile 决策、adapter 操作和 marker 清理结果。不要记录 ServiceAccount token。

建议指标：

| 指标 | 类型 | 标签 | 含义 |
| --- | --- | --- | --- |
| `patio_reconcile_pass_total` | Counter | `result` | reconcile pass 结果 |
| `patio_reconcile_condition` | Gauge | `condition` | 当前条件值 |
| `patio_reconcile_member_state` | Gauge | `engine`, `protocol` | member 事实：present / absent / unknown |
| `patio_reconcile_quiescent_state` | Gauge | `engine`, `source` | quiescent 事实：yes / no / unknown |
| `patio_reconcile_action_duration_seconds` | Histogram | `operation` | router add/remove/query 和 marker 清理延迟 |

## 发布顺序

1. 先把现有注册重构为单 reconcile loop、router adapter 和 engine probe，同时保留 startup 行为。
2. 在该重构之上加入 SGLang bring-up 和 drain。
3. KEP-527 合并后，再加入 RBG marker 集成。
4. 只有确认具体 proxy 和版本支持可确认的 remove API 后，才加入 vLLM。

## 测试计划

1. 单测每个条件和期望状态真值表。
2. 使用 fake router adapter 和 engine probe 验证 add/remove/no-op 决策。
3. 验证 startup、SIGTERM、hook 状态变化和 manual-drain 文件变化只会唤醒循环。
4. 验证 Patio 重启后能从当前 RI 和 Pod 状态收敛。
5. 验证 SGLang PD drain 对 prefill 和 decode 使用正确指标。
6. 验证 vLLM V1 drain 只使用 running 和 waiting。
7. 在 remove API 支持前，验证选择 vLLM 并启用 RBG reconciliation 时 fail closed。
8. 验证只有 membership 和 quiescence 满足 hook 条件后才清除 marker。
9. 验证 projected token 在 Patio 和 engine 容器之间隔离。

## 开放问题

1. sgl-project/rbg#546 合并后的最终 KEP-527 marker ABI。
2. 提供可确认 remove API 的具体 vLLM proxy 和版本。

## 参考

1. sgl-project/rbg#546
2. `proposals/README.md`

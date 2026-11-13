# Proposal 0014: Patio as RoleInstance Traffic Reconciler

| Item | Content |
| --- | --- |
| Status | draft |
| Date | Created 2026-11-04; updated 2026-11-13 |
| Author | inference-engine-runtime maintainers |
| Reference | sgl-project/rbg#546 (KEP-527 draft) |
| Scope | Single-writer traffic reconciliation for one RoleInstance; SGLang first; vLLM via xPyD-proxy v1.7.0+ |

## Summary

Patio becomes a traffic reconciler for one RoleInstance (RI).

One reconcile loop is the only writer of router membership. Each pass computes whether this worker should be registered, compares that decision with what the router reports, and adds or removes the worker to match. The heartbeat is just the periodic pass. Startup, SIGTERM, hook-state changes, and annotation changes are events that update their own condition and wake the loop; they never write router membership directly.

This removes the need for `REGISTRY_MODE`, the stop-heartbeat-then-unregister ordering, and hook-task preemption. A Patio restart converges from the RI's current state instead of replaying historical events.

## Motivation

Current `topo_client` behavior is:

```text
Patio starts
  -> derive router / worker / engine endpoints
  -> wait for engine readiness
  -> register
  -> periodically re-register
  -> unregister on SIGTERM / SIGINT
```

This is suitable for unmanaged startup registration, but not for RBG-controlled instances:

1. A worker can receive traffic before RBG allows it.
2. Router membership can remain stale during RBG recovery.
3. A process restart can re-register a worker after RBG removed it.
4. Startup registration, heartbeat, and SIGTERM handling are three separate writers of the same router state.

## Goals

1. Preserve existing startup registration behavior by default.
2. Make one reconcile loop the only writer of router membership.
3. Derive desired membership from independent, observable conditions.
4. Let router adapters and engine probes vary independently.
5. Confirm removal before clearing RBG markers.
6. Make Patio restart converge from current process, Pod, and RI state.

## Non-Goals

1. Do not modify the RBG API, RoleInstance spec, or controller state machine.
2. Do not implement executor election, leases, or authorization.
3. Do not manage Pod finalizers or Pod deletion ordering.
4. Do not define member discovery for multi-Pod RoleInstances.
5. Do not implement business-custom drain checks.
6. Do not implement `preRecover` in an in-Pod executor in v1; an independently deployed executor is required when the Pod may already be gone.

## Desired-State Model

The loop computes:

```text
should_register =
    process_condition
AND engine_condition
AND manual_drain_condition
AND rbg_condition
```

### Conditions

| Condition | Source | True when |
| --- | --- | --- |
| Process | Local process lifecycle | The process has started and has not received SIGTERM/SIGINT |
| Engine | Engine probe selected by `INFERENCE_ENGINE` | The engine reports ready |
| Manual drain | Pod annotation projected through the downward API | The drain annotation is absent or false |
| RBG | RoleInstance status and hook-state label | `status.phase` is Running and hook-state is absent or PostReady |

Manual drain uses the Pod annotation:

```text
patio.workloads.x-k8s.io/drain=true
```

The annotation is projected through the downward API into:

```text
/etc/patio/annotations
```

The file survives Patio restarts and needs no Pod RBAC. Patio rereads it periodically and may watch the projected directory with inotify to react sooner.

### Reconcile rule

Each pass:

1. Recompute `should_register`.
2. Query the router adapter for the current membership fact.
3. If `should_register` is true and membership is absent, add.
4. If `should_register` is false and membership is present, remove.
5. Otherwise take no action.
6. Retry unknown membership or failed operations on the next pass with bounded backoff.

The heartbeat is not a separate registration thread. It is the periodic pass that performs the same comparison.

### Published facts

The loop publishes two facts:

| Fact | Values | Meaning |
| --- | --- | --- |
| Member | present / absent / unknown | Router membership for this instance identity |
| Quiescent | yes / no / unknown | Engine has no in-flight or queued work |

RBG marker acknowledgment depends on these facts:

| RBG hook | Required facts |
| --- | --- |
| postReady | member present |
| preUpdate | member absent and quiescent |
| preDelete | member absent and quiescent |
| preRecover | member absent (out of scope for in-Pod v1) |

## Architecture

### Router adapter

A router adapter selected by `TOPO_TYPE` implements:

```text
add(instance)
remove(instance)
query(instance)
```

The adapter owns instance identity and the membership check. SGLang uses a stable worker endpoint; a Motor-style adapter may treat a group of DP endpoints as one instance. This keeps the generic model from assuming one endpoint per instance.

Adapters must not contain retry loops. One call is one attempt with a timeout. Backoff belongs to the reconcile loop.

### Engine probe

An engine probe selected by `INFERENCE_ENGINE` implements:

```text
ready()
quiescent()
```

The probe is independent of the router. A vLLM engine can sit behind the vLLM proxy, Motor, or another router.

For SGLang PD, the probe must select metrics by `worker_type`:

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

Idle requires every applicable gauge to remain zero for the configured number of consecutive samples.

For vLLM V1, drain uses running and waiting only. `vllm:num_requests_swapped` is not a V1 metric and is not required.

### vLLM status

The concrete proxy is xPyD-proxy v1.7.0 or later, currently v1.8.0.

It implements authenticated `POST /instances/add` and `POST /instances/remove` with the same `type` + `instance` payload used by the current Patio client. Removal marks the target `draining`, waits for active requests to reach zero, then removes it from discovery, health checks, scheduling, and the registry. A timeout returns `504` while leaving the instance draining, so the same removal can be retried.

The admin endpoints require an `X-API-Key` header matching the proxy's `ADMIN_API_KEY`; the vLLM router adapter must send it.

For completeness:

- upstream core vLLM's `examples/disaggregated/disaggregated_serving/disagg_proxy_demo.py` only documents `/instances/add`;
- vLLM Ascend v0.30.0 also has `/instances/remove`, but its payload uses `type` + `instances` as a list, not the singular `instance` field used by current Patio.

### RBG marker handling

Patio watches only the RI named by `LIFECYCLE_RI_NAME`.

Marker key names and state values intentionally track KEP-527. Because sgl-project/rbg#546 is still open, this proposal remains draft and will pin the final ABI after that PR merges.

Before clearing a marker:

1. GET the latest RI.
2. Confirm the marker key and value still match.
3. PATCH only that key with a JSON Merge Patch.
4. Use the GET's `resourceVersion` as an optimistic concurrency precondition.
5. On conflict, GET again and retry.

Patio never overwrites the whole label map, never writes spec, and never manages finalizers.

### Module layout

```text
patio/
├── app.py
├── application.py
├── registration/
│   ├── reconciler.py       # Single writer and desired-state comparison
│   ├── conditions.py       # Process, Pod file, and RBG conditions
│   ├── adapters/
│   │   ├── sglang.py
│   │   └── vllm.py         # Future; blocked on remove API
├── engine/
│   └── probes/
│       ├── sglang.py
│       └── vllm.py
├── metrics/
├── api/
└── topo/                   # Preserve existing server compatibility
```

Migration from the current code:

| Current | Target | Change |
| --- | --- | --- |
| `topo/client/sgl_topo_client.py` | `registration/adapters/sglang.py` | Keep add, remove, and list-worker semantics; remove heartbeat and retry ownership |
| `topo/client/vllm_topo_client.py` | future `registration/adapters/vllm.py` | Do not enable until remove is verifiable |
| `topo/factory.py` | adapter selection in `application.py` | Keep `topo/server` unchanged |
| Engine health waiting in topo clients | `engine/probes` | Separate readiness and quiescence from registration |
| `app.py` startup and signal handling | `registration/reconciler.py` | Replace direct register/unregister calls with condition updates |
| `metrics/engine_metric_rules.py` | unchanged | Consumed by engine probes |

## Security

1. Use a dedicated ServiceAccount for Patio.
2. Grant only `get`, `list`, `watch`, and `patch` on `roleinstances` in the Pod namespace.
3. Set `automountServiceAccountToken: false` on the Pod.
4. Mount a projected ServiceAccount token volume only into the Patio container.
5. Do not expose the token to the engine container.

## Compatibility

1. Without RBG reconciliation configuration, preserve existing startup registration behavior.
2. Startup registration and SIGTERM unregistration use the same reconcile loop.
3. There is no `REGISTRY_MODE`.
4. Existing `TOPO_TYPE`, `INFERENCE_ENGINE`, and endpoint configuration remain the adapter selection inputs.
5. No behavior-preserving refactor depends on KEP-527 merging.

## Risks and Mitigations

| Risk | Impact | Mitigation |
| --- | --- | --- |
| A slow router or engine probe blocks the loop | Registration convergence is delayed | One attempt per pass, strict timeouts, and bounded backoff; the next pass retries |
| KEP-527 changes the marker ABI | Marker handling needs adjustment | Keep marker observation isolated in the RBG condition and adapter; do not couple router or engine adapters to it |
| vLLM remove remains unverified | vLLM cannot safely support pre-hooks | Fail closed until a concrete proxy and version are identified |
| Manual-drain file changes are delayed | Removal happens later than desired | Periodic reread plus optional inotify watch |
| Unknown membership blocks marker clearing | RBG waits | Expose member and quiescent facts, log the blocking condition, and alert |
| Engine metrics report a false idle | Traffic is removed too early | Require consecutive zero samples for all applicable metrics |

## Alternatives Considered

### Keep the current multi-writer design

This is the smallest change, but startup, heartbeat, and SIGTERM can race and re-add a worker after RBG removed it. It also forces explicit stop-heartbeat-then-unregister ordering.

### Let RBG call Patio directly

Direct calls are easier to reason about per request, but couple the controller to Patio's network, availability, and business API. Label-based markers keep the controller independent of the executor.

### Use explicit registration modes

`REGISTRY_MODE=startup | hook | none` looks simple, but mixes time, mechanism, and an on/off switch. It also makes startup and hook mutually exclusive and cannot converge after restart. The desired-state model removes the mode entirely.

## Observability

Logs record the effective conditions, RI `resourceVersion`, router query result, reconcile decision, adapter operation, and marker cleanup result. Do not log ServiceAccount tokens.

Proposed metrics:

| Metric | Type | Labels | Meaning |
| --- | --- | --- | --- |
| `patio_reconcile_pass_total` | Counter | `result` | Reconcile pass outcome |
| `patio_reconcile_condition` | Gauge | `condition` | Current condition value |
| `patio_reconcile_member_state` | Gauge | `engine`, `protocol` | Member fact: present / absent / unknown |
| `patio_reconcile_quiescent_state` | Gauge | `engine`, `source` | Quiescent fact: yes / no / unknown |
| `patio_reconcile_action_duration_seconds` | Histogram | `operation` | Router add/remove/query and marker cleanup latency |

## Rollout

1. Refactor current registration into the single reconcile loop, router adapters, and engine probes while preserving startup behavior.
2. Add SGLang bring-up and drain on top of that refactor.
3. Add RBG marker integration after KEP-527 merges.
4. Add vLLM only after a concrete proxy and version with a confirmable remove API are identified.

## Test Plan

1. Unit-test every condition and the desired-state truth table.
2. Use fake router adapters and engine probes to verify add/remove/no-op decisions.
3. Verify startup, SIGTERM, hook-state changes, and manual-drain file changes only wake the loop.
4. Verify a Patio restart converges from current RI and Pod state.
5. Verify SGLang PD drain uses the correct metrics for prefill and decode.
6. Verify vLLM V1 drain uses running and waiting only.
7. Verify xPyD-proxy v1.7.0+ add/remove, `X-API-Key`, drain-and-retry, and timeout behavior.
8. Verify marker clearing only after membership and quiescence facts satisfy the hook.
9. Verify projected token isolation between Patio and engine containers.

## Open Questions

1. Final KEP-527 marker ABI after sgl-project/rbg#546 merges.

## References

1. sgl-project/rbg#546
2. `proposals/README.md`
3. xPyD-proxy v1.7.0 release: https://github.com/xPyD-hub/xPyD-proxy/releases/tag/v1.7.0
4. xPyD-proxy drain-and-remove PR: https://github.com/xPyD-hub/xPyD-proxy/pull/172

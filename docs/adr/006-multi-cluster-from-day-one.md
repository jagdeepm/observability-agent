# ADR-006: Multi-Cluster from Day One

**Status:** Accepted
**Date:** 2026-07-20

## Context

Cloud-native deployments span multiple Kubernetes clusters (prod-us-east-1, prod-eu-west-1, staging, etc.). An observability agent must:

- Receive alerts from different clusters
- Keep investigations isolated per cluster
- Allow cross-cluster correlation when needed (e.g., a service in one cluster depends on a database in another)
- Avoid alert/investigation leakage between clusters

Adding `cluster_id` later would require a migration touching every entity and every query.

## Decision

**`cluster_id` is a first-class dimension on all partitionable entities.** It is added from day one, even though MVP might only use a single cluster.

### Entities with `cluster_id`

| Entity | Scope |
|---|---|
| `Alert` | Partitioned by cluster |
| `Incident` | Partitioned by cluster |
| `Node` | Partitioned by cluster |
| `Edge` | Partitioned by cluster |

### What `cluster_id` enables

- **Alert grouping.** The Alert Receiver groups alerts into incidents only within the same cluster. Alerts from different clusters never merge into the same incident.
- **Topology isolation.** The topology graph is scoped to a cluster. Cross-cluster edges (e.g., `service-a.us-east-1 → db.eu-west-1`) are stored as edges with different `cluster_id` values on source and target.
- **Multi-cluster deployments.** A single agent instance can serve multiple clusters. The `AlertSource` carries the `cluster_id` so the receiver knows which cluster an alert came from.

### Default value

The `cluster_id` defaults to `"default"` for single-cluster deployments. This is zero-cost for MVP — no configuration needed, but the field is there when multi-cluster is needed.

## Consequences

### Positive

- **No migration needed.** Multi-cluster is a configuration change, not a code change. Flip from `"default"` to `"us-east-1"` and it works.
- **Isolation by construction.** An alert for `cluster-A` can never group into an incident for `cluster-B`. The `_find_existing_incident` method filters by `cluster_id`.
- **Future-proof.** Cross-cluster correlation, fleet-wide dashboards, and per-cluster rate limits all build on this dimension.

### Negative

- **Slightly more verbose.** Every entity has a `cluster_id` field. The default value makes it invisible in single-cluster mode.
- **Not a full multi-tenancy solution.** `cluster_id` partitions data but doesn't provide tenant isolation at the DB level. A future multi-tenant deployment would need additional authz scoping.

## Alternatives Not Chosen

- **Flat namespace (no `cluster_id`).** Everything is in one flat space. Adding cluster support later would require a migration and potentially break alert grouping logic. Rejected — the cost of adding `cluster_id` now is zero.
- **Fully hierarchical namespace (`cluster/namespace/service`).** Over-engineering for MVP. `cluster_id` is the right level of granularity. `namespace` is already a label on alerts and nodes.
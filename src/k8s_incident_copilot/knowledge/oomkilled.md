# OOMKilled

## Confirm an out of memory termination
Source: https://kubernetes.io/docs/tasks/configure-pod-container/assign-memory-resource/#exceed-a-container-s-memory-limit

A container that tries to allocate more memory than its limit can be killed by the kernel. If the kubelet restarts it, the previous termination reason can still show `OOMKilled` while the new instance is running.

Action: Run `kubectl describe pod <pod> -n <namespace>` and inspect the last termination reason for the affected container.

## Compare memory usage with container limits
Source: https://kubernetes.io/docs/tasks/configure-pod-container/assign-memory-resource/#specify-a-memory-request-and-a-memory-limit

The memory request guides scheduling, while the memory limit constrains the container's memory use. Usage can exceed the request during normal operation. Compare the affected container's metrics with its limit to assess how much headroom it has.

Action: Compare the affected container's memory usage from `kubectl top pod <pod> -n <namespace> --containers` with its `resources.limits.memory` value when metrics are available.

## Check node memory pressure
Source: https://kubernetes.io/docs/concepts/scheduling-eviction/node-pressure-eviction/#node-out-of-memory-behavior

If a node runs out of memory before the kubelet can reclaim it, the kernel may kill a container on the node. Node memory pressure can affect multiple Pods, so check node conditions and events near the termination time.

Action: Run `kubectl describe node <node>` and look for `MemoryPressure` or out of memory events around the failure.

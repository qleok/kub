# CrashLoopBackOff

## Container restart backoff
Source: https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/#container-restarts

`CrashLoopBackOff` means the kubelet is delaying restart attempts after a container repeatedly exits. The Pod's container status and events can help distinguish an application crash from a configuration error or a failed probe.

Action: Run `kubectl describe pod <pod> -n <namespace>` and inspect the affected container's last state and Pod events.

## Previous container logs
Source: https://kubernetes.io/docs/tasks/debug/debug-application/debug-running-pod/#examine-pod-logs

When a container restarts, its current logs can miss the startup error, exception, or stack trace from the failed run. The `--previous` option retrieves logs for the previous instance of the selected container when available.

Action: Run `kubectl logs <pod> -n <namespace> -c <container> --previous` to inspect the error before the restart.

## Startup and liveness probe failures
Source: https://kubernetes.io/docs/tasks/configure-pod-container/configure-liveness-readiness-startup-probes/#define-startup-probes

A slow application can be restarted before it finishes initializing if the liveness probe runs too early. A startup probe gives the application time to initialize before liveness checks begin. Its failure threshold and period control how long startup can take.

Action: Compare `startupProbe.failureThreshold * startupProbe.periodSeconds` with the application's longest observed initialization time.

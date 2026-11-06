# K8s Incident Copilot

**An AI-Powered Kubernetes Incident Diagnosis Assistant**

K8s Incident Copilot helps triage Kubernetes Pod failures from the status, events, and logs you provide. Phase 1 is a local FastAPI MVP that uses deterministic rules to suggest a likely cause, show what triggered the diagnosis, and recommend what to check next. The service makes no external calls at runtime. You can try every example without a Kubernetes cluster, kubeconfig, or API key.

| Supported failure | What it signals | Kubernetes documentation |
| --- | --- | --- |
| `CrashLoopBackOff` | A container has repeatedly failed and Kubernetes is delaying its next restart. | [Pod lifecycle](https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/) |
| `ImagePullBackOff` | Kubernetes failed to pull a container image and is delaying its next attempt. | [Container images](https://kubernetes.io/docs/concepts/containers/images/) |
| `OOMKilled` | The container was reported as terminated after running out of memory. | [Resource management for Pods and containers](https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/) |

## Run locally

From the repository root, create a virtual environment and start the API. No environment variables are required.

**Windows PowerShell** (with Python 3.13 installed):

```powershell
py -3.13 -m venv .venv
.venv\Scripts\python.exe -m pip install -e '.[dev]'
.venv\Scripts\python.exe -m uvicorn k8s_incident_copilot.main:app
```

**macOS or Linux** (Python 3.11 or later):

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m uvicorn k8s_incident_copilot.main:app
```

Open [the interactive API docs](http://127.0.0.1:8000/docs) to send a request in the browser. To use a sample payload, run these commands from the repository root in a second terminal.

**Windows PowerShell**:

```powershell
curl.exe http://127.0.0.1:8000/health
curl.exe --header 'Content-Type: application/json' --data-binary '@examples/crashloopbackoff.json' http://127.0.0.1:8000/api/v1/diagnose
```

**macOS or Linux**:

```sh
curl http://127.0.0.1:8000/health
curl --header 'Content-Type: application/json' --data-binary '@examples/crashloopbackoff.json' http://127.0.0.1:8000/api/v1/diagnose
```

The health endpoint returns `{"status":"ok"}`. The example request returns a diagnosis with `failure_type` set to `CrashLoopBackOff`. You can substitute [imagepullbackoff.json](examples/imagepullbackoff.json) or [oomkilled.json](examples/oomkilled.json) to try the other cases.

## Diagnosis API

`POST /api/v1/diagnose` accepts a normalized JSON object. Supply observations for the container you are investigating.

| Field | Value |
| --- | --- |
| `pod_name`, `namespace` | Optional strings that identify the Pod. |
| `status` | Optional status string from `kubectl get pods`, including supported reasons prefixed with `Init:`. The displayed status can differ from the Pod phase. |
| `container_reason` | Optional reason from the observed container's current state or last termination, such as `OOMKilled`. |
| `events` | Optional array of objects. Each must include a nonblank string `reason` or `message`. |
| `logs` | Optional array of log strings. |
| `description` | Optional string with additional observations. |

The request must contain at least one nonblank diagnostic observation. Invalid field types, unknown fields, and requests with only Pod identity return HTTP `422`. A valid request with unrecognized observations returns HTTP `200` with `status: "insufficient_evidence"`.

The rules use the highest priority source with a recognized signal: `container_reason`, then `status`, then `events`, then `logs` and `description` together. When the highest matched source contains signals for different supported failures, the response is `insufficient_evidence`. Exit code `137` alone is insufficient to infer `OOMKilled`.

| Response field | Meaning |
| --- | --- |
| `incident_id` | UUID generated for the response. Phase 1 does not persist IDs or provide a lookup endpoint. |
| `status` | `diagnosed` or `insufficient_evidence`. |
| `failure_type` | One of the three canonical labels above, or `null` if the rules cannot select a failure type. |
| `suspected_cause` | A likely explanation based on the observations, or `null` if evidence is insufficient. |
| `severity` | `high`, `medium`, or `unknown`. This is a rule-based estimate of the observed failure, not a measure of workload impact. |
| `evidence` | Array of strings naming the matched signals and where they appeared. |
| `recommended_steps` | Array of suggested checks to confirm the cause or gather more evidence. |
| `references` | Array of `{ "title": "...", "url": "..." }` objects linking to official Kubernetes documentation. These links are built into the service. |
| `diagnosis_method` | `rules`. |

Phase 1 evaluates only the observations in the request. It does not fetch logs or events, call a language model, retrieve documentation, or modify a cluster. Free text is checked for explicit Kubernetes markers, so it cannot establish whether a marker is negated or historical. The suggested cause is a starting point for investigation; the same failure type can have multiple underlying causes. Use synthetic or sanitized data with the local demo. See [Debug Pods](https://kubernetes.io/docs/tasks/debug/debug-application/debug-pods/) for how to gather more evidence.

## Development

```text
src/k8s_incident_copilot/
  __init__.py   Python package
  main.py       FastAPI routes
  schemas.py    Request and response models
  diagnosis.py  Diagnosis rules and recommendations
examples/       JSON requests for the three supported failures
tests/          Automated tests
```

Run the tests and linter from the repository root.

**Windows PowerShell**:

```powershell
.venv\Scripts\python.exe -m pytest
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m ruff format --check .
```

**macOS or Linux**:

```sh
.venv/bin/python -m pytest
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
```

Licensed under the [MIT License](LICENSE).

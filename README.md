# K8s Incident Copilot

**An AI-Powered Kubernetes Incident Diagnosis Assistant**

K8s Incident Copilot helps triage Kubernetes Pod failures from the status, events, and logs you provide. The FastAPI service uses deterministic rules to suggest a likely cause and show what triggered the diagnosis. An optional retrieval mode adds troubleshooting guidance from a bundled Kubernetes knowledge base. You can run every example offline without a cluster, kubeconfig, or API key.

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

Use `?mode=rules` for the Phase 1 baseline or `?mode=rag` to retrieve and apply documentation. Omitting `mode` uses rules. Unsupported mode values return HTTP `422`.

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
| `incident_id` | UUID for this response. Reports are returned synchronously and are not stored. |
| `status` | `diagnosed` or `insufficient_evidence`. |
| `failure_type` | One of the three canonical labels above, or `null` if the rules cannot select a failure type. |
| `suspected_cause` | A likely explanation based on the observations, or `null` if evidence is insufficient. |
| `severity` | `high`, `medium`, or `unknown`. This is a rule-based estimate of the observed failure, not a measure of workload impact. |
| `evidence` | Array of strings naming the matched signals and where they appeared. |
| `recommended_steps` | Array of suggested checks to confirm the cause or gather more evidence. |
| `references` | In rules mode, each reference has `title` and `url`. Retrieved references also have `chunk_id`, `excerpt`, and `recommended_step`. |
| `diagnosis_method` | `rules+rag` when retrieved guidance is applied, or `rules` otherwise. |

The service evaluates the observations supplied in the request. Free text is checked for explicit Kubernetes markers, so it cannot establish whether a marker is negated or historical. The suggested cause is a starting point for investigation. Use synthetic or sanitized data with the local demo. For ways to gather observations, see [Debug Pods](https://kubernetes.io/docs/tasks/debug/debug-application/debug-pods/).

## Local retrieval and comparison

The [knowledge base](src/k8s_incident_copilot/knowledge/) contains short paraphrases of official Kubernetes documentation. Each Markdown heading defines a passage with a source section and a recommended action. The service reads the bundled files, filters passages by the rule diagnosis, and ranks matching terms from the submitted observations using inverse document frequency. Rare terms such as `unauthorized` can surface narrower guidance than a generic failure marker. The diagnosis itself remains the same.

The report uses up to two passages. The overview of the diagnosed failure can fill in when the observations contain little detail. Each citation links to the official section, identifies the local passage by its `chunk_id`, and includes the exact `recommended_step` it contributed. The service assembles the report locally from these passages and deterministic rules. If the rules cannot identify a supported failure or retrieval has no match, the endpoint returns the rules report. Ranking depends on overlapping words in the small English corpus.

With Uvicorn running, send the same three sample incidents through both modes. The script prints the diagnosis and references for each mode, along with the retrieved recommendations.

```powershell
.venv\Scripts\python.exe scripts/compare_modes.py
```

On macOS or Linux, use `.venv/bin/python scripts/compare_modes.py`.

In the bundled comparison, both modes classified all three examples the same way. Rules returned a general reference for each diagnosis. Retrieval returned two cited actions per example.

| Example | Guidance added by retrieval |
| --- | --- |
| `CrashLoopBackOff` | Inspect the last container state and fetch previous logs with `kubectl logs --previous`. |
| `ImagePullBackOff` | For the `unauthorized` pull, verify the registry Secret in the Pod namespace and read the pull event. |
| `OOMKilled` | Compare container memory usage with its limit and check the last termination reason. |

## Development

```text
src/k8s_incident_copilot/
  __init__.py   Python package
  main.py       FastAPI routes
  schemas.py    Request and response models
  diagnosis.py  Diagnosis rules and recommendations
  retrieval.py  Markdown loader, ranking, and report enrichment
  knowledge/    Bundled Kubernetes documentation summaries
examples/       JSON requests for the three supported failures
scripts/        Rules vs. retrieval comparison
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

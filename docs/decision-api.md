# External Laya choice API

SCS can call any service that implements this HTTP contract with the pinned
Laya model. The service owns model installation, MLX inference, warmup, and
memory use. SCS owns its seven playbook choices and validates every answer.

Set `decision_model = "laya"` and `decision_base_url` in
`~/.scs/config.toml`. The default base URL is `http://127.0.0.1:10000/v1`.
SCS allows loopback hosts by default. An exact non-loopback hostname must be
listed in `decision_trusted_hosts`. Requests carry no authorization header.
Only use a host you trust with query goals and anchors.

## Readiness

`GET {decision_base_url}/decisions/ready` must return HTTP 200 with exactly:

```json
{
  "status": "ok",
  "model": "aac6fef/laya-mlx@20aed815fc6acde75733882e7ec0e3f28aeb9717"
}
```

Return a non-200 status until the pinned model is loaded and warmed. SCS
requires this response before its daemon reports ready and polls it while the
daemon runs. An unavailable or incompatible service prevents configured
startup. A later outage makes SCS unready; SCS waits for bounded recovery and
then shuts down if the service does not return.

## Choice request

SCS sends `POST {decision_base_url}/decisions` with `Content-Type:
application/json`. The request has:

| Field | Meaning |
| --- | --- |
| `model` | The exact pinned model identity above. |
| `state` | A bounded object containing the goal and optional caller anchors. It contains no retrieved source, graph records, or embeddings. |
| `question.key` | `playbook`. |
| `question.instructions` | The routing instruction supplied by SCS. |
| `question.criteria` | A map of the seven SCS playbook labels to descriptions. |

The service must answer within SCS's per-mode classifier budget: 150 ms for
`fast`, 500 ms for `balanced`, and 1 second for `thorough`. SCS can also apply
a lower configured `decision_timeout_seconds` ceiling. A slow or malformed
in-flight answer degrades that query to deterministic discovery.

## Choice response

The HTTP 200 response must be a JSON object with exactly these fields:

| Field | Meaning |
| --- | --- |
| `model` | The same pinned identity. |
| `choice` | One of the supplied playbook labels. |
| `confidence` | A finite number from 0 to 1. |
| `probabilities` | One finite 0-to-1 probability for every supplied label, summing to 1 within 0.01. |

SCS rejects any other model identity, unknown label, missing or extra
probability, nonfinite number, or malformed response. Model output can select
only one fixed SCS playbook; it cannot name tools, files, or execution steps.

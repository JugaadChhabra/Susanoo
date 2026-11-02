# HTTP / n8n-style webhook recipe (tested against local fakes only)

`recipe.py` returns typed `HttpConnection`s for the generic `HttpAgentAdapter` (I03):

- `n8n_chat_connection(url, secret_ref="N8N_WEBHOOK_KEY", header="X-N8N-Key")`: n8n Chat Trigger shape
  (`chatInput`, `sessionId` in; `output` out). The secret is an environment variable *name*.
- `wrapper_connection(url)`: a wrapper that receives the invocation context (with the scoped tool route).

Status: request/response mapping and an end-to-end suite run are tested against local fake servers in
`tests/runtime/test_recipes.py`. No live n8n instance or hosted vendor has been verified, so this is not
claimed as a native n8n connector. For async n8n executions use `AsyncHttpAgentAdapter` the same way.

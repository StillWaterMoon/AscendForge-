# AscendForge MCP

This folder contains the task-scoped MCP integration:

- `contracts.py` — the five tool schemas and argument validation;
- `functions.py` — read/submit tool implementations;
- `server_eval.py` — fixed-test NPU server evaluation backend;
- `server.py` — stdio MCP server.

Start one server per task:

```bash
python -m ascendforge.mcp.server --task-id <task_id> --workspace <draft_dir>
```

Full contracts and examples: [docs/mcp-tools.md](../../docs/mcp-tools.md).

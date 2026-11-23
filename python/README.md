# byobot

**Bring your own bot.** One local database for all your health data, from any device, and a CLI and MCP server so the agent of your choice can read it and coach you.

```bash
byobot init
byobot log effort.performed '{"activity_id": "barbell_back_squat", "metrics": {"reps": {"value": 5}, "load": {"value": 100, "unit": "kg"}}}'
byobot query "SELECT * FROM plan_vs_actual"
byobot mcp            # serve the same commands to an agent over MCP (stdio)
byobot serve          # sync with the iPhone app over your local network
```

Pre-alpha. See https://github.com/byobot/byobot for the data contract, the full command list and the roadmap.

Apache-2.0. Copyright The byobot Authors.

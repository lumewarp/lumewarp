# Playground

The Playground in the Admin UI sends chat requests through the real gateway with one of
your own keys, so what you see is what a client with that key gets: the same names, limits,
budget and usage records.

![The Playground](/screenshots/playground.png)

- Pick one of your active keys, then a name it may call. Only `llm` names are listed.
- The request body is editable as JSON, so any field the upstream supports can be tried.
- After each reply the Playground shows the time to the first token and the tokens per
  second.
- Requests count as usage of the key you picked.

The Playground never sees your key's secret. The control plane asks the gateway on your
behalf with a token that is valid for a few minutes and only for that key.

If the page says the Playground is not configured, the installation has not set the
gateway URL the control plane should use.

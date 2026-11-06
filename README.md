# bazaar-contrib-server-computer

The computers of a Bazaar Compute server: connecting each with a token given once, the addresses its starlink listens and reports on, its pages, and its stall on the overview. `server-relay-memory` keeps the calls waiting for computers and their answers in memory, for a server with one replica.

```toml
[[contribs]]
package = "bazaar-contrib-server-computer~=0.3.0rc1"
[[contribs.enabled]]
entry = "server-relay-memory"
[[contribs.enabled]]
entry = "server-computer"
```

A contrib calls a computer by accepting `COMPUTERS` and hears what computers report through `REPORTED`; a relay for several replicas offers `RELAY` in place of `server-relay-memory`.

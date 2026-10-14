# Architecture and follow-up work

The implementation keeps selection, policy, matching, storage, and interception
separate. `Config` compiles shorthand and resolves shared rules; `Engine` decides
whether to execute or supply an outcome; `Cassette` stores typed results and
reserves occurrences. `Instrumentor` wraps source definitions using an AST
transform and a scoped source import loader. `Addon` adapts mitmproxy HTTP flows
into the same normalized events. The CLI launches either the instrumented Python
entrypoint or an executable configured to use a local proxy.

The cassette envelope includes ID, kind, target, logical input, fingerprint,
sequence allocated at invocation, parent ID, source, timestamps when available,
outcome, and replayability. This initial version intentionally rejects non-JSON
matching inputs; pickle storage is not a canonical matcher.

## Next milestones

1. Typed input/receiver codecs, configurable normalization and identity/redaction
   policies, declarative JSON Schema, resolved target inventories and ambiguity
   diagnostics. Full cassette validation without unpickling.
2. Linux isolation with explicit proxy allowlisting; IPv4/IPv6, UDP and DNS bypass
   tests. Fail closed on unavailable enforcement. No automatic downgrade.
3. Shared coordinator for Python/proxy sessions with explicit causal correlation,
   child interpreter instrumentation, logical concurrency scopes, and complete
   session tracking across hard termination.
4. Streaming HTTP and generator event models, protocol adapters, state restoration
   adapters, transforms of recorded outcomes, and transparent capture integrations.

Do not advertise strict offline, arbitrary-binary network interception, or
whole-program deterministic replay until their enforcement and semantics are
implemented and tested on declared platforms.

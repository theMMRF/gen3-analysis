# Selective file visibility

Set `FILE_VISIBILITY_ENABLED=true` after preparing every served Elasticsearch
index. The default is false. Normal metadata authorization still applies;
Arborist must additionally validate the request's token and supply its download
resources. Authorization mapping failures stop the request with 503 before data
queries. The token and permissions are scoped to a request and propagated to
threaded routes. Guppy proxy calls retain the caller's identity.

`VisibilityElasticsearch` applies filtering under the query builders to direct
search and count, including DSL, aggregations and PIT/search_after pagination.
A global aggregation is rejected. Public documents require an explicit keyword
`_gen3_visibility: public`; unmarked/unknown documents are hidden when enabled.
Restricted documents require `_gen3_visibility: restricted` and a nonempty
keyword `_gen3_visibility_authz` array. Access requires `fence/read-storage` on
**all** resources, matching IndexD and normal Fence download checks.

Prepare all file, case, gene, mutation, CNV, occurrence and project derivatives.
Entire private datasets need policy markers on every projection, even documents
without GUIDs. Mixed parents inherit the union of their private children's
resources, hiding the whole parent unless every grant is present. This avoids
nested metadata and facet leaks; it does not provide child-level redaction.
Use the MMRF GitOps preparation and readiness tools. Markers are an ingestion
contract: IndexD updates do not automatically rewrite Elasticsearch. Remove old
served public copies before restricting previously public data and publish
rebuilt indices atomically. Follow the coordinated runbook for cache eviction,
exports, graph/MDS policy, and rollback.

Run `pytest tests/security`. Set `VISIBILITY_TEST_ES_URL` to a disposable ES 7
cluster for the real query/count/facet/DSL/PIT integration cases. The integration
suite also confirms that unmarked indices are excluded when the flag is enabled.

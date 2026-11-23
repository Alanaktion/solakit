# Web search

Add one tool for finding public sources by keyword or natural-language query.
Use Brave Search, the provider used in v1, and the
[shared tool plan](README.md#follow-up-tool-plans).

## Tool call

`web.search(query, limit=5)` returns ordered results with `title`, `url` and
`snippet`. Accept 1–10 results and a nonempty bounded query. Tell the model that
snippets are excerpts and that it can cite the returned source URLs. Return an
empty list for a successful search with no matches. Keep page fetching, full-text
reading, pagination, provider selection and query options outside this slice.

## Implementation

- Configure `[web_search]` with a credential environment reference. Use the
  existing async HTTP stack, one fixed HTTPS provider origin, verified TLS and
  no redirects. Resolve and register the credential immediately before the
  request. The model controls the search text and limit.
- Confirm the current provider endpoint and response format at implementation.
  Bound response bytes before decoding. Return rank order, accept only HTTP(S)
  result links and strip provider markup from snippets. Do not follow results.
  Bound field sizes so the default tool budget can carry five useful results.
- Report missing credentials, rate limits, timeouts, and provider failures
  separately from an empty search. Skip malformed result rows and fail a
  malformed response envelope. Keep query and snippet data in the normal tool
  evidence. Tool descriptions should steer queries toward terms needed for the
  search, without private transcript text or credentials.

## Acceptance

Use a fake provider response and assert query encoding, order, empty results,
markup handling, response limits, denial before network, and credential
redaction. Verify that a URL in a hit is never fetched. Run a model task that asks
for sources on a current question and check that it uses short queries and cites
returned links. The opt-in live smoke uses a public query and disposable data.

# Wikipedia lookups

Bring forward v1’s Wikipedia summary tool for short encyclopedic lookups. Start
with English Wikipedia and the [shared tool plan](README.md#follow-up-tool-plans).

## Tool call

`wiki.summary(title)` accepts a Wikipedia article title such as `Ada Lovelace`.
Return the resolved title, canonical article URL, and a bounded plain-text lead.
Resolve normal article redirects. Return a distinct missing-title or ambiguous
result so the model can refine the title. For ambiguity, include a small list of
candidate titles when the selected endpoint supplies them. Keep full articles,
other wiki installations and Wikidata queries outside the first slice.

## Implementation

- Use an operator enable switch and the exact tool grant. Query one fixed
  HTTPS origin through the existing HTTP stack. Verify the Wikimedia API
  contract and user-agent requirements during implementation. Use structured
  title, redirect and disambiguation data from the API. Encode the title as an
  argument, reject URL inputs, and bound response bytes before parsing.
- Serve the article title and URL with its summary so the model can cite it.
  Accept a canonical URL only on the configured Wikipedia origin. Keep redirects
  within the API’s article-resolution mechanism. Do not follow HTTP redirects or
  article links. Distinguish rate limits, timeouts and invalid responses from a
  missing article.
- Use the short description “Read the opening summary of a Wikipedia article by
  title.” An uncertain exact title can be refined using the article candidates
  or [`web.search`](../contracts/tools.md#public-web-search) when granted. The wiki service makes
  no web search calls itself.

## Acceptance

Use fixtures for a normal article, a title redirect, a missing title, and a
disambiguation page. Check output bounds, request encoding and no network on
denial. Run a model task that resolves an ambiguous person or place and inspect
whether its final answer links the chosen article. Smoke-test one exact title
and one redirect through the public API with disposable data.
